"""
Integration REST API (keyless, LAN-only) — for local line/machine integration.

This is the OPEN counterpart to the API-key protected /api/v1 machine API.
It is meant to be called by machines, PLC gateways and line controllers on the
same trusted LAN, so it does NOT require an API key. Identity (which station is
calling) is taken from the `station_code` supplied in the request.

Endpoints:
  1. GET  /api/integration/health
  2. GET  /api/integration/mos/<mo_number>              (get MO number)
  3. GET  /api/integration/mos/<mo_number>/serials      (serial batch vs MO)
  4. GET  /api/integration/serials/<serial_number>      (unit status)
  5. POST /api/integration/process-status               (update process status)

JSON is returned for every endpoint. A machine-readable spec is available at
GET /api/integration/openapi.json.
"""
import json
from datetime import datetime

from flask import Blueprint, jsonify, request

from database.db import db
from services.genealogy_service import GenealogyService
from services.transaction_service import TransactionService
from services.validation_service import ValidationService

INTEGRATION_VERSION = "1.0.0"
bp = Blueprint("integration_api", __name__, url_prefix="/api/integration")


# ------------------------------------------------------------------ helpers
def _ok(**payload):
    return jsonify({"ok": True, **payload})


def _err(message, status=400):
    return jsonify({"ok": False, "error": message}), status


def _iso(value):
    return str(value)[:19] if value else None


def _body():
    data = request.get_json(silent=True)
    if data is None:
        data = request.form.to_dict()
    return data if isinstance(data, dict) else {}


def _first(d, *names):
    """Look up the first present non-empty value, tolerant to key casing/spaces."""
    if not isinstance(d, dict):
        return None
    for n in names:
        if n in d and d[n] not in (None, ""):
            return d[n]
    norm = {str(k).strip().lower().replace(" ", "_").replace("-", "_"): v
            for k, v in d.items()}
    for n in names:
        nk = n.strip().lower().replace(" ", "_").replace("-", "_")
        if nk in norm and norm[nk] not in (None, ""):
            return norm[nk]
    return None


def _station(station_code):
    if not station_code:
        return None
    return db.one(
        """SELECT id, station_code, station_name, station_type, stage_code, line_code,
                  machine_id, COALESCE(halted, 0) AS halted, active
           FROM stations WHERE station_code = ? COLLATE NOCASE""",
        (str(station_code).strip(),))


def _station_payload(st):
    return {"station_code": st["station_code"], "station_name": st["station_name"],
            "station_type": st["station_type"], "stage_code": st["stage_code"],
            "line_code": st["line_code"], "machine_id": st["machine_id"],
            "halted": bool(st["halted"])}


def _machine_user_id():
    row = db.one("SELECT id FROM users WHERE username = 'machine'")
    if row:
        return row["id"]
    import secrets
    from auth.authentication import Authentication
    Authentication().create_user("machine", secrets.token_urlsafe(32),
                                 "Machine API (system)", "OPERATOR")
    return db.one("SELECT id FROM users WHERE username = 'machine'")["id"]


# ------------------------------------------------------------------ 1. health
@bp.route("/health", methods=["GET"])
def health():
    """1 - Health check: is the MES server alive and its store reachable."""
    db_ok = True
    try:
        db.one("SELECT 1")
    except Exception:
        db_ok = False
    return _ok(service="mts-integration-api", version=INTEGRATION_VERSION,
               server_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               database=("UP" if db_ok else "DOWN"))


@bp.route("/openapi.json", methods=["GET"])
def openapi():
    base = request.host_url.rstrip("/")
    return jsonify({
        "openapi": "3.0.3",
        "info": {"title": "MTS Integration API", "version": INTEGRATION_VERSION,
                 "description": "Keyless LAN integration API for machines and lines."},
        "servers": [{"url": base}],
        "paths": {
            "/api/integration/health": {"get": {"summary": "Health check"}},
            "/api/integration/mos/{mo_number}": {"get": {"summary": "Get MO number"}},
            "/api/integration/mos/{mo_number}/serials": {"get": {"summary": "Get serial numbers as batch against MO"}},
            "/api/integration/serials/{serial_number}": {"get": {"summary": "Get unit status"}},
            "/api/integration/process-status": {"post": {"summary": "Update process status from machines"}},
        },
    })


