"""
Machine REST API — this system acts as the MES server.

Machines (station PCs, PLC gateways, robot controllers) authenticate with a
per-station API key (Configuration page) and call:
  - production events  (material scan, QC decision, downtime, NCR, alert, heartbeat)
  - query endpoints    (serial status, station work data, MOs, materials, alerts)

An interactive docs page lives at /api/docs (offline, no CDN) and the machine
readable spec at /api/v1/openapi.json.
"""
import json
import secrets
from datetime import datetime
from functools import wraps

from flask import Blueprint, g, jsonify, render_template, request

from database.db import db
from services.downtime_service import DowntimeService
from services.genealogy_service import GenealogyService
from services.ncr_service import NCRService
from services.notification_service import notify, recent as notif_recent, unread_count
from services.system_config import ConfigService
from services.transaction_service import TransactionService
from services.validation_service import ValidationService

API_VERSION = "1.0.0"
bp = Blueprint("machine_api", __name__, url_prefix="/api/v1")


# ------------------------------------------------------------------ helpers
def _machine_user_id():
    row = db.one("SELECT id FROM users WHERE username = 'machine'")
    if row:
        return row["id"]
    from auth.authentication import Authentication
    Authentication().create_user("machine", secrets.token_urlsafe(32),
                                 "Machine API (system)", "OPERATOR")
    row = db.one("SELECT id FROM users WHERE username = 'machine'")
    return row["id"]


def _ok(**payload):
    return jsonify({"ok": True, **payload})


def _err(message, status=400):
    return jsonify({"ok": False, "error": message}), status


def _norm_key(k):
    if k is None:
        return ""
    s = str(k).strip().lower().replace(" ", "_").replace("-", "_")
    # collapse multiple
    while "__" in s:
        s = s.replace("__", "_")
    return s

def _get_first(d, *names):
    if not isinstance(d, dict):
        return None
    # try as-is
    for n in names:
        if n in d and d[n] not in (None, ""):
            return d[n]
    # norm
    nd = {_norm_key(k): v for k, v in d.items()}
    for n in names:
        nk = _norm_key(n)
        if nk in nd and nd[nk] not in (None, ""):
            return nd[nk]
    return None

def _get_params(d):
    if not isinstance(d, dict):
        return []
    for n in ("parameters", "measurements", "data", "params", "process_parameters", "values", "results"):
        v = _get_first(d, n)
        if isinstance(v, list):
            return v
        if isinstance(v, dict):
            # maybe list under items
            for n2 in ("items", "list", "rows"):
                if n2 in v and isinstance(v[n2], list):
                    return v[n2]
            return []
    # also scan norm
        nd = {_norm_key(k): v for k, v in d.items()}
        for n in ("parameters", "measurements", "data", "params"):
            if n in nd and isinstance(nd[n], list):
                return nd[n]
    return []


def _get_bindings(d):
    """Child parts installed into the parent unit.

    Accepts ``childParts`` / ``child_parts`` / ``children`` / ``components`` as a
    list of child serial strings, objects with ``serial_number``, or objects with
    a dynamic key carrying a part barcode, e.g. ``{"PartBarcode1": "MAT-06"}``.
    """
    if not isinstance(d, dict):
        return []
    raw = None
    for n in ("childParts", "childparts", "child_parts", "children", "components",
              "Child Parts", "child serial numbers", "child_serials"):
        v = _get_first(d, n)
        if isinstance(v, list):
            raw = v
            break
    if raw is None:
        return []
    out = []
    for item in raw:
        if isinstance(item, str):
            sn = item.strip()
            if sn:
                out.append({"serial_number": sn, "barcode": sn})
            continue
        if not isinstance(item, dict):
            continue
        sn = _get_first(item, "serial_number", "Serial Number", "child_serial",
                        "child_serial_number", "child", "sn", "SN")
        material_code = _get_first(item, "material_code", "material", "Material Code",
                                   "Material", "Part Code", "PartCode")
        barcode = _get_first(item, "barcode", "Barcode", "child_barcode",
                             "Part Barcode", "PartBarcode")
        if sn is None and material_code is None and barcode is None:
            for k, v in item.items():
                if v is None or str(v).strip() == "":
                    continue
                barcode = str(v).strip()
                break
        ident = sn if sn is not None else (barcode if barcode is not None else material_code)
        if ident is None or str(ident).strip() == "":
            continue
        out.append({
            "serial_number": str(ident).strip(),
            "material_code": (str(material_code).strip() if material_code is not None else None),
            "barcode": (str(barcode).strip() if barcode is not None else str(ident).strip()),
            "qty": _get_first(item, "qty", "quantity", "Qty") or 1,
        })
    return out


