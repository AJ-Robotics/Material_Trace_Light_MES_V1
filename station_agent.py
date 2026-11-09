"""
Station agent - runs on a machine PC / PLC gateway and pushes production data
to this MTS installation's machine REST API (/api/v1).

Stdlib only on purpose: copy this single file to any PC with Python 3.8+.

    python station_agent.py --help
    python station_agent.py run                     # daemon: heartbeat + queue flush
    python station_agent.py scan --serial SN-1 --barcode MAT-05
    python station_agent.py qc --serial SN-1 --decision PASS
    python station_agent.py downtime start --reason CHANGEOVER
    python station_agent.py status
    python station_agent.py simulate --interval 3 --count 20

Configuration (station_agent.json next to this file, or --config PATH):
    {
      "url": "http://mes-host:5000/api/v1",
      "api_key": "mts_...",
      "station_code": "ST10",
      "heartbeat_seconds": 30,
      "flush_seconds": 5,
      "timeout": 10,
      "max_attempts": 50,
      "queue": "data/station_agent_outbox.db"
    }

Events that cannot be delivered (server down, 5xx, 401/403/503) are stored in
the local SQLite queue and retried by `run`, so a network blip never loses a
scan. Permanent rejections (400/404/409) are reported and dropped.
"""
import argparse
import ipaddress
import json
import os
import random
import socket
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "station_agent.json"
RETRY_STATUSES = {401, 403, 429, 500, 502, 503, 504}


# --------------------------------------------------------------------- config
def load_config(path=None, **overrides):
    cfg = {
        "url": "http://127.0.0.1:5000/api/v1",
        "api_key": "",
        "station_code": "",
        "heartbeat_seconds": 30,
        "flush_seconds": 5,
        "timeout": 10,
        "max_attempts": 50,
        "allow_public_hosts": False,
        "queue": "data/station_agent_outbox.db",
    }
    p = Path(path) if path else Path(os.environ.get("MTS_AGENT_CONFIG", DEFAULT_CONFIG))
    if p.exists():
        with open(p, "r", encoding="utf-8") as fh:
            cfg.update(json.load(fh))
    for k, v in overrides.items():
        if v not in (None, ""):
            cfg[k] = v
    cfg["url"] = str(cfg["url"]).rstrip("/")
    cfg["queue"] = str(p.parent / cfg["queue"])
    return cfg


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ------------------------------------------------------------ target guard
def target_error(cfg):
    """None when the configured MES URL stays on this PC / private LAN.

    The agent must never become a client of a public host: set
    "allow_public_hosts": true in station_agent.json only if you really mean it.
    """
    if cfg.get("allow_public_hosts"):
        return None
    url = str(cfg.get("url", "") or "")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"unsupported scheme '{parsed.scheme}'"
    host = (parsed.hostname or "").strip()
    if not host:
        return "URL has no host"
    if host.lower().rstrip(".") in ("localhost", "localhost.localdomain", "ip6-localhost"):
        return None
    try:
        addr = ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        addr = None
    if addr is not None:
        if addr.is_loopback or addr.is_private or addr.is_link_local:
            return None
        return f"'{host}' is a public address"
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        return f"'{host}' could not be resolved ({exc})"
    ips = sorted({info[4][0] for info in infos})
    if not ips:
        return f"'{host}' could not be resolved"
    for ip in ips:
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            return f"'{host}' resolved to an unusable address ({ip})"
        if not (a.is_loopback or a.is_private or a.is_link_local):
            return f"'{host}' resolves to {', '.join(ips)} - public network blocked"
    return None