# ------------------------------------------------------------------ 2. get MO
@bp.route("/mos/<mo_number>", methods=["GET"])
def get_mo(mo_number):
    """2 - Get a manufacturing order by its MO number."""
    row = db.one(
        """SELECT mo.id, mo.mo_number, mo.description, mo.planned_qty, mo.status,
                  mo.active, mo.created_at, pr.product_code, pr.product_name,
                  (SELECT COUNT(*) FROM serial_numbers s WHERE s.mo_id = mo.id) AS serials,
                  (SELECT COUNT(*) FROM serial_numbers s WHERE s.mo_id = mo.id
                     AND s.status = 'COMPLETED') AS completed
           FROM manufacturing_orders mo
           LEFT JOIN products pr ON pr.id = mo.product_id
           WHERE mo.mo_number = ? COLLATE NOCASE""",
        (str(mo_number).strip(),))
    if not row:
        return _err(f"MO '{mo_number}' not found.", 404)
    return _ok(mo={
        "id": row["id"], "mo_number": row["mo_number"], "description": row["description"],
        "product_code": row["product_code"], "product_name": row["product_name"],
        "planned_qty": row["planned_qty"], "status": row["status"],
        "active": bool(row["active"]), "serials": row["serials"],
        "completed": row["completed"], "created_at": _iso(row["created_at"]),
    })


# ------------------------------------------------------------------ 3. serials vs MO
@bp.route("/mos/<mo_number>/serials", methods=["GET"])
def get_mo_serials(mo_number):
    """3 - Get the serial numbers (batch) belonging to an MO.

    Query params: status (optional), limit (default 200, max 1000), offset (0).
    """
    mo = db.one("SELECT id, mo_number, status FROM manufacturing_orders "
                "WHERE mo_number = ? COLLATE NOCASE", (str(mo_number).strip(),))
    if not mo:
        return _err(f"MO '{mo_number}' not found.", 404)

    status = (request.args.get("status") or "").strip().upper()
    try:
        limit = min(max(int(request.args.get("limit", 200)), 1), 1000)
    except (TypeError, ValueError):
        limit = 200
    try:
        offset = max(int(request.args.get("offset", 0)), 0)
    except (TypeError, ValueError):
        offset = 0

    where = "WHERE sn.mo_id = ?"
    params = [mo["id"]]
    if status:
        where += " AND sn.status = ?"
        params.append(status)

    total = db.one(f"SELECT COUNT(*) AS n FROM serial_numbers sn {where}", tuple(params))["n"]
    rows = db.query(
        f"""SELECT sn.id, sn.serial_number, sn.status, sn.created_at,
                   (SELECT t.station_id FROM material_transactions t
                      WHERE t.serial_id = sn.id ORDER BY t.id DESC LIMIT 1) AS last_station_id,
                   (SELECT t.timestamp FROM material_transactions t
                      WHERE t.serial_id = sn.id ORDER BY t.id DESC LIMIT 1) AS last_at
            FROM serial_numbers sn {where}
            ORDER BY sn.id LIMIT ? OFFSET ?""",
        tuple(params) + (limit, offset))
    stmap = {s["id"]: s["station_code"]
             for s in db.query("SELECT id, station_code FROM stations")}
    return _ok(mo={"mo_number": mo["mo_number"], "status": mo["status"]},
               total=total, limit=limit, offset=offset,
               serials=[{
                   "id": r["id"], "serial_number": r["serial_number"], "status": r["status"],
                   "created_at": _iso(r["created_at"]),
                   "last_station": stmap.get(r["last_station_id"]),
                   "last_activity": _iso(r["last_at"]),
               } for r in rows])