def _json_body():
    data = request.get_json(silent=True)
    if data is None:
        data = request.form.to_dict()
    return data if isinstance(data, dict) else {}


def _iso(value):
    return str(value)[:19] if value else None


def _extract_key():
    key = (request.headers.get("X-API-Key") or "").strip()
    if not key:
        auth = (request.headers.get("Authorization") or "").strip()
        if auth.lower().startswith("bearer "):
            key = auth[7:].strip()
    if not key and request.method == "GET":
        key = (request.args.get("api_key") or "").strip()
    return key


def machine_key_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not ConfigService.get_bool("api_enabled", True):
            return _err("Machine API is disabled on this server.", 503)
        key = _extract_key()
        if not key:
            return _err("Missing API key. Send X-API-Key: <station key> "
                        "(or Authorization: Bearer <key>).", 401)
        row = db.one(
            """SELECT sc.station_id, sc.api_enabled, sc.api_last_seen,
                      s.station_code, s.station_name, s.station_type, s.stage_code,
                      COALESCE(s.halted, 0) AS halted, s.active, s.line_code
               FROM station_comm sc JOIN stations s ON s.id = sc.station_id
               WHERE sc.api_key = ?""",
            (key,))
        if not row:
            return _err("Invalid API key.", 403)
        if not row["api_enabled"]:
            return _err(f"API access is disabled for station {row['station_code']}.", 403)
        if not row["active"]:
            return _err(f"Station {row['station_code']} is inactive.", 403)
        db.execute("UPDATE station_comm SET api_last_seen = CURRENT_TIMESTAMP "
                   "WHERE station_id = ?", (row["station_id"],))
        g.api_station = row
        g.api_machine_user = _machine_user_id()
        return f(*args, **kwargs)
    return wrapped


def _station_state(station_id):
    ev = DowntimeService().open_event(station_id)
    return ("DOWN", ev) if ev else ("RUNNING", None)


# ------------------------------------------------------------------ system
@bp.route("/health")
def health():
    return _ok(version=API_VERSION, time=datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
               service="mts-machine-api")


@bp.route("/openapi.json")
def openapi():
    from openapi_spec import build_spec
    return jsonify(build_spec(request.host_url.rstrip("/")))


# ------------------------------------------------------------------ events
@bp.route("/events/heartbeat", methods=["POST"])
@machine_key_required
def heartbeat():
    """Machine keeps its link alive; response carries server state the machine
    UI may mirror (open stoppage, alerts)."""
    st = g.api_station
    state, open_ev = _station_state(st["station_id"])
    return _ok(
        server_time=datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        station={"station_code": st["station_code"], "station_name": st["station_name"],
                 "station_type": st["station_type"], "stage_code": st.get("stage_code"),
                 "halted": bool(st.get("halted")), "line_code": st.get("line_code")},
        state=state,
        open_stoppage=(None if not open_ev else {
            "reason": open_ev.get("reason_label"), "notes": open_ev.get("notes"),
            "started_at": _iso(open_ev.get("started_at"))}),
        unread_alerts=unread_count(),
    )