# ---------------------------------------------------------------------- queue
def _queue(cfg):
    path = Path(cfg["queue"])
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS outbox (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               path TEXT NOT NULL,
               payload TEXT NOT NULL,
               created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
               attempts INTEGER NOT NULL DEFAULT 0,
               last_error TEXT)""")
    conn.commit()
    return conn


def enqueue(cfg, path, payload, error=""):
    conn = _queue(cfg)
    conn.execute("INSERT INTO outbox (path, payload, last_error) VALUES (?, ?, ?)",
                 (path, json.dumps(payload), error))
    conn.commit()
    conn.close()


def queue_size(cfg):
    conn = _queue(cfg)
    n = conn.execute("SELECT COUNT(*) c FROM outbox").fetchone()["c"]
    conn.close()
    return n


# ----------------------------------------------------------------------- http
def request(cfg, method, path, payload=None):
    """Returns (status, body_dict_or_text). status 0 means network failure."""
    refused = target_error(cfg)
    if refused:
        return 0, f"target refused: {refused}"
    url = f"{cfg['url']}{path}"
    data = None
    headers = {"X-API-Key": cfg.get("api_key", "")}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=float(cfg["timeout"])) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw
    except (urllib.error.URLError, OSError) as e:
        return 0, str(getattr(e, "reason", e))


def _ok_response(status, body):
    return status and 200 <= status < 300 and (not isinstance(body, dict) or body.get("ok"))


def _show(status, body):
    if isinstance(body, dict):
        print(json.dumps(body, indent=2, ensure_ascii=False))
    else:
        print(body)


def send_event(cfg, path, payload, quiet=False):
    """Send now; on transport/retryable errors queue for the daemon."""
    refused = target_error(cfg)
    if refused:
        log(f"target refused ({refused}) - event not sent and not queued")
        return False
    status, body = request(cfg, "POST", path, payload)
    if _ok_response(status, body):
        if not quiet:
            _show(status, body)
        return True
    if status == 0 or status in RETRY_STATUSES:
        enqueue(cfg, path, payload, str(body))
        log(f"server unreachable/rejecting (HTTP {status or '-'}): event queued "
            f"(queue={queue_size(cfg)})")
        return False
    if not quiet:
        print(f"rejected (HTTP {status}):", flush=True)
        _show(status, body)
    return False


def flush(cfg):
    refused = target_error(cfg)
    if refused:
        log(f"target refused ({refused}) - queue held ({queue_size(cfg)} pending)")
        return 0
    conn = _queue(cfg)
    rows = conn.execute("SELECT * FROM outbox ORDER BY id LIMIT 100").fetchall()
    sent = kept = dropped = 0
    for row in rows:
        status, body = request(cfg, "POST", row["path"], json.loads(row["payload"]))
        if _ok_response(status, body):
            conn.execute("DELETE FROM outbox WHERE id = ?", (row["id"],))
            sent += 1
        elif status == 0 or status in RETRY_STATUSES:
            attempts = row["attempts"] + 1
            if attempts >= int(cfg["max_attempts"]):
                conn.execute("DELETE FROM outbox WHERE id = ?", (row["id"],))
                dropped += 1
            else:
                conn.execute("UPDATE outbox SET attempts = ?, last_error = ? WHERE id = ?",
                             (attempts, str(body), row["id"]))
            kept += 1
        else:
            conn.execute("DELETE FROM outbox WHERE id = ?", (row["id"],))
            dropped += 1
            log(f"queued event dropped (HTTP {status}): {body}")
    conn.commit()
    conn.close()
    if sent or kept or dropped:
        log(f"queue flush: sent={sent} retrying={kept} dropped={dropped} "
            f"remaining={queue_size(cfg)}")
    return sent


# ------------------------------------------------------------------ commands
def cmd_heartbeat(cfg, _args):
    status, body = request(cfg, "POST", "/events/heartbeat", {})
    if _ok_response(status, body):
        _show(status, body)
        return 0
    print(f"heartbeat failed (HTTP {status}): {body}")
    return 1


def cmd_scan(cfg, args):
    ok = send_event(cfg, "/events/material_scan",
                    {"serial_number": args.serial, "material_barcode": args.barcode})
    return 0 if ok else 1


def cmd_qc(cfg, args):
    payload = {"serial_number": args.serial, "decision": args.decision}
    if args.fail_status:
        payload["fail_status"] = args.fail_status
    if args.reason:
        payload["reason"] = args.reason
    ok = send_event(cfg, "/events/qc", payload)
    return 0 if ok else 1


def cmd_downtime(cfg, args):
    payload = {"action": args.action}
    if args.reason:
        payload["reason_code"] = args.reason
    if args.notes:
        payload["notes"] = args.notes
    ok = send_event(cfg, "/events/downtime", payload)
    return 0 if ok else 1


def cmd_ncr(cfg, args):
    payload = {"defect_reason": args.reason}
    if args.description:
        payload["description"] = args.description
    if args.serial:
        payload["serial_number"] = args.serial
    ok = send_event(cfg, "/events/ncr", payload)
    return 0 if ok else 1


def cmd_alert(cfg, args):
    ok = send_event(cfg, "/events/alert", {"message": args.message, "level": args.level})
    return 0 if ok else 1


def cmd_status(cfg, _args):
    status, body = request(cfg, "GET", "/stations/me")
    if _ok_response(status, body):
        _show(status, body)
        return 0
    print(f"status failed (HTTP {status}): {body}")
    return 1


def cmd_show(cfg, args):
    status, body = request(cfg, "GET", f"/serials/{urllib.parse.quote(args.serial)}")
    if _ok_response(status, body):
        _show(status, body)
        return 0
    print(f"serial lookup failed (HTTP {status}): {body}")
    return 1


def cmd_run(cfg, args):
    log(f"station agent -> {cfg['url']} (station={cfg['station_code'] or 'from key'})")
    last_hb = 0.0
    try:
        while True:
            now = time.monotonic()
            if now - last_hb >= float(cfg["heartbeat_seconds"]):
                status, body = request(cfg, "POST", "/events/heartbeat", {})
                if _ok_response(status, body):
                    st = body.get("state", "?")
                    log(f"heartbeat ok - state={st} unread_alerts={body.get('unread_alerts', 0)} "
                        f"queue={queue_size(cfg)}")
                else:
                    log(f"heartbeat failed (HTTP {status or '-'}): {body}")
                last_hb = now
            flush(cfg)
            if args.once:
                break
            time.sleep(max(1.0, float(cfg["flush_seconds"])))
    except KeyboardInterrupt:
        log("stopped")
    return 0


# ----------------------------------------------------------------- simulate
def _sample_barcode(rule, material_code):
    rule = (rule or "").strip()
    if not rule:
        return material_code
    if "|" in rule:
        parts = rule.split("|")
        return "|".join([material_code] + [f"SIM{i + 1}" for i in range(len(parts) - 1)])
    if rule.startswith("^"):
        return None
    return material_code


def _pending_for(cfg, serial_number):
    status, body = request(cfg, "GET", f"/serials/{urllib.parse.quote(serial_number)}")
    if not _ok_response(status, body):
        return []
    return body.get("pending_at_calling_station") or []


def cmd_simulate(cfg, args):
    """Automatic station demo: heartbeat, scans, alerts and stoppages from the
    work queue of the calling station. Use on a test station only."""
    status, me = request(cfg, "GET", "/stations/me")
    if not _ok_response(status, me):
        log(f"simulate needs a working station: HTTP {status} {me}")
        return 1
    code = me["station"]["station_code"]
    log(f"simulating automatic station {code} every {args.interval}s "
        f"(count={args.count or 'forever'})")
    done = 0
    while True:
        if args.count and done >= args.count:
            break
        status, me = request(cfg, "GET", "/stations/me")
        if not _ok_response(status, me):
            log(f"heartbeat/query failed (HTTP {status}) - retry later")
            time.sleep(max(1, args.interval))
            continue
        request(cfg, "POST", "/events/heartbeat", {})
        work = me.get("work_queue") or []
        scanned = False
        for item in work:
            pending = _pending_for(cfg, item["serial_number"])
            for p in pending:
                barcode = _sample_barcode(p.get("barcode_rule"), p["material_code"])
                if barcode:
                    status, body = request(cfg, "POST", "/events/material_scan",
                                           {"serial_number": item["serial_number"],
                                            "material_barcode": barcode})
                    if _ok_response(status, body):
                        log(f"scan {item['serial_number']} <- {p['material_code']} ok")
                        scanned = True
                    else:
                        log(f"scan rejected ({status}): {body}")
                    break
            if scanned:
                break
        if not scanned:
            roll = random.random()
            if me.get("state") == "DOWN" and roll < 0.5:
                request(cfg, "POST", "/events/downtime", {"action": "end"})
                log("stoppage ended")
            elif roll < 0.3:
                reasons = request(cfg, "GET", "/downtime/reasons")[1]
                codes = [r["code"] for r in (reasons.get("reasons") or [])] \
                    if isinstance(reasons, dict) else []
                status, body = request(cfg, "POST", "/events/downtime",
                                       {"action": "start",
                                        "reason_code": random.choice(codes) if codes else "",
                                        "notes": "automatic station demo"})
                log(f"stoppage start: HTTP {status}")
            else:
                level = random.choice(["INFO", "WARN", "WARN", "CRIT"])
                request(cfg, "POST", "/events/alert",
                        {"level": level,
                         "message": f"automatic station {code} demo alert ({level})"})
                log(f"alert {level} sent")
        done += 1
        if args.count and done >= args.count:
            break
        time.sleep(max(1, args.interval))
    return 0


# ---------------------------------------------------------------------- main
def build_parser():
    p = argparse.ArgumentParser(description="MTS station agent - push data to the machine REST API")
    p.add_argument("--config", help="path to station_agent.json")
    p.add_argument("--url", help="API base URL, e.g. http://mes-host:5000/api/v1")
    p.add_argument("--key", help="station API key (X-API-Key)")
    p.add_argument("--station", help="station code (informational)")
    p.add_argument("--allow-public-hosts", action="store_true",
                   help="permit a non-private MES URL (default: LAN/loopback only)")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("heartbeat", help="send one heartbeat").set_defaults(func=cmd_heartbeat)

    s = sub.add_parser("scan", help="record a material scan")
    s.add_argument("--serial", required=True)
    s.add_argument("--barcode", required=True)
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("qc", help="record a QC decision")
    s.add_argument("--serial", required=True)
    s.add_argument("--decision", required=True, choices=["PASS", "FAIL", "pass", "fail"])
    s.add_argument("--fail-status", dest="fail_status", choices=["HOLD", "REJECTED", "hold", "rejected"])
    s.add_argument("--reason")
    s.set_defaults(func=cmd_qc)

    s = sub.add_parser("downtime", help="start or end a stoppage")
    s.add_argument("action", choices=["start", "end"])
    s.add_argument("--reason", help="reason code, e.g. MACHINE_FAULT")
    s.add_argument("--notes")
    s.set_defaults(func=cmd_downtime)

    s = sub.add_parser("ncr", help="raise a non-conformance")
    s.add_argument("--reason", required=True)
    s.add_argument("--description")
    s.add_argument("--serial")
    s.set_defaults(func=cmd_ncr)

    s = sub.add_parser("alert", help="push an alert into the MES feed")
    s.add_argument("--message", required=True)
    s.add_argument("--level", default="WARN", choices=["INFO", "WARN", "CRIT"])
    s.set_defaults(func=cmd_alert)

    sub.add_parser("status", help="show this station's work data").set_defaults(func=cmd_status)

    s = sub.add_parser("show", help="serial detail")
    s.add_argument("--serial", required=True)
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("run", help="daemon: heartbeat + flush the offline queue")
    s.add_argument("--once", action="store_true", help="one cycle then exit (testing)")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("simulate", help="automatic station demo (scans/alerts/stoppages)")
    s.add_argument("--interval", type=int, default=5)
    s.add_argument("--count", type=int, default=0, help="0 = run until Ctrl+C")
    s.set_defaults(func=cmd_simulate)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config, url=args.url, api_key=args.key,
                      station_code=args.station,
                      allow_public_hosts=True if args.allow_public_hosts else None)
    if not cfg.get("api_key") and args.command not in ("run",):
        print("No API key configured. Generate one in Configuration -> Machine REST API "
              "and put it in station_agent.json (or pass --key).", file=sys.stderr)
        return 2
    return args.func(cfg, args)


if __name__ == "__main__":
    sys.exit(main())