# ------------------------------------------------------------------ 4. unit status
@bp.route("/serials/<serial_number>", methods=["GET"])
def get_unit_status(serial_number):
    """4 - Get one unit's full status (serial, MO, route progress, QC, NCR).

    Optional query param: station_code — when given, route progress is evaluated
    for that station and pending materials are computed for it.
    """
    genealogy = GenealogyService().serial_genealogy(serial_number)
    if not genealogy:
        return _err(f"Serial '{serial_number}' not found.", 404)
    serial = genealogy["serial"]

    station_code = (request.args.get("station_code") or "").strip()
    pending, qc_state, qc_retest = [], None, None
    st = _station(station_code) if station_code else None
    if st:
        val = ValidationService()
        if val.is_qc_station(st):
            qc_state = val.qc_result(serial["id"], st["id"])
            qc_retest = val.qc_retest_state(serial["id"], st["id"])
        else:
            pending = [{"material_code": p["material_code"],
                        "material_description": p["material_description"]}
                       for p in val.pending_materials(st["id"], serial["id"])]

    last_tx = genealogy["materials"][-1] if genealogy["materials"] else None
    return _ok(unit={
        "serial_number": serial["serial_number"],
        "status": serial["status"],
        "mo_number": serial["mo_number"],
        "created_at": _iso(serial["created_at"]),
    },
        route=[{"station_code": s["station_code"], "station_type": s["station_type"],
                "done": bool(s["visited"])} for s in genealogy["route"]],
        route_complete=all(bool(s["visited"]) for s in genealogy["route"]) if genealogy["route"] else False,
        child_parts=[{"serial_number": c["child_serial_number"],
                      "material_code": c["child_material_code"],
                      "status": c["child_status"],
                      "qty": c["qty"], "station_code": c["station_code"],
                      "line_code": c["line_code"], "stage_code": c["stage_code"],
                      "bound_at": _iso(c["timestamp"])}
                     for c in genealogy.get("children", [])],
        station=station_code or None,
        pending_at_station=pending,
        qc_at_station=qc_state,
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
                   if n["disposition"] == "OPEN"])