@bp.route("/events/material_scan", methods=["POST"])
@machine_key_required
def material_scan():
    """Record a material barcode scan at the calling station (same validation
    and route control as the operator screen)."""
    body = _json_body()
    serial_number = (body.get("serial_number") or "").strip()
    barcode = (body.get("material_barcode") or body.get("barcode") or "").strip()
    if not serial_number or not barcode:
        return _err("serial_number and material_barcode are required.")
    station_id = g.api_station["station_id"]
    val = ValidationService()
    res = val.validate_serial(station_id, serial_number)
    if not res.ok:
        TransactionService().log_exception(
            operator_id=g.api_machine_user, station_id=station_id, serial=None,
            expected="valid serial at station", scanned=serial_number, error=res.error)
        return _err(res.error)
    serial = res.data["serial"]
    if res.data.get("qc"):
        return _err(f"{g.api_station['station_code']} is a QC station — "
                    "use POST /api/v1/events/qc instead.")
    pending = res.data.get("pending") or val.pending_materials(station_id, serial["id"])
    res2 = val.validate_material(station_id, serial["id"], barcode, pending)
    if not res2.ok:
        TransactionService().log_exception(
            operator_id=g.api_machine_user, station_id=station_id, serial=serial,
            expected=", ".join(a["material_code"] for a in pending),
            scanned=barcode, error=res2.error)
        return _err(res2.error)
    material = res2.data["material"]
    parsed = res2.data.get("parsed")
    tx_id, remaining, done, route_done = TransactionService().record_material(
        operator_id=g.api_machine_user, station_id=station_id, serial=serial,
        material=material, barcode=barcode, parsed=parsed)
    status = db.one("SELECT status FROM serial_numbers WHERE id = ?",
                    (serial["id"],))["status"]
    return _ok(transaction_id=tx_id, serial_number=serial["serial_number"],
               material_code=material["material_code"],
               remaining=[p["material_code"] for p in remaining],
               station_complete=done, route_complete=route_done, serial_status=status)


@bp.route("/events/qc", methods=["POST"])
@machine_key_required
def qc_decision():
    """Record a QC inspection at a QC station (PASS / FAIL→HOLD / FAIL→REJECTED)."""
    body = _json_body()
    serial_number = (body.get("serial_number") or "").strip()
    decision = (body.get("decision") or "").upper()
    fail_status = (body.get("fail_status") or "").upper() or None
    reason = (body.get("reason") or "").strip()
    if not serial_number:
        return _err("serial_number is required.")
    if decision not in ("PASS", "FAIL"):
        return _err("decision must be PASS or FAIL.")
    station_id = g.api_station["station_id"]
    if not ValidationService().is_qc_station(g.api_station):
        return _err(f"{g.api_station['station_code']} is not a QC station.")
    res = ValidationService().validate_serial(station_id, serial_number)
    if not res.ok:
        return _err(res.error)
    serial = res.data["serial"]
    try:
        result = TransactionService().record_qc(
            operator_id=g.api_machine_user, station_id=station_id, serial=serial,
            decision=decision, fail_status=fail_status, reason=reason)
    except ValueError as exc:
        return _err(str(exc), 409)
    status = db.one("SELECT status FROM serial_numbers WHERE id = ?",
                    (serial["id"],))["status"]
    return _ok(serial_number=serial["serial_number"], decision=decision,
               result=result, serial_status=status)