# ------------------------------------------------------------------ 5. update process status
@bp.route("/process-status", methods=["POST"])
def update_process_status():
    """5 - Update process status reported by a machine for a unit.

    Body (JSON or form):
      Required: serial_number, station_code, status
      status values:
        PASS / OK       -> QC pass at the calling (QC) station
        FAIL            -> QC fail (fail_status HOLD/REJECTED, optional reason)
        IN_PROCESS      -> machine trace / in-progress record (default for non-QC)
      Optional: stage_code, line_code, operator_code, fixture_id, sw_version,
                hw_version, cycle_time, parameters[] (Key/value/unit/LSL/USL),
                material_barcode, fail_status, reason
    """
    body = _body()
    serial_number = _first(body, "serial_number", "Serial Number", "Serial No", "serial no",
                           "sn", "serial")
    serial_number = str(serial_number).strip() if serial_number is not None else ""
    station_code = _first(body, "station_code", "Station Code", "Station ID", "station id",
                          "station")
    station_code = str(station_code).strip() if station_code is not None else ""
    status_in = _first(body, "status", "Status", "result", "Result", "decision", "Decision")
    status_in = str(status_in).strip().upper() if status_in is not None else ""

    if not serial_number:
        return _err("serial_number is required.")
    if not station_code:
        return _err("station_code is required.")
    if not status_in:
        return _err("status is required (PASS | FAIL | IN_PROCESS).")

    st = _station(station_code)
    if not st:
        return _err(f"Unknown station_code '{station_code}'.", 404)
    if not st["active"]:
        return _err(f"Station {st['station_code']} is inactive.", 403)

    val = ValidationService()
    res = val.validate_serial(st["id"], serial_number)
    if not res.ok:
        TransactionService().log_exception(
            operator_id=_machine_user_id(), station_id=st["id"], serial=None,
            expected="valid serial at station", scanned=serial_number, error=res.error)
        return _err(res.error)
    serial = res.data["serial"]

    stage_code = (_first(body, "stage_code", "Stage Code", "stage") or st["stage_code"])
    line_code = (_first(body, "line_code", "Line ID", "Line Code", "line") or st["line_code"])
    operator_code = _first(body, "operator_code", "operator_id", "operator", "Operator")
    fixture_id = _first(body, "fixture_id", "fixture")
    sw_version = _first(body, "sw_version", "software_version")
    hw_version = _first(body, "hw_version", "hardware_version")
    parameters = _params(body)
    bindings = _bindings(body)
    cycle_time = None
    ct = _first(body, "cycle_time", "Cycle Time", "cycletime")
    try:
        if ct is not None and str(ct).strip() != "":
            cycle_time = float(str(ct).replace(",", "."))
    except Exception:
        cycle_time = None

    txs = TransactionService()
    machine_user = _machine_user_id()
    is_qc = val.is_qc_station(st)

    if status_in in ("PASS", "FAIL") and is_qc:
        fail_status = (_first(body, "fail_status", "fail status") or "").strip().upper() or None
        reason = (_first(body, "reason", "defect") or "").strip() or None
        try:
            qc_dec, qc_id = txs.record_qc_enhanced(
                operator_id=machine_user, station_id=st["id"], serial=serial,
                decision=status_in, fail_status=fail_status, reason=reason,
                fixture_id=fixture_id, sw_version=sw_version, hw_version=hw_version,
                cycle_time=cycle_time, line_code=line_code, stage_code=stage_code,
                operator_code=operator_code, source="MACHINE", parameters=parameters,
                bindings=bindings)
        except ValueError as exc:
            return _err(str(exc), 409)
        cur = db.one("SELECT status FROM serial_numbers WHERE id = ?", (serial["id"],))["status"]
        return _ok(event="qc", serial_number=serial["serial_number"],
                   station_code=st["station_code"], stage_code=stage_code,
                   line_code=line_code, decision=qc_dec, serial_status=cur,
                   parameters_count=len(parameters), bindings_count=len(bindings),
                   qc_result_id=qc_id)

    # Production (work) station. IN_PROCESS / PASS record a production trace;
    # FAIL records a failed trace and puts the unit on HOLD (or REJECTED when
    # fail_status=REJECTED) so it cannot continue.
    is_fail = status_in == "FAIL"

    # Idempotency: a serial already processed at this station must not be
    # processed here a second time (repeat trace / re-scan blocked).
    if val.trace_reported(serial["id"], st["id"]):
        return _err(f"Serial '{serial['serial_number']}' has already been processed at "
                    f"{st['station_code']}. It cannot be scanned at the same station again.", 409)

    material = None
    barcode = (_first(body, "material_barcode", "barcode", "Barcode") or "").strip()
    pending = res.data.get("pending") or val.pending_materials(st["id"], serial["id"])
    if pending and barcode:
        res2 = val.validate_material(st["id"], serial["id"], barcode, pending)
        if res2.ok:
            material = res2.data["material"]

    # Child parts: each carries a part/material barcode (dynamic-key form). Any
    # child whose barcode matches a material assigned at this station is consumed
    # as a material transaction; every child is also recorded as a unit binding.
    consumed, unmatched = [], []
    for b in bindings:
        child_bc = (b.get("barcode") or b.get("serial_number") or "").strip()
        pend_now = val.pending_materials(st["id"], serial["id"])
        mres = val.validate_material(st["id"], serial["id"], child_bc, pend_now) if pend_now else None
        if mres and mres.ok:
            m = mres.data["material"]
            txs.record_material(operator_id=machine_user, station_id=st["id"], serial=serial,
                                material=m, barcode=child_bc, parsed=mres.data.get("parsed"))
            b["material_code"] = b.get("material_code") or m["material_code"]
            consumed.append(m["material_code"])
        else:
            unmatched.append(child_bc)

    fail_status = None
    if is_fail:
        fail_status = (_first(body, "fail_status", "fail status") or "").strip().upper() or None
        if fail_status not in ("HOLD", "REJECTED"):
            fail_status = "HOLD"
        db.execute("UPDATE serial_numbers SET status = ? WHERE id = ?",
                   (fail_status, serial["id"]))

    tx_id = txs.record_machine_trace(
        operator_id=machine_user, station_id=st["id"], serial=serial, material=material,
        barcode=barcode, fixture_id=fixture_id, sw_version=sw_version, hw_version=hw_version,
        cycle_time=cycle_time, line_code=line_code, stage_code=stage_code,
        operator_code=operator_code, source="MACHINE", parameters=parameters,
        bindings=bindings, result=(status_in or "IN_PROCESS"))
    cur = db.one("SELECT status FROM serial_numbers WHERE id = ?", (serial["id"],))["status"]
    return _ok(event="trace", result=(status_in or "IN_PROCESS"), transaction_id=tx_id,
               serial_number=serial["serial_number"], station_code=st["station_code"],
               stage_code=stage_code, line_code=line_code, serial_status=cur,
               fail_status=fail_status, parameters_count=len(parameters),
               bindings_count=len(bindings),
               child_parts_consumed=consumed, child_parts_unmatched=unmatched)