@bp.route("/events/trace", methods=["POST"])
@machine_key_required
def machine_trace():
    body = _json_body()
    serial_number = (_get_first(body, "serial_number", "Serial Number", "sn", "serial") or "").strip()
    if not serial_number:
        return _err("serial_number (Serial Number) is required.")
    st = g.api_station
    station_id = st["station_id"]
    line_id = (_get_first(body, "line_id", "Line ID", "line", "line_code") or "").strip()
    station_id_in = (_get_first(body, "station_id", "Station ID", "station_code") or "").strip()
    stage_code = (_get_first(body, "stage_code", "stage code", "Stage Code", "stage") or "").strip()
    status_in = (_get_first(body, "status", "Status", "result", "Result", "decision", "Decision") or "").strip()
    operator_id_in = (_get_first(body, "operator_id", "operator id", "operator", "Operator", "op_id") or "").strip()
    fixture_id = (_get_first(body, "fixture_id", "fixture id", "fixture", "Fixture") or "").strip() or None
    sw_version = (_get_first(body, "sw_version", "SW version", "software_version", "sw") or "").strip() or None
    hw_version = (_get_first(body, "hw_version", "HW version", "hardware_version", "hw") or "").strip() or None
    cycle_time_raw = _get_first(body, "cycle_time", "cycle time", "Cycle Time", "cycletime")
    cycle_time = None
    try:
        if cycle_time_raw is not None and str(cycle_time_raw).strip() != "":
            cycle_time = float(str(cycle_time_raw).replace(",", "."))
    except Exception:
        cycle_time = None
    if station_id_in and station_id_in != st["station_code"]:
        return _err(f"Station ID mismatch: expected {st['station_code']} got {station_id_in}.", 403)
    from services.validation_service import ValidationService
    from services.transaction_service import TransactionService
    val = ValidationService()
    res = val.validate_serial(station_id, serial_number)
    if not res.ok:
        TransactionService().log_exception(
            operator_id=g.api_machine_user, station_id=station_id, serial=None,
            expected="valid serial at station", scanned=serial_number, error=res.error)
        return _err(res.error)
    serial = res.data["serial"]
    params = _get_params(body)
    bindings = _get_bindings(body)
    txs = TransactionService()
    if status_in.upper() in ("PASS", "FAIL"):
        decision = status_in.upper()
        fail_status = (_get_first(body, "fail_status", "fail status") or "").upper() or None
        reason = (_get_first(body, "reason", "reason", "defect") or "").strip() or None
        try:
            res_qc = txs.record_qc_enhanced(
                operator_id=g.api_machine_user, station_id=station_id, serial=serial,
                decision=decision, fail_status=fail_status, reason=reason,
                fixture_id=fixture_id, sw_version=sw_version, hw_version=hw_version,
                cycle_time=cycle_time, line_code=line_id or st.get("line_code"),
                stage_code=stage_code or st.get("stage_code"), operator_code=operator_id_in or None,
                source="MACHINE", parameters=params, bindings=bindings)
            qc_dec, qc_id = res_qc if isinstance(res_qc, tuple) else (res_qc, None)
        except ValueError as exc:
            return _err(str(exc), 409)
        except Exception:
            qc_dec = txs.record_qc(operator_id=g.api_machine_user, station_id=station_id, serial=serial,
                                   decision=decision, fail_status=fail_status, reason=reason)
            if params:
                try:
                    txs._save_params(serial, station_id, None, None, params,
                                    line_id or st.get("line_code"), stage_code or st.get("stage_code"))
                except Exception:
                    pass
            qc_id = None
        status_cur = db.one("SELECT status FROM serial_numbers WHERE id = ?", (serial["id"],))["status"]
        return _ok(event="qc", serial_number=serial["serial_number"], station_code=st["station_code"],
                   stage_code=stage_code or st.get("stage_code"), line_code=line_id or st.get("line_code"),
                   decision=qc_dec, serial_status=status_cur, parameters_count=len(params),
                   bindings_count=len(bindings), qc_result_id=qc_id)
    pending = res.data.get("pending") or val.pending_materials(station_id, serial["id"])
    if val.trace_reported(serial["id"], station_id):
        return _err(f"Serial '{serial['serial_number']}' has already been processed at "
                    f"{st['station_code']}. It cannot be scanned at the same station again.", 409)
    material = None
    barcode = (_get_first(body, "material_barcode", "barcode", "Barcode") or "").strip()
    if pending and barcode:
        res2 = val.validate_material(station_id, serial["id"], barcode, pending)
        if res2.ok:
            material = res2.data["material"]
    try:
        tx_id = txs.record_machine_trace(
            operator_id=g.api_machine_user, station_id=station_id, serial=serial, material=material,
            barcode=barcode, fixture_id=fixture_id, sw_version=sw_version, hw_version=hw_version,
            cycle_time=cycle_time, line_code=line_id or st.get("line_code"),
            stage_code=stage_code or st.get("stage_code"), operator_code=operator_id_in or None,
            source="MACHINE", parameters=params, bindings=bindings)
    except Exception:
        tx_id = None
        if params:
            try:
                txs._save_params(serial, station_id, None, None, params,
                                line_id or st.get("line_code"), stage_code or st.get("stage_code"))
            except Exception:
                pass
    status_cur = db.one("SELECT status FROM serial_numbers WHERE id = ?", (serial["id"],))["status"]
    return _ok(event="trace", transaction_id=tx_id, serial_number=serial["serial_number"],
               station_code=st["station_code"], stage_code=stage_code or st.get("stage_code"),
               line_code=line_id or st.get("line_code"), serial_status=status_cur,
               parameters_count=len(params), bindings_count=len(bindings))


@bp.route("/events/downtime", methods=["POST"])
@machine_key_required
def downtime_event():
    """Machine-driven stoppage: action=start|end, optional reason_code + notes."""
    body = _json_body()
    action = (body.get("action") or "").lower()
    station_id = g.api_station["station_id"]
    svc = DowntimeService()
    if action == "start":
        reason_id = None
        code = (body.get("reason_code") or "").strip().upper()
        if code:
            r = db.one("SELECT id FROM downtime_reasons WHERE code = ? AND active = 1", (code,))
            if not r:
                return _err(f"Unknown downtime reason_code '{code}'. "
                            "GET /api/v1/downtime/reasons for the list.")
            reason_id = r["id"]
        ok, payload = svc.start(station_id, reason_id, g.api_machine_user,
                                notes=body.get("notes", ""))
        if not ok:
            return _err("Station already has an open stoppage.", 409)
        return _ok(state="DOWN", reason_code=code or None)
    if action == "end":
        ok, msg = svc.end_for_station(station_id, g.api_machine_user)
        return _ok(state="RUNNING", message=msg)
    return _err("action must be 'start' or 'end'.")


@bp.route("/events/ncr", methods=["POST"])
@machine_key_required
def ncr_event():
    """Raise a non-conformance from the machine."""
    body = _json_body()
    defect_reason = (body.get("defect_reason") or "").strip()
    if not defect_reason:
        return _err("defect_reason is required.")
    ok, payload = NCRService().open_ncr(
        source="MANUAL", defect_reason=defect_reason,
        description=(body.get("description") or "").strip() or None,
        serial_number=(body.get("serial_number") or "").strip() or None,
        station_id=g.api_station["station_id"], operator_id=g.api_machine_user)
    if not ok:
        return _err(payload, 409)
    return _ok(ncr_no=payload["ncr_no"], disposition=payload["disposition"])


@bp.route("/events/alert", methods=["POST"])
@machine_key_required
def alert_event():
    """Push an alarm/alert into the MES feed (bell + Andon ticker)."""
    body = _json_body()
    message = (body.get("message") or "").strip()
    if not message:
        return _err("message is required.")
    level = (body.get("level") or "WARN").upper()
    nid = notify(level, "MACHINE", message, station_id=g.api_station["station_id"],
                 dedupe_minutes=0)
    return _ok(alert_id=nid, level=level)


# ------------------------------------------------------------------ queries
@bp.route("/stations/me")
@machine_key_required
def stations_me():
    """Everything the calling machine needs: profile, work assignments, state."""
    st = g.api_station
    assignments = db.query(
        """SELECT mo.mo_number, m.material_code, m.material_description,
                  a.barcode_rule, m.barcode_rule AS material_rule
           FROM material_assignment a
           JOIN manufacturing_orders mo ON mo.id = a.mo_id
           JOIN materials m ON m.id = a.material_id
           WHERE a.station_id = ? AND a.active = 1 AND m.active = 1
             AND mo.status = 'RELEASED' AND mo.active = 1
           ORDER BY mo.mo_number, a.sort_order, m.material_code""",
        (st["station_id"],))
    by_mo = {}
    for a in assignments:
        by_mo.setdefault(a["mo_number"], []).append({
            "material_code": a["material_code"],
            "material_description": a["material_description"],
            "barcode_rule": a["barcode_rule"] or a["material_rule"]})
    state, open_ev = _station_state(st["station_id"])
    last = db.query(
        """SELECT t.timestamp, sn.serial_number, m.material_code
           FROM material_transactions t
           JOIN serial_numbers sn ON sn.id = t.serial_id
           JOIN materials m ON m.id = t.material_id
           WHERE t.station_id = ? ORDER BY t.id DESC LIMIT 5""",
        (st["station_id"],))
    work = db.query(
        """SELECT DISTINCT sn.serial_number, sn.status
           FROM serial_numbers sn
           JOIN manufacturing_orders mo ON mo.id = sn.mo_id
           WHERE mo.active = 1 AND mo.status = 'RELEASED' AND sn.status = 'IN_PROCESS'
             AND (EXISTS (SELECT 1 FROM material_assignment a
                          WHERE a.mo_id = mo.id AND a.station_id = ? AND a.active = 1)
                  OR ? = 1)
           ORDER BY sn.id LIMIT 50""",
        (st["station_id"], 1 if ValidationService().is_qc_station(st) else 0))
    return _ok(
        station={"station_code": st["station_code"], "station_name": st["station_name"],
                 "station_type": st["station_type"], "station_id": st["station_id"],
                 "stage_code": st.get("stage_code"), "halted": bool(st.get("halted"))},
        state=state,
        open_stoppage=(None if not open_ev else {
            "reason": open_ev.get("reason_label"), "notes": open_ev.get("notes")}),
        materials_by_mo=by_mo,
        work_queue=[{"serial_number": w["serial_number"], "status": w["status"]}
                    for w in work],
        last_scans=[{"timestamp": _iso(r["timestamp"]), "serial_number": r["serial_number"],
                     "material_code": r["material_code"]} for r in last],
    )