def _params(body):
    for n in ("parameters", "measurements", "params", "process_parameters", "data", "values", "results"):
        v = _first(body, n)
        if isinstance(v, list):
            return v
    return []


def _bindings(body):
    """Child parts installed into the parent unit.

    Accepts several shapes:
      * ``childParts`` / ``child_parts`` / ``children`` / ``components`` list;
      * a plain list of child serial strings, e.g. ``["SN-2", "SN-3"]``;
      * objects with ``serial_number`` / ``Serial Number`` / ``child_serial``;
      * objects with a DYNAMIC key carrying a part barcode, e.g.
        ``{"PartBarcode1": "MAT-06"}`` — the value is the part/material barcode.

    Each entry resolves to ``{serial_number, material_code, barcode, qty}``.
    """
    if not isinstance(body, dict):
        return []
    raw = None
    for n in ("childParts", "childparts", "child_parts", "children", "components",
              "Child Parts", "child serial numbers", "child_serials"):
        v = _first(body, n)
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
        sn = _first(item, "serial_number", "Serial Number", "child_serial",
                    "child_serial_number", "child", "sn", "SN")
        material_code = _first(item, "material_code", "material", "Material Code",
                               "Material", "Part Code", "PartCode")
        barcode = _first(item, "barcode", "Barcode", "child_barcode",
                         "Part Barcode", "PartBarcode")
        # Dynamic-key form: {"PartBarcode1": "MAT-06"} -> value is the part barcode
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
            "qty": _first(item, "qty", "quantity", "Qty") or 1,
        })
    return out


# ==================================================================
# Machine integration API (keyless, LAN-only) — /api/v1
# Implements the machine/edge-gateway contract:
#   1. POST /api/v1/heartbeat
#   2. GET  /api/v1/orders/<moNumber>/serials
#   3. GET  /api/v1/orders/<moNumber>/product
#   4. GET  /api/v1/units/<serialNumber>/status
#   5. GET  /api/v1/units/<serialNumber>/routing-validation
#   6. GET  /api/v1/orders/active?lineId=&productCode=
# Responses follow the documented shapes (no "ok" envelope).
# ==================================================================
mapi = Blueprint("machine_integration", __name__, url_prefix="/api/v1")


def _now_iso():
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _date_only(value):
    return str(value)[:10] if value else None