@bp.route("/serials/<serial_number>")
@machine_key_required
def serial_detail(serial_number):
    genealogy = GenealogyService().serial_genealogy(serial_number)
    if not genealogy:
        return _err(f"Serial '{serial_number}' not found.", 404)
    station_id = g.api_station["station_id"]
    pending, qc_state, qc_retest = [], None, None
    if ValidationService().is_qc_station(g.api_station):
        qc_state = ValidationService().qc_result(genealogy["serial"]["id"], station_id)
        qc_retest = ValidationService().qc_retest_state(genealogy["serial"]["id"], station_id)
    else:
        pending = [{"material_code": p["material_code"],
                    "material_description": p["material_description"],
                    "barcode_rule": p["barcode_rule"] or p["material_rule"]}
                   for p in ValidationService().pending_materials(
                       station_id, genealogy["serial"]["id"])]
    last_tx = genealogy["materials"][-1] if genealogy["materials"] else None
    return _ok(
        serial={"serial_number": genealogy["serial"]["serial_number"],
                "status": genealogy["serial"]["status"],
                "mo_number": genealogy["serial"]["mo_number"],
                "created_at": _iso(genealogy["serial"]["created_at"])},
        route=[{"station_code": s["station_code"], "station_type": s["station_type"],
                "done": bool(s["visited"])} for s in genealogy["route"]],
        pending_at_calling_station=pending,
        qc_at_calling_station=qc_state,
        qc_retest=qc_retest,
        last_scan=(None if not last_tx else {
            "timestamp": _iso(last_tx["timestamp"]),
            "station_code": last_tx["station_code"],
            "material_code": last_tx["material_code"]}),
        qc_fails=[{"timestamp": _iso(q["timestamp"]), "station_code": q["station_code"],
                   "fail_status": q["fail_status"], "reason": q["reason"]}
                  for q in genealogy["quality"] if q["decision"] == "FAIL"],
        open_ncrs=[{"ncr_no": n["ncr_no"], "defect_reason": n["defect_reason"],
                    "disposition": n["disposition"]} for n in genealogy["ncrs"]
                   if n["disposition"] == "OPEN"],
    )


@bp.route("/mos")
@machine_key_required
def mos():
    rows = db.query(
        """SELECT mo.id, mo.mo_number, mo.description, mo.planned_qty, mo.status,
                  (SELECT COUNT(*) FROM serial_numbers s WHERE s.mo_id = mo.id) AS serials,
                  (SELECT COUNT(*) FROM serial_numbers s WHERE s.mo_id = mo.id
                     AND s.status = 'COMPLETED') AS completed
           FROM manufacturing_orders mo WHERE mo.active = 1 ORDER BY mo.id""")
    return _ok(mos=[{k: r[k] for k in ("id", "mo_number", "description", "planned_qty",
                                       "status", "serials", "completed")} for r in rows])


@bp.route("/materials")
@machine_key_required
def materials():
    rows = db.query("SELECT material_code, material_description, barcode_rule, active "
                    "FROM materials ORDER BY material_code")
    return _ok(materials=[dict(r) for r in rows])


@bp.route("/downtime/reasons")
@machine_key_required
def downtime_reasons():
    rows = DowntimeService().reasons(active_only=True)
    return _ok(reasons=[{"code": r["code"], "label": r["label"]} for r in rows])


@bp.route("/notifications")
@machine_key_required
def notifications():
    rows = notif_recent(20)
    return _ok(unread=unread_count(),
               items=[{"id": n["id"], "level": n["level"], "category": n["category"],
                       "message": n["message"], "station_code": n["station_code"],
                       "created_at": _iso(n["created_at"]), "read_at": _iso(n["read_at"])}
                      for n in rows])


def register(app):
    app.register_blueprint(bp)

    @app.route("/api/docs")
    def api_docs():
        return render_template("api_docs.html")