def _mo_by_number(mo_number):
    return db.one(
        """SELECT mo.*, p.product_code, p.product_name, p.revision
           FROM manufacturing_orders mo
           LEFT JOIN products p ON p.id = mo.product_id
           WHERE mo.mo_number = ? COLLATE NOCASE""",
        (str(mo_number).strip(),))


def _unit_row(serial_number):
    return db.one(
        """SELECT s.id, s.serial_number, s.status, s.created_at,
                  mo.mo_number, mo.id AS mo_id,
                  p.product_code, p.product_name, p.revision
           FROM serial_numbers s
           JOIN manufacturing_orders mo ON mo.id = s.mo_id
           LEFT JOIN products p ON p.id = mo.product_id
           WHERE s.serial_number = ? COLLATE NOCASE""",
        (str(serial_number).strip(),))


# ------------------------------------------------------------------ 1. heartbeat
@mapi.route("/heartbeat", methods=["POST", "GET"])
def machine_heartbeat():
    """Machine/edge-gateway health check. Records the caller as alive (when a
    known station_code is supplied) and acknowledges with the server time."""
    body = _body() if request.method == "POST" else {}
    station_code = (_first(body, "station_code", "Station ID", "stationId")
                    or request.args.get("station_code") or "").strip()
    station = _station(station_code) if station_code else None
    if station:
        db.execute("UPDATE station_comm SET api_last_seen = CURRENT_TIMESTAMP "
                   "WHERE station_id = ?", (station["id"],))
    return jsonify({
        "status": "ACK",
        "serverTime": _now_iso(),
        "service": "mts-machine-api",
        "database": "UP",
        "stationCode": station["station_code"] if station else (station_code or None),
    })


# ------------------------------------------------------------------ 2. serials vs MO
@mapi.route("/orders/<mo_number>/serials")
def order_serials(mo_number):
    """3? (2) - List the serial numbers generated for a manufacturing order."""
    mo = _mo_by_number(mo_number)
    if not mo:
        return jsonify({"error": f"MO '{mo_number}' not found."}), 404
    rows = db.query(
        """SELECT serial_number, status FROM serial_numbers
           WHERE mo_id = ? ORDER BY id""", (mo["id"],))
    return jsonify({
        "moNumber": mo["mo_number"],
        "totalSerials": len(rows),
        "serials": [{"serialNumber": r["serial_number"], "status": r["status"]} for r in rows],
    })


# ------------------------------------------------------------------ 3. product vs MO
@mapi.route("/orders/<mo_number>/product")
def order_product(mo_number):
    """3 - Product master (code/name/revision) linked to the MO."""
    mo = _mo_by_number(mo_number)
    if not mo:
        return jsonify({"error": f"MO '{mo_number}' not found."}), 404
    return jsonify({
        "moNumber": mo["mo_number"],
        "productCode": mo["product_code"],
        "productName": mo["product_name"],
        "revision": mo["revision"],
    })


# ------------------------------------------------------------------ 4. unit status
@mapi.route("/units/<serial_number>/status")
def unit_status(serial_number):
    """4 - Lifecycle state of a physical unit before a machine touches it."""
    row = _unit_row(serial_number)
    if not row:
        return jsonify({"error": f"Serial '{serial_number}' not found."}), 404
    status = row["status"]
    return jsonify({
        "serialNumber": row["serial_number"],
        "moNumber": row["mo_number"],
        "productCode": row["product_code"],
        "currentStatus": status,
        "isScrapped": status == "REJECTED",
        "isRework": status == "HOLD",
    })


# ------------------------------------------------------------------ 5. routing validation
@mapi.route("/units/<serial_number>/routing-validation")
def unit_routing_validation(serial_number):
    """5 - Interlocking check: has the unit passed the previous routing step?
    Read-only (does NOT halt the station) — machines call this before opening
    fixtures to confirm the unit is at the expected point in the route."""
    row = _unit_row(serial_number)
    if not row:
        return jsonify({"error": f"Serial '{serial_number}' not found."}), 404
    serial_id = row["id"]
    mo_id = row["mo_id"]
    val = ValidationService()
    route = val.route_stations(mo_id)
    done_flags = [val.station_done(serial_id, s["id"], mo_id) for s in route]

    station_code = (request.args.get("station_code") or "").strip()
    caller = _station(station_code) if station_code else None

    prev_station = None
    next_station = None
    allowed = True

    def _station_result(station_id, station_code_):
        qc = db.one(
            """SELECT decision, timestamp FROM quality_checks
               WHERE serial_id=? AND station_id=? ORDER BY id DESC LIMIT 1""",
            (serial_id, station_id))
        if qc:
            return {"stationId": station_code_, "status": qc["decision"],
                    "timestamp": _iso(qc["timestamp"])}
        tx = db.one(
            """SELECT result, timestamp FROM material_transactions
               WHERE serial_id=? AND station_id=? ORDER BY id DESC LIMIT 1""",
            (serial_id, station_id))
        if tx:
            return {"stationId": station_code_, "status": (tx["result"] or "PASSED"),
                    "timestamp": _iso(tx["timestamp"])}
        return {"stationId": station_code_, "status": None, "timestamp": None}

    positions = {s["id"]: i for i, s in enumerate(route)}
    if caller and caller["id"] in positions:
        cur = positions[caller["id"]]
        # previous = last station before the caller that has any record
        for i in range(cur - 1, -1, -1):
            s = route[i]
            res = _station_result(s["id"], s["station_code"])
            if res["status"] is not None:
                prev_station = res
                break
        if cur + 1 < len(route):
            next_station = route[cur + 1]["station_code"]
        # allowed when every earlier stage is done and the caller is not already done
        allowed = all(done_flags[:cur]) and not done_flags[cur]
    elif caller:
        allowed = False  # caller is not part of this MO's route
        for i in range(len(route) - 1, -1, -1):
            if done_flags[i]:
                prev_station = _station_result(route[i]["id"], route[i]["station_code"])
                break
    else:
        for i in range(len(route) - 1, -1, -1):
            if done_flags[i]:
                prev_station = _station_result(route[i]["id"], route[i]["station_code"])
                break
        for s, done in zip(route, done_flags):
            if not done:
                next_station = s["station_code"]
                break

    return jsonify({
        "serialNumber": row["serial_number"],
        "allowedToProcess": allowed,
        "previousStation": prev_station,
        "nextExpectedStationId": next_station,
    })


# ------------------------------------------------------------------ 6. active orders
@mapi.route("/orders/active")
def active_orders():
    """6 - Find released/active MOs by master criteria (lineId / productCode)."""
    line_id = (request.args.get("lineId") or request.args.get("line_id") or "").strip()
    product_code = (request.args.get("productCode") or request.args.get("product_code") or "").strip()

    where = ["mo.active = 1", "UPPER(mo.status) IN ('RELEASED','IN_PROGRESS','ACTIVE')"]
    params = []
    if product_code:
        where.append("UPPER(p.product_code) = ?")
        params.append(product_code.upper())
    if line_id:
        where.append(
            """EXISTS (SELECT 1 FROM mo_route mr JOIN stations st ON st.id = mr.station_id
                       WHERE mr.mo_id = mo.id AND UPPER(st.line_code) = ?)""")
        params.append(line_id.upper())

    rows = db.query(
        f"""SELECT mo.mo_number, mo.status, mo.created_at, p.product_code
            FROM manufacturing_orders mo
            LEFT JOIN products p ON p.id = mo.product_id
            WHERE {' AND '.join(where)}
            ORDER BY mo.id""", tuple(params))
    return jsonify({
        "lineId": line_id or None,
        "activeOrders": [{
            "moNumber": r["mo_number"],
            "scheduleDate": _date_only(r["created_at"]),
            "priority": 1,
            "status": r["status"],
        } for r in rows],
    })


def register(app):
    app.register_blueprint(bp)
    app.register_blueprint(mapi)
