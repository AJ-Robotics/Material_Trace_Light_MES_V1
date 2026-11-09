"""
Material Tracking System - Flask app (modern-classic UI).
Reuses the same service layer / SQLite DB as the Streamlit version.
"""
import json
import os
import secrets
import sys
from datetime import datetime
from functools import wraps
from io import BytesIO
from pathlib import Path

from flask import (Flask, Response, abort, flash, jsonify, redirect, render_template,
                   request, send_file, session, url_for)

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import BACKUP_DIR, DATABASE_PATH  # noqa: E402
from database.db import db  # noqa: E402
from auth.authentication import Authentication  # noqa: E402
from services.admin_service import AdminService  # noqa: E402
from services.material_service import MaterialService  # noqa: E402
from services.serial_service import SerialService  # noqa: E402
from services.validation_service import ValidationService  # noqa: E402
from services.transaction_service import TransactionService  # noqa: E402
from services.system_config import ConfigService  # noqa: E402
from services import (barcode_service, dashboard_service, excel_service,  # noqa: E402
                      report_service, station_output, notification_service)
from services import netguard  # noqa: E402
from services.downtime_service import DowntimeService  # noqa: E402
from services.genealogy_service import GenealogyService  # noqa: E402
from services.ncr_service import NCRService  # noqa: E402
from services.product_service import ProductService  # noqa: E402
from services.mes_client import mes as mes_client  # noqa: E402
import machine_api  # noqa: E402
import integration_api  # noqa: E402

ADMIN_PAGES = [
    ("stations", "Stations"),
    ("station_types", "Station Types"),
    ("materials", "Materials"),
    ("products", "Products"),
    ("mos", "MO Management"),
    ("mo_master", "MO Master"),
    ("assignment", "Material Assignment"),
    ("serials", "Serial Numbers"),
    ("qc", "QC Checks"),
    ("parameters", "Parameters"),
    ("bulk", "Bulk Upload"),
    ("users", "Users & Allocation"),
    ("transactions", "Transactions"),
    ("exceptions", "Exceptions"),
    ("genealogy", "Genealogy & Recall"),
    ("downtime", "Downtime & OEE"),
    ("ncr", "NCR / Rework"),
    ("reports", "Reports"),
    ("api", "API"),
    ("configuration", "Configuration"),
    ("dbmaint", "Backup & Reset"),
]

# Pages VIEWER may open (everything else in ADMIN_PAGES is ADMIN-only).
VIEWER_PAGES = ("transactions", "exceptions", "genealogy", "downtime", "ncr", "reports", "parameters")

REPORT_TYPES = {
    "traceability": ("Traceability Ledger", report_service.traceability,
                     ["timestamp", "station", "station_type", "serial_number", "mo_number",
                      "material_code", "material_description", "barcode", "operator",
                      "operator_code", "result", "remarks", "transaction_id"]),
    "serial_ledger": ("Serial Ledger", report_service.serial_ledger,
                      ["serial_number", "mo_number", "status", "block", "scans", "qc_fails",
                       "first_activity", "last_activity"]),
    "station_summary": ("Station Summary", report_service.station_summary,
                        ["station_code", "station_name", "station_type", "route_order",
                         "scans", "serials", "operators", "passed"]),
    "qc_ledger": ("QC Audit Ledger", report_service.qc_ledger,
                  ["timestamp", "station", "serial_number", "mo_number", "operator",
                   "decision", "fail_status", "reason"]),
    "exception_ledger": ("Exception Ledger", report_service.exception_ledger,
                         ["timestamp", "serial_number", "mo_number", "station", "operator",
                          "error_type", "expected", "scanned", "error_message"]),
    "downtime_ledger": ("Downtime Ledger",
                        lambda f: report_service.downtime_ledger(f),
                        ["started_at", "ended_at", "minutes", "station", "reason_code",
                         "reason_label", "notes", "started_by", "ended_by", "mo_number"]),
    "ncr_ledger": ("NCR Ledger",
                   lambda f: report_service.ncr_ledger(f),
                   ["ncr_no", "opened_at", "closed_at", "source", "defect_reason",
                    "description", "disposition", "rework_count", "serial_number",
                    "mo_number", "station", "rework_station", "opened_by", "closed_by",
                    "close_notes"]),
}

REPORT_FILTER_FIELDS = {
    "traceability": ("mo", "serial", "station", "operator_id", "date_from", "date_to"),
    "serial_ledger": ("mo", "serial"),
    "station_summary": ("mo", "station", "operator_id", "date_from", "date_to"),
    "qc_ledger": ("mo", "serial", "station", "operator_id", "date_from", "date_to"),
    "exception_ledger": ("mo", "serial", "station", "operator_id", "date_from", "date_to"),
    "downtime_ledger": ("mo", "station", "operator_id", "date_from", "date_to"),
    "ncr_ledger": ("mo", "serial", "station", "operator_id", "date_from", "date_to"),
}


def _report_filter_values(rtype):
    supported = REPORT_FILTER_FIELDS[rtype]
    all_fields = ("mo", "serial", "station", "operator_id", "date_from", "date_to")
    values = {key: request.args.get(key, "").strip() if key in supported else ""
              for key in all_fields}
    return values, {key: value for key, value in values.items() if value}


def _report_date_error(filters):
    for key in ("date_from", "date_to"):
        if filters.get(key):
            try:
                datetime.strptime(filters[key], "%Y-%m-%d")
            except ValueError:
                return "Enter report dates in YYYY-MM-DD format."
    if filters.get("date_from") and filters.get("date_to"):
        if filters["date_from"] > filters["date_to"]:
            return "The start date must be on or before the end date."
    return None


def _load_secret_key():
    env = os.environ.get("MTS_SECRET_KEY")
    if env:
        return env
    key_file = Path(__file__).resolve().parent / "data" / ".flask_secret"
    if not key_file.exists():
        key_file.write_text(secrets.token_hex(32))
    return key_file.read_text().strip()


app = Flask(__name__)
app.config["SECRET_KEY"] = _load_secret_key()
app.config["DATABASE_PATH"] = str(DATABASE_PATH)
app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024
db.initialize()
app.register_blueprint(machine_api.bp)
app.register_blueprint(integration_api.bp)
app.register_blueprint(integration_api.mapi)


def _fmt_dt(v):
    if not v:
        return None
    return str(v)[:19].replace("T", " ")


# ------------------------------------------------------------------ auth guards
def login_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not session.get("user"):
            try:
                return redirect(url_for("login", next=request.path))
            except Exception:
                return redirect("/login")
        return f(*args, **kwargs)
    return wrapped

def role_required(*roles):
    def deco(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            user = session.get("user")
            if not user:
                try:
                    return redirect(url_for("login", next=request.path))
                except Exception:
                    return redirect("/login")
            if user["role"] not in roles:
                abort(403)
            return f(*args, **kwargs)
        return wrapped
    return deco

# ------------------------------------------------------------------ csrf
def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]

app.jinja_env.globals["csrf_token"] = csrf_token
app.jinja_env.globals["excel_service_name"] = \
    lambda kind: excel_service.TEMPLATES[kind]["label"]

@app.before_request
def csrf_protect():
    # Machine API paths (/api/...) authenticate with API keys instead of CSRF
    if request.method == "POST" and not request.path.startswith("/api/"):
        tok = session.get("csrf")
        if not tok or request.form.get("csrf") != tok:
            abort(400)

@app.context_processor
def inject_globals():
    user = session.get("user") or {}
    nav = []
    if user.get("role") == "OPERATOR":
        nav = [("/operator", "Operator Interface"),
               ("/my-transactions", "My Transactions"),
               ("/andon", "Andon Board")]
    elif user.get("role") == "ADMIN":
        nav = [("/dashboard", "Live Dashboard"),
               ("/operator", "Operator Interface"),
               ("/admin/stations", "Stations"),
               ("/admin/station_types", "Station Types"),
               ("/admin/materials", "Materials"),
               ("/admin/products", "Products"),
               ("/admin/mos", "MO Management"),
               ("/admin/mo_master", "MO Master"),
               ("/admin/assignment", "Material Assignment"),
               ("/admin/serials", "Serial Numbers"),
               ("/admin/qc", "QC Checks"),
               ("/admin/parameters", "Parameters"),
               ("/admin/bulk", "Bulk Upload"),
               ("/admin/users", "Users & Allocation"),
           ("/admin/transactions", "Transactions"),
           ("/admin/exceptions", "Exceptions"),
           ("/admin/genealogy", "Genealogy & Recall"),
           ("/admin/downtime", "Downtime & OEE"),
           ("/admin/ncr", "NCR / Rework"),
           ("/admin/reports", "Reports"),
           ("/admin/api", "API"),
           ("/admin/configuration", "Configuration"),
           ("/admin/dbmaint", "Backup & Reset"),
           ("/andon", "Andon Board")]
    elif user.get("role") == "VIEWER":
        nav = [("/dashboard", "Live Dashboard"),
               ("/admin/transactions", "Transactions"),
               ("/admin/exceptions", "Exceptions"),
               ("/admin/genealogy", "Genealogy & Recall"),
               ("/admin/downtime", "Downtime & OEE"),
               ("/admin/ncr", "NCR / Rework"),
               ("/admin/reports", "Reports"),
               ("/admin/parameters", "Parameters"),
               ("/andon", "Andon Board")]
    unread = notification_service.unread_count() if user else 0
    return dict(user=user, nav=nav, csrf_token=csrf_token, path=request.path,
                unread_alerts=unread)

# ------------------------------------------------------------------ errors
def _error_page(code, title, message):
    return render_template("error.html", code=code, title=title,
                           message=message), code

@app.errorhandler(400)
def _bad_request(e):
    return _error_page(400, "Bad request", "The request could not be processed "
                       "(possibly an expired form or CSRF token).")

@app.errorhandler(403)
def _forbidden(e):
    return _error_page(403, "Access denied",
                       "Your role does not allow this action.")

@app.errorhandler(404)
def _not_found(e):
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "Endpoint not found."}), 404
    return _error_page(404, "Page not found",
                       "The page you requested does not exist.")

@app.errorhandler(500)
def _server_error(e):
    return _error_page(500, "System error",
                       "An unexpected error occurred. Check the server log.")

# ------------------------------------------------------------------ auth routes
@app.route("/health")
def health():
    return "ok"

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = Authentication().login(username, password)
        if not user:
            flash("Invalid username or password.", "error")
            return redirect(url_for("login"))
        session.clear()
        session["user"] = user
        session["csrf"] = secrets.token_hex(16)
        nxt = request.args.get("next")
        dest = nxt if nxt and nxt.startswith("/") else url_for("home")
        return redirect(dest)
    return render_template("login.html")

@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You have been signed out.", "success")
    return redirect(url_for("login"))

@app.route("/")
@login_required
def home():
    role = session["user"]["role"]
    if role == "OPERATOR":
        return redirect(url_for("operator"))
    if role in ("ADMIN",):
        return redirect(url_for("admin_page", page="stations"))
    return redirect(url_for("admin_page", page="transactions"))

# ------------------------------------------------------------------ operator
@app.route("/operator", methods=["GET"])
@role_required("OPERATOR", "ADMIN")
def operator():
    user = session["user"]
    station = None
    if user.get("station_id"):
        station = db.one("SELECT * FROM stations WHERE id = ?", (user["station_id"],))
    ctx = dict(station=station or None, serial=None, assignments=[], pending=[],
               done_ids=set(), open_stoppage=None, dt_reasons=[], route_progress=[],
               today_dt={"minutes": 0, "events": 0}, station_scans_today=0)
    if not station or not station["active"]:
        return render_template("operator.html", **ctx)

    dt = DowntimeService()
    ctx["open_stoppage"] = dt.open_event(station["id"])
    ctx["dt_reasons"] = dt.reasons(active_only=True)
    ctx["today_dt"] = dt.today_summary(station["id"])
    ctx["station_scans_today"] = db.one(
        "SELECT COUNT(*) c FROM material_transactions "
        "WHERE station_id = ? AND date(timestamp) = date('now')",
        (station["id"],))["c"]

    serial_id = session.get("op_serial_id")
    if serial_id:
        srow = db.one(
            """SELECT s.*, mo.mo_number, mo.status AS mo_status
               FROM serial_numbers s JOIN manufacturing_orders mo ON s.mo_id = mo.id
               WHERE s.id = ?""", (serial_id,))
        if srow:
            val = ValidationService()
            if val.is_qc_station(station):
                ctx.update(serial=srow, assignments=[], pending=[], done_ids=set(),
                           qc_result=val.qc_result(serial_id, station["id"]))
            else:
                assignments = val.get_assignments(station["id"], srow["mo_id"])
                pending = val.pending_materials(station["id"], serial_id)
                done = {a["id"] for a in assignments} - {a["id"] for a in pending}
                ctx.update(serial=srow, assignments=assignments, pending=pending, done_ids=done)
            ctx["route_progress"] = val.route_progress(serial_id, srow["mo_id"])
        else:
            session.pop("op_serial_id", None)
    return render_template("operator.html", **ctx)

@app.route("/operator/serial", methods=["POST"])
@role_required("OPERATOR", "ADMIN")
def operator_serial():
    user = session["user"]
    station = db.one("SELECT * FROM stations WHERE id = ?", (user.get("station_id"),))
    if not station or not station["active"]:
        flash("No active station is assigned to your user account.", "error")
        return redirect(url_for("operator"))
    serial_number = request.form.get("serial", "").strip()
    if not serial_number:
        flash("Please scan or enter a serial number.", "error")
        return redirect(url_for("operator"))
    res = ValidationService().validate_serial(station["id"], serial_number)
    if not res.ok:
        flash(res.error, "blocked")
        return redirect(url_for("operator"))
    session["op_serial_id"] = res.data["serial"]["id"]
    flash("Serial locked. Scan the required materials below.", "success")
    return redirect(url_for("operator"))

@app.route("/operator/material", methods=["POST"])
@role_required("OPERATOR", "ADMIN")
def operator_material():
    user = session["user"]
    station = db.one("SELECT * FROM stations WHERE id = ?", (user.get("station_id"),))
    if not station or not station["active"]:
        flash("No active station.", "error")
        return redirect(url_for("operator"))
    static = station["id"]
    sid = session.get("op_serial_id")
    if not sid:
        flash("Scan a serial first.", "warn")
        return redirect(url_for("operator"))
    srow = db.one(
        """SELECT s.*, mo.mo_number, mo.status AS mo_status
           FROM serial_numbers s JOIN manufacturing_orders mo ON s.mo_id = mo.id
           WHERE s.id = ?""", (sid,))
    if not srow:
        session.pop("op_serial_id", None)
        flash("Serial not found. Scan again.", "error")
        return redirect(url_for("operator"))

    barcode = request.form.get("barcode", "").strip()
    val = ValidationService()
    pending = val.pending_materials(static, sid)
    if not pending:
        flash("All required materials for this serial are already recorded.", "complete")
        session.pop("op_serial_id", None)
        return redirect(url_for("operator"))

    res = val.validate_material(static, sid, barcode, pending)
    if not res.ok:
        error = res.error
        try:
            TransactionService().log_exception(
                operator_id=user["id"], station_id=static, serial=srow,
                scanned=barcode, error=error)
        except Exception:
            pass
        flash(error, "blocked")
        return redirect(url_for("operator"))

    tx_id, remaining, all_done, route_done = TransactionService().record_material(
        operator_id=user["id"], station_id=static, serial=srow,
        material=res.data["material"], barcode=barcode, parsed=res.data.get("parsed"))
    if all_done:
        session.pop("op_serial_id", None)
        flash(f"All required materials for serial {srow['serial_number']} were recorded. "
              f"Last: {res.data['material']['material_code']} (tx {tx_id}).", "complete")
    else:
        flash(f"{res.data['material']['material_code']} — {res.data['material']['material_description']} "
              f"(tx {tx_id}). {len(remaining)} more material(s) to scan.", "recorded")
    return redirect(url_for("operator"))

@app.route("/operator/qc", methods=["POST"])
@role_required("OPERATOR", "ADMIN")
def operator_qc():
    user = session["user"]
    station = db.one("SELECT * FROM stations WHERE id = ?", (user.get("station_id"),))
    if not station or not station["active"]:
        flash("No active station.", "error")
        return redirect(url_for("operator"))
    if not ValidationService().is_qc_station(station):
        flash("QC decisions are only allowed at a quality station.", "blocked")
        return redirect(url_for("operator"))
    sid = session.get("op_serial_id")
    if not sid:
        flash("Scan a serial first.", "warn")
        return redirect(url_for("operator"))
    srow = db.one("SELECT * FROM serial_numbers WHERE id = ?", (sid,))
    if not srow:
        session.pop("op_serial_id", None)
        flash("Serial not found. Scan again.", "error")
        return redirect(url_for("operator"))

    decision = request.form.get("decision", "").upper()
    if decision not in ("PASS", "FAIL"):
        flash("Invalid QC decision.", "error")
        return redirect(url_for("operator"))
    if decision == "FAIL" and request.form.get("fail_status", "").upper() not in ("HOLD", "REJECTED"):
        flash("Choose HOLD or REJECTED for a failed inspection.", "error")
        return redirect(url_for("operator"))

    reason = request.form.get("reason", "").strip()
    try:
        outcome = TransactionService().record_qc(
            operator_id=user["id"], station_id=station["id"], serial=srow,
            decision=decision,
            fail_status=request.form.get("fail_status", "").upper(),
            reason=reason)
    except ValueError as exc:
        session.pop("op_serial_id", None)
        flash(str(exc), "blocked")
        return redirect(url_for("operator"))
    if outcome == "PASS":
        session.pop("op_serial_id", None)
        flash(f"QC PASS recorded for {srow['serial_number']}. The unit may continue.", "success")
    elif outcome == "HOLD":
        flash(f"QC FAIL → HOLD recorded for {srow['serial_number']}. "
              f"It must pass a quality station before continuing.", "blocked")
    else:
        flash(f"QC FAIL → REJECTED recorded for {srow['serial_number']}. No further processing.", "blocked")
    return redirect(url_for("operator"))

@app.route("/operator/cancel", methods=["POST"])
@role_required("OPERATOR", "ADMIN")
def operator_cancel():
    session.pop("op_serial_id", None)
    flash("Scan cancelled. Scan a new serial.", "warn")
    return redirect(url_for("operator"))

@app.route("/my-transactions")
@role_required("OPERATOR")
def my_transactions():
    rows = TransactionService().transactions({"operator_id": session["user"]["id"]})
    return render_template("my_transactions.html", rows=rows)

# ------------------------------------------------------------------ admin
@app.route("/admin/<page>")
@login_required
def admin_page(page):
    if session["user"]["role"] not in ("ADMIN", "VIEWER"):
        abort(403)
    if page not in dict(ADMIN_PAGES):
        abort(404)
    if page == "transactions":
        return _render_transactions()
    if page == "exceptions":
        return _render_exceptions()
    if session["user"]["role"] != "ADMIN" and page not in VIEWER_PAGES:
        abort(403)
    by_page = {
        "stations": _render_stations,
        "station_types": _render_station_types,
        "materials": _render_materials,
        "products": _render_products,
        "mos": _render_mos,
        "mo_master": _render_mo_master,
        "assignment": _render_assignment,
        "serials": _render_serials,
        "qc": _render_qc,
        "parameters": _render_parameters,
        "users": _render_users,
        "genealogy": _render_genealogy,
        "downtime": _render_downtime,
        "ncr": _render_ncr,
        "dbmaint": _render_dbmaint,
    }
    return by_page[page]()

def _render_stations():
    service = AdminService()
    return render_template("admin/stations.html", rows=service.stations(),
                           station_types=[t for t in service.station_types() if t["active"]])

def _render_station_types():
    return render_template("admin/station_types.html", types=AdminService().station_types())

def _render_products():
    return render_template("admin/products.html", products=ProductService().list())

def _render_materials():
    mats = MaterialService().list(active=False)
    return render_template("admin/materials.html", rows=mats)

def _render_mos():
    return render_template("admin/mos.html", rows=AdminService().mos())

def _render_assignment():
    service = AdminService()
    stations = [s for s in service.stations() if s["active"]]
    mos = [m for m in service.mos() if m["active"] and m["status"] != "CLOSED"]
    mats = MaterialService().list()
    # Only stations in a MO's route can receive materials for it.
    val = ValidationService()
    allowed = {}
    station_ids = [s["id"] for s in stations]
    for m in mos:
        route = val.route_stations(m["id"])
        allowed[m["id"]] = {s["id"] for s in route} if route else set(station_ids)
    for s in stations:
        s["mos_data"] = " ".join(str(m["id"]) for m in mos if s["id"] in allowed[m["id"]])
    return render_template(
        "admin/assignment.html", stations=stations, mos=mos, mats=mats,
        rows=service.assignments())

def _render_mo_master():
    service = AdminService()
    mos = [m for m in service.mos() if m["active"]]
    sel_mo = request.args.get("mo", type=int)
    if not any(m["id"] == sel_mo for m in mos):
        sel_mo = mos[0]["id"] if mos else None
    mo_row = next((m for m in mos if m["id"] == sel_mo), None)
    vals = ValidationService()
    stations = [s for s in service.stations() if s["active"]]
    explicit = {r["station_id"]: r for r in service.mo_route(sel_mo)} if sel_mo else {}
    effective = vals.route_stations(sel_mo) if sel_mo else []
    eff_ids = {s["id"] for s in effective}
    for s in stations:
        s["included"] = s["id"] in eff_ids
        s["order"] = explicit[s["id"]]["route_order"] if s["id"] in explicit else \
            (next((r["route_order"] for r in effective if r["id"] == s["id"]), s["route_order"]) or 0)
        s["stage"] = (explicit[s["id"]].get("stage_code") if s["id"] in explicit else None) \
            or next((r.get("stage_code") for r in effective if r["id"] == s["id"]), None) \
            or s.get("stage_code") or ""
    assigns = service.assignments(mo_id=sel_mo) if sel_mo else []
    by_station = {}
    for a in assigns:
        by_station.setdefault(a["station_id"], []).append(a)
    serials = SerialService().by_mo(sel_mo) if sel_mo else []
    mats = [m for m in MaterialService().list() if m["active"]]
    products = ProductService().list(active=True)
    mo_product = service.mo_product(sel_mo) if sel_mo else None
    if mo_product is not None and mo_product["id"] is None:
        mo_product = None
    route_source = vals.route_source(sel_mo) if sel_mo else None
    return render_template(
        "admin/mo_master.html", mos=mos, mo=mo_row, stations=stations,
        route_stations=effective, by_station=by_station, assigns=assigns, serials=serials,
        mats=mats, statuses=["AVAILABLE", "IN_PROCESS", "COMPLETED", "HOLD", "REJECTED"],
        explicit=bool(explicit), products=products, mo_product=mo_product,
        route_source=route_source)

def _render_serials():
    service = AdminService()
    mos = [m for m in service.mos() if m["active"]]
    sel_mo = request.args.get("mo") or (mos[0]["id"] if mos else None)
    rows = SerialService().by_mo(sel_mo) if sel_mo else []
    return render_template("admin/serials.html", mos=mos, sel_mo=sel_mo, rows=rows,
                           statuses=["AVAILABLE", "IN_PROCESS", "COMPLETED", "HOLD", "REJECTED"])

def _render_users():
    service = AdminService()
    stations = [s for s in service.stations() if s["active"]]
    users = [r for r in service.users()
             if r["username"] not in ("admin", "machine")]
    return render_template("admin/users.html", stations=stations, users=users,
                           roles=["OPERATOR", "ADMIN", "VIEWER"])

def _render_transactions():
    f = {k: request.args.get(k, "").strip() for k in ("mo", "serial", "station")}
    filters = {k: v for k, v in f.items() if v}
    rows = TransactionService().transactions(filters)
    return render_template("admin/transactions.html", rows=rows, f=f)

def _render_exceptions():
    return render_template("admin/exceptions.html", rows=TransactionService().exceptions())

def _render_qc():
    return render_template("admin/qc.html", rows=TransactionService().qc_checks())

def _render_parameters():
    f = {k: request.args.get(k, "").strip()
         for k in ("mo", "serial", "station", "parameter", "date_from", "date_to")}
    filters = {k: v for k, v in f.items() if v}
    rows, columns = report_service.parameter_rows(filters)
    stations = db.query("SELECT station_code FROM stations WHERE active = 1 "
                        "ORDER BY route_order, station_code")
    keys = db.query("SELECT DISTINCT key FROM process_parameters ORDER BY key")
    return render_template("admin/parameters.html", f=f, rows=rows, columns=columns,
                           stations=stations, param_keys=keys)

def _render_genealogy():
    f = {k: request.args.get(k, "").strip()
         for k in ("serial", "material", "vendor", "batch", "mo", "date_from", "date_to")}
    svc = GenealogyService()
    genealogy = recall_rows = impact = mode = None
    if f["serial"]:
        mode = "serial"
        genealogy = svc.serial_genealogy(f["serial"])
    elif any(f[k] for k in ("material", "vendor", "batch", "mo", "date_from", "date_to")):
        mode = "recall"
        recall_rows = svc.recall_search({k: v for k, v in f.items() if v})
        impact = svc.recall_impact(recall_rows)
    else:
        recall_rows = []
    return render_template("admin/genealogy.html", f=f, mode=mode,
                           genealogy=genealogy, rows=recall_rows or [], impact=impact,
                           export_cols=GenealogyService.recall_export_columns())

def _render_downtime():
    f = {k: request.args.get(k, "").strip()
         for k in ("station", "date_from", "date_to", "planned")}
    svc = DowntimeService()
    planned = None
    if f["planned"]:
        try:
            planned = max(1, int(f["planned"]))
        except ValueError:
            planned = None
    oee = svc.oee(date_from=f["date_from"] or None, date_to=f["date_to"] or None,
                  station_code=f["station"] or None, planned_override=planned)
    return render_template(
        "admin/downtime.html", f=f,
        reasons=svc.reasons(), open_ev=svc.open_events(), history=svc.recent(100),
        stations=db.query("SELECT * FROM stations WHERE active = 1 "
                          "ORDER BY route_order, station_code"),
        oee=oee)

def _render_ncr():
    f = {k: request.args.get(k, "").strip()
         for k in ("disposition", "serial", "mo", "date_from", "date_to")}
    svc = NCRService()
    return render_template(
        "admin/ncr.html", f=f, rows=svc.list(f), stats=svc.stats(),
        needs_ncr=svc.open_serials(),
        stations=db.query("SELECT * FROM stations WHERE active = 1 "
                          "ORDER BY route_order, station_code"))

def _render_dbmaint():
    return render_template("admin/dbmaint.html", backups=AdminService().backups())

def _bulk_ctx(kind):
    kind = kind if kind in excel_service.TEMPLATES else "stations"
    return dict(kinds=list(excel_service.TEMPLATES.keys()),
                spec=excel_service.TEMPLATES[kind], kind=kind,
                columns=excel_service.display_format(kind))

# ------------------------------------------------------------- bulk upload
@app.route("/admin/bulk", methods=["GET"])
@role_required("ADMIN")
def bulk_page():
    return render_template("admin/bulk.html", **_bulk_ctx(request.args.get("kind", "stations")))

@app.route("/admin/bulk/<kind>/template")
@role_required("ADMIN")
def bulk_template(kind):
    if kind not in excel_service.TEMPLATES:
        abort(404)
    payload = excel_service.build_template(kind)
    return send_file(BytesIO(payload),
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True,
                     download_name=f"mts_{kind}_template.xlsx")

@app.route("/admin/bulk/<kind>", methods=["POST"])
@role_required("ADMIN")
def bulk_upload(kind):
    if kind not in excel_service.TEMPLATES:
        abort(404)
    f = request.files.get("file")
    if not f or not f.filename:
        flash("Please choose an Excel (.xlsx) or CSV file to upload.", "error")
        return redirect(url_for("bulk_page", kind=kind))
    ok, payload = excel_service.read_upload(f.read(), f.filename, kind)
    if not ok:
        flash(payload, "error")
        return redirect(url_for("bulk_page", kind=kind))
    result = excel_service.apply_import(kind, payload)
    return render_template("admin/bulk.html", result=result, **_bulk_ctx(kind))

# ------------------------------------------------------------- admin actions
@app.route("/admin/stations/add", methods=["POST"])
@role_required("ADMIN")
def station_add():
    ok, msg = AdminService().add_station(form=request.form)
    flash(msg, "success" if ok else "error")
    return redirect(url_for("admin_page", page="stations"))

@app.route("/admin/stations/<int:sid>/toggle", methods=["POST"])
@role_required("ADMIN")
def station_toggle(sid):
    AdminService().toggle_station(sid)
    return redirect(url_for("admin_page", page="stations"))

@app.route("/admin/stations/<int:sid>/update", methods=["POST"])
@role_required("ADMIN")
def station_update(sid):
    action = request.form.get("action")
    service = AdminService()
    if action == "delete":
        ok, msg = service.delete_station(sid)
    elif action == "clear_halt":
        service.clear_halt(sid)
        ok, msg = True, "Station cleared and released for scanning."
    else:
        ok, msg = service.update_station(sid, form=request.form)
    flash(msg, "success" if ok else "error")
    return redirect(url_for("admin_page", page="stations"))

@app.route("/admin/stations/<int:sid>/halt", methods=["POST"])
@role_required("ADMIN")
def station_halt(sid):
    AdminService().halt_station(sid, request.form.get("reason", ""))
    flash(f"Station {sid} halted.", "success")
    return redirect(url_for("admin_page", page="stations"))

@app.route("/admin/stations/<int:sid>/clear-halt", methods=["POST"])
@role_required("ADMIN")
def station_clear_halt(sid):
    AdminService().clear_halt(sid)
    flash("Station cleared and released for scanning.", "success")
    return redirect(url_for("admin_page", page="stations"))

# ------------------------------------------------------ station type catalogue
@app.route("/admin/station_types/add", methods=["POST"])
@role_required("ADMIN")
def admin_station_type_add():
    ok, msg = AdminService().add_station_type(
        request.form.get("code"), request.form.get("label"),
        bool(request.form.get("is_qc")), bool(request.form.get("requires_material")))
    flash(msg, "success" if ok else "error")
    return redirect(url_for("admin_page", page="station_types"))

@app.route("/admin/station_types/<int:tid>/update", methods=["POST"])
@role_required("ADMIN")
def admin_station_type_update(tid):
    action = request.form.get("action")
    service = AdminService()
    if action == "delete":
        ok, msg = service.delete_station_type(tid)
    elif action == "toggle":
        service.toggle_station_type(tid)
        ok, msg = True, "Station type toggled."
    else:
        ok, msg = service.update_station_type(
            tid, request.form.get("label"),
            bool(request.form.get("is_qc")), bool(request.form.get("requires_material")))
    flash(msg, "success" if ok else "error")
    return redirect(url_for("admin_page", page="station_types"))

# ------------------------------------------------------ product master
@app.route("/admin/products/add", methods=["POST"])
@role_required("ADMIN")
def admin_product_add():
    ok, msg = ProductService().add(
        request.form.get("code"), request.form.get("name"),
        request.form.get("revision"), request.form.get("description"))
    flash(msg, "success" if ok else "error")
    return redirect(url_for("admin_page", page="products"))

@app.route("/admin/products/<int:pid>/update", methods=["POST"])
@role_required("ADMIN")
def admin_product_update(pid):
    action = request.form.get("action")
    service = ProductService()
    if action == "delete":
        ok, msg = service.delete(pid)
    elif action == "toggle":
        service.toggle(pid)
        ok, msg = True, "Product toggled."
    else:
        ok, msg = service.update(pid, request.form.get("code"),
                                 request.form.get("name"),
                                 request.form.get("revision"),
                                 request.form.get("description"))
    flash(msg, "success" if ok else "error")
    return redirect(url_for("admin_page", page="products"))

@app.route("/admin/products/<int:pid>/route")
@role_required("ADMIN")
def admin_product_route(pid):
    service = ProductService()
    product = service.get(pid)
    if not product:
        abort(404)
    return render_template("admin/product_route.html", product=product,
                           stages=service.route(pid),
                           stations=[s for s in AdminService().stations() if s["active"]])

@app.route("/admin/products/<int:pid>/route/stage/add", methods=["POST"])
@role_required("ADMIN")
def admin_product_route_stage_add(pid):
    ok, msg = ProductService().add_stage(
        pid, request.form.get("station_id", type=int),
        request.form.get("stage_code"),
        request.form.get("seq", type=int))
    flash(msg, "success" if ok else "error")
    return redirect(url_for("admin_product_route", pid=pid))

@app.route("/admin/products/<int:pid>/route/stage/<int:rid>/remove", methods=["POST"])
@role_required("ADMIN")
def admin_product_route_stage_remove(pid, rid):
    ProductService().remove_stage(rid)
    flash("Stage removed.", "success")
    return redirect(url_for("admin_product_route", pid=pid))

@app.route("/admin/products/<int:pid>/route/stage/<int:rid>/update", methods=["POST"])
@role_required("ADMIN")
def admin_product_route_stage_update(pid, rid):
    db.execute("UPDATE product_route SET stage_code = ? WHERE id = ?",
               ((request.form.get("stage_code") or "").strip() or None, rid))
    flash("Stage code updated.", "success")
    return redirect(url_for("admin_product_route", pid=pid))

@app.route("/admin/products/<int:pid>/route/stage/<int:rid>/move", methods=["POST"])
@role_required("ADMIN")
def admin_product_route_move(pid, rid):
    ProductService().move_stage(rid, request.form.get("dir", "down"))
    return redirect(url_for("admin_product_route", pid=pid))

@app.route("/admin/materials/add", methods=["POST"])
@role_required("ADMIN")
def material_add():
    service = MaterialService()
    ok = service.create(request.form.get("code", ""),
                        request.form.get("desc", ""),
                        request.form.get("rule", "").strip() or None)
    flash("Material added." if ok else "Material code already exists.",
          "success" if ok else "error")
    return redirect(url_for("admin_page", page="materials"))

@app.route("/admin/materials/<int:mid>/toggle", methods=["POST"])
@role_required("ADMIN")
def material_toggle(mid):
    MaterialService().toggle(mid)
    return redirect(url_for("admin_page", page="materials"))

@app.route("/admin/materials/<int:mid>/update", methods=["POST"])
@role_required("ADMIN")
def material_update(mid):
    action = request.form.get("action")
    service = MaterialService()
    if action == "delete":
        ok, msg = service.delete(mid)
    else:
        ok, msg = service.update(mid, request.form.get("code", ""),
                                 request.form.get("desc", ""),
                                 request.form.get("rule", ""))
    flash(msg, "success" if ok else "error")
    return redirect(url_for("admin_page", page="materials"))

@app.route("/admin/mos/add", methods=["POST"])
@role_required("ADMIN")
def mo_add():
    service = AdminService()
    qty = request.form.get("qty", "1")
    ok = service.add_mo(request.form.get("number", ""),
                        request.form.get("desc", ""), qty)
    flash("MO added." if ok else "MO number already exists.", "success" if ok else "error")
    return redirect(url_for("admin_page", page="mos"))

@app.route("/admin/mos/<int:mid>/status", methods=["POST"])
@role_required("ADMIN")
def mo_status(mid):
    status = request.form.get("status", "RELEASED")
    AdminService().set_mo_status(mid, status)
    return redirect(url_for("admin_page", page="mos"))

@app.route("/admin/mos/<int:mid>/update", methods=["POST"])
@role_required("ADMIN")
def mo_update(mid):
    action = request.form.get("action")
    service = AdminService()
    if action == "delete":
        ok, msg = service.delete_mo(mid)
    else:
        ok, msg = service.update_mo(mid, request.form.get("number", ""),
                                    request.form.get("desc", ""),
                                    request.form.get("qty", "1"))
        if ok:
            service.set_mo_status(mid, request.form.get("status", "DRAFT"))
    flash(msg, "success" if ok else "error")
    return redirect(url_for("admin_page", page="mos"))

# ------------------------------------------------------------- MO master
@app.route("/admin/mo-master/<int:mid>/status", methods=["POST"])
@role_required("ADMIN")
def mo_master_status(mid):
    AdminService().set_mo_status(mid, request.form.get("status", "DRAFT"))
    return redirect(url_for("admin_page", page="mo_master", mo=mid))

@app.route("/admin/mo-master/<int:mid>/route", methods=["POST"])
@role_required("ADMIN")
def mo_master_route(mid):
    service = AdminService()
    items = []
    for st in service.stations():
        if not st["active"]:
            continue
        if request.form.get(f"inc_{st['id']}"):
            items.append((st["id"], int(request.form.get(f"ord_{st['id']}", "0") or "0"),
                          request.form.get(f"stage_{st['id']}", "")))
    items.sort(key=lambda x: x[1])
    service.save_mo_route(mid, [(sid, i + 1, stage)
                                for i, (sid, _, stage) in enumerate(items)])
    flash("MO route saved." if items else "Route cleared - falling back to the product/default order.",
          "success")
    return redirect(url_for("admin_page", page="mo_master", mo=mid))

@app.route("/admin/mo-master/<int:mid>/route-default", methods=["POST"])
@role_required("ADMIN")
def mo_master_route_default(mid):
    service = AdminService()
    snap = service.default_route_snapshot()
    service.save_mo_route(mid, [(st["id"], i + 1, st.get("stage_code"))
                                for i, st in enumerate(snap)])
    flash("Route set to the global default station order.", "success")
    return redirect(url_for("admin_page", page="mo_master", mo=mid))

@app.route("/admin/mo-master/<int:mid>/route-from-product", methods=["POST"])
@role_required("ADMIN")
def mo_master_route_from_product(mid):
    service = AdminService()
    product = service.mo_product(mid)
    if not product or product.get("id") is None:
        flash("This MO is not linked to a product master.", "error")
        return redirect(url_for("admin_page", page="mo_master", mo=mid))
    stages = ProductService().route(product["id"])
    if not stages:
        flash("The linked product has no route stages defined.", "error")
    else:
        service.save_mo_route(mid, [(r["station_id"], r["seq"] or i + 1, r.get("stage_code"))
                                    for i, r in enumerate(stages)])
        flash("Route copied from the product master.", "success")
    return redirect(url_for("admin_page", page="mo_master", mo=mid))

@app.route("/admin/mo-master/<int:mid>/product", methods=["POST"])
@role_required("ADMIN")
def mo_master_product(mid):
    ok, msg = AdminService().set_mo_product(mid, request.form.get("product_id", type=int))
    flash(msg, "success" if ok else "error")
    return redirect(url_for("admin_page", page="mo_master", mo=mid))

@app.route("/admin/mo-master/<int:mid>/material/add", methods=["POST"])
@role_required("ADMIN")
def mo_master_material_add(mid):
    station_id = request.form.get("station_id", type=int)
    material_id = request.form.get("material_id", type=int)
    rule = request.form.get("rule", "").strip() or None
    ok = AdminService().add_assignment(station_id, mid, material_id, rule)
    flash("Material assigned." if ok else "Assignment failed (duplicate?).",
          "success" if ok else "error")
    return redirect(url_for("admin_page", page="mo_master", mo=mid))

@app.route("/admin/mo-master/<int:mid>/material/<int:aid>/remove", methods=["POST"])
@role_required("ADMIN")
def mo_master_material_remove(mid, aid):
    AdminService().remove_assignment(aid)
    flash("Assignment removed.", "success")
    return redirect(url_for("admin_page", page="mo_master", mo=mid))

@app.route("/admin/mo-master/<int:mid>/serials/add", methods=["POST"])
@role_required("ADMIN")
def mo_master_serials_add(mid):
    single = request.form.get("serial", "").strip()
    bulk = request.form.get("bulk", "").strip()
    items = [single] if single else [x.strip() for x in bulk.splitlines() if x.strip()]
    ok, fail = SerialService().add_many(items, mid) if items else (0, 0)
    flash(f"Added {ok}, failed {fail}.", "success" if ok else ("error" if fail else "warn"))
    return redirect(url_for("admin_page", page="mo_master", mo=mid))

@app.route("/admin/mo-master/<int:mid>/serials/<int:sid>/delete", methods=["POST"])
@role_required("ADMIN")
def mo_master_serial_delete(mid, sid):
    ok = SerialService().delete(sid)
    flash("Serial deleted." if ok else "Has transactions; cannot delete.",
          "success" if ok else "error")
    return redirect(url_for("admin_page", page="mo_master", mo=mid))

@app.route("/admin/mo-master/<int:mid>/serials/<int:sid>/status", methods=["POST"])
@role_required("ADMIN")
def mo_master_serial_status(mid, sid):
    SerialService().set_status(sid, request.form.get("status", "AVAILABLE"))
    return redirect(url_for("admin_page", page="mo_master", mo=mid))

@app.route("/admin/assignment/add", methods=["POST"])
@role_required("ADMIN")
def assignment_add():
    service = AdminService()
    station_id = request.form.get("station_id", type=int)
    mo_id = request.form.get("mo_id", type=int)
    rule = request.form.get("rule", "").strip() or None
    material_ids = request.form.getlist("material_ids")
    added = sum(1 for mid in material_ids
                if service.add_assignment(station_id, mo_id, int(mid), rule))
    flash(f"Assigned {added} material(s)." if added else "Nothing to assign.",
          "success" if added else "warn")
    return redirect(url_for("admin_page", page="assignment"))

@app.route("/admin/assignment/<int:aid>/remove", methods=["POST"])
@role_required("ADMIN")
def assignment_remove(aid):
    AdminService().remove_assignment(aid)
    flash("Assignment removed.", "success")
    return redirect(url_for("admin_page", page="assignment"))

@app.route("/admin/assignment/<int:aid>/update", methods=["POST"])
@role_required("ADMIN")
def assignment_update(aid):
    service = AdminService()
    if request.form.get("action") == "remove":
        service.remove_assignment(aid)
        flash("Assignment removed.", "success")
    else:
        ok, msg = service.update_assignment(
            aid, request.form.get("material_id", type=int),
            request.form.get("rule", ""))
        flash(msg, "success" if ok else "error")
    return redirect(url_for("admin_page", page="assignment"))

@app.route("/admin/serials/add", methods=["POST"])
@role_required("ADMIN")
def serial_add():
    service = SerialService()
    mo_id = request.form.get("mo_id", type=int)
    single = request.form.get("serial", "").strip()
    bulk = request.form.get("bulk", "").strip()
    items = [single] if single else [x.strip() for x in bulk.splitlines() if x.strip()]
    ok, fail = service.add_many(items, mo_id) if mo_id else (0, 1)
    flash(f"Added {ok}, failed {fail}.", "success" if ok else "error")
    return redirect(url_for("admin_page", page="serials", mo=mo_id or None))

@app.route("/admin/serials/<int:sid>/status", methods=["POST"])
@role_required("ADMIN")
def serial_status(sid):
    status = request.form.get("status", "AVAILABLE")
    SerialService().set_status(sid, status)
    mo = request.form.get("mo_id")
    return redirect(url_for("admin_page", page="serials", mo=mo or None))

@app.route("/admin/serials/<int:sid>/delete", methods=["POST"])
@role_required("ADMIN")
def serial_delete(sid):
    mo = request.form.get("mo_id")
    ok = SerialService().delete(sid)
    flash("Serial deleted." if ok else "Has transactions; cannot delete.",
          "success" if ok else "error")
    return redirect(url_for("admin_page", page="serials", mo=mo or None))

@app.route("/admin/serials/<int:sid>/update", methods=["POST"])
@role_required("ADMIN")
def serial_update(sid):
    action = request.form.get("action")
    service = SerialService()
    ok, msg = (True, "Serial saved."), None
    if action == "delete":
        ok = service.delete(sid)
        msg = "Serial deleted." if ok else "Has transactions; cannot delete."
    else:
        ok, msg = service.update_serial(sid, request.form.get("serial", ""))
        if ok and request.form.get("status"):
            service.set_status(sid, request.form.get("status", "AVAILABLE"))
    flash(msg, "success" if ok else "error")
    return redirect(url_for("admin_page", page="serials",
                            mo=request.form.get("mo_id") or None))

@app.route("/admin/users/create", methods=["POST"])
@role_required("ADMIN")
def user_create():
    auth = Authentication()
    station_id = request.form.get("station_id", type=int) or None
    usr = request.form.get("username", "")
    pwd = request.form.get("password", "")
    ok = auth.create_user(usr, pwd, request.form.get("full", ""),
                          request.form.get("role", "OPERATOR"), station_id)
    flash("User created." if ok else "Username exists or invalid input.", "success" if ok else "error")
    return redirect(url_for("admin_page", page="users"))

@app.route("/admin/users/station", methods=["POST"])
@role_required("ADMIN")
def user_station_alloc():
    uid = request.form.get("user_id", type=int)
    station_id = request.form.get("station_id", type=int) or None
    AdminService().set_user_station(uid, station_id)
    flash("Station updated.", "success")
    return redirect(url_for("admin_page", page="users"))

@app.route("/admin/users/<int:uid>/pw", methods=["POST"])
@role_required("ADMIN")
def user_pw(uid):
    new_pw = request.form.get("password", "1234") or "1234"
    Authentication().reset_password(uid, new_pw)
    flash("Password reset.", "success")
    return redirect(url_for("admin_page", page="users"))

@app.route("/admin/users/<int:uid>/toggle", methods=["POST"])
@role_required("ADMIN")
def user_toggle(uid):
    AdminService().toggle_user(uid)
    return redirect(url_for("admin_page", page="users"))

@app.route("/admin/users/<int:uid>/update", methods=["POST"])
@role_required("ADMIN")
def user_update(uid):
    action = request.form.get("action")
    service = AdminService()
    if action == "delete":
        ok, msg = service.delete_user(uid, current_user_id=session["user"]["id"])
    else:
        ok, msg = service.update_user(uid, request.form.get("full", ""),
                                      request.form.get("role", "OPERATOR"))
        if ok:
            station_id = request.form.get("station_id", type=int) or None
            service.set_user_station(uid, station_id)
    flash(msg, "success" if ok else "error")
    return redirect(url_for("admin_page", page="users"))

# ------------------------------------------------------ live dashboard
@app.route("/dashboard")
@role_required("ADMIN", "VIEWER")
def dashboard():
    kpis = dashboard_service.kpis()
    refresh = ConfigService.get_int("dashboard_refresh", 10)
    return render_template("dashboard.html", kpis=kpis,
                           stations=dashboard_service.station_activity(),
                           progress=dashboard_service.serial_progress(),
                           recent=dashboard_service.recent_transactions(10),
                           open_downtime=DowntimeService().open_events(),
                           ncr_stats=NCRService().stats(),
                           refresh=refresh)

@app.route("/api/dashboard")
@role_required("ADMIN", "VIEWER")
def api_dashboard():
    return jsonify(dashboard_service.live_json())

# ------------------------------------------------------ reports
@app.route("/admin/reports", methods=["GET"])
@role_required("ADMIN", "VIEWER")
def admin_reports():
    rtype = request.args.get("type", "traceability")
    if rtype not in REPORT_TYPES:
        rtype = "traceability"
    f, filters = _report_filter_values(rtype)
    label, fn, columns = REPORT_TYPES[rtype]
    date_error = _report_date_error(filters)
    if date_error:
        flash(date_error, "error")
        rows = []
    else:
        rows = fn(filters)
    return render_template("admin/reports.html", rtype=rtype, f=f, rows=rows,
                           label=label, columns=columns, types=REPORT_TYPES,
                           filter_fields=REPORT_FILTER_FIELDS[rtype],
                           stations=AdminService().stations(),
                           operators=db.query("SELECT id, full_name FROM users "
                                              "ORDER BY full_name"))

@app.route("/admin/reports/export")
@role_required("ADMIN", "VIEWER")
def admin_reports_export():
    rtype = request.args.get("type", "traceability")
    fmt = request.args.get("fmt", "csv")
    if rtype not in REPORT_TYPES:
        abort(404)
    if fmt not in ("csv", "xlsx"):
        abort(404)
    f, filters = _report_filter_values(rtype)
    date_error = _report_date_error(filters)
    if date_error:
        flash(date_error, "error")
        return redirect(url_for("admin_reports", type=rtype, **f))
    label, fn, columns = REPORT_TYPES[rtype]
    rows = fn(filters)
    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    fname = f"mts_{rtype}_{stamp}"
    if fmt == "xlsx":
        payload = report_service.export_xlsx(rows, columns)
        return send_file(BytesIO(payload),
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                         as_attachment=True, download_name=f"{fname}.xlsx")
    payload = report_service.export_csv(rows, columns)
    data = BytesIO(payload.encode("utf-8-sig"))
    return send_file(data, mimetype="text/csv", as_attachment=True,
                     download_name=f"{fname}.csv")

@app.route("/admin/parameters/export")
@role_required("ADMIN", "VIEWER")
def admin_parameters_export():
    fmt = request.args.get("fmt", "csv")
    f = {k: request.args.get(k, "").strip()
         for k in ("mo", "serial", "station", "parameter", "date_from", "date_to")}
    filters = {k: v for k, v in f.items() if v}
    rows, columns = report_service.parameter_rows(filters)
    columns = ["timestamp", "serial_number", "station", "station_type", "mo_number"] + columns
    flat = report_service.parameter_values(rows, [c for c in columns
                                                  if c not in ("timestamp", "serial_number",
                                                               "station", "station_type", "mo_number")])
    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    fname = f"mts_parameters_{stamp}"
    if fmt == "xlsx":
        payload = report_service.export_xlsx(flat, columns)
        return send_file(BytesIO(payload),
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                         as_attachment=True, download_name=f"{fname}.xlsx")
    payload = report_service.export_csv(flat, columns)
    data = BytesIO(payload.encode("utf-8-sig"))
    return send_file(data, mimetype="text/csv", as_attachment=True,
                     download_name=f"{fname}.csv")

# --------------------------------------------------------- machine API page
@app.route("/admin/api")
@role_required("ADMIN")
def admin_api():
    cfg = ConfigService.all()
    stations = db.query(
        """SELECT s.id AS id, s.station_code, s.station_name, s.station_type,
                  sc.api_key, sc.api_enabled, sc.api_last_seen
           FROM stations s LEFT JOIN station_comm sc ON sc.station_id = s.id
           ORDER BY s.station_code""")
    mid = db.one("SELECT id FROM users WHERE username = 'machine'")
    mid = mid["id"] if mid else None
    events = []
    if mid:
        for r in db.query(
                """SELECT t.timestamp AS ts, s.station_code, m.material_code,
                          sn.serial_number
                   FROM material_transactions t
                   JOIN stations s ON s.id = t.station_id
                   JOIN serial_numbers sn ON sn.id = t.serial_id
                   LEFT JOIN materials m ON m.id = t.material_id
                   WHERE t.operator_id = ? ORDER BY t.id DESC LIMIT 15""", (mid,)):
            events.append({"ts": r["ts"], "station": r["station_code"], "kind": "SCAN",
                           "detail": f"{r['material_code'] or '-'} -> {r['serial_number']}"})
        for r in db.query(
                """SELECT q.timestamp AS ts, s.station_code, q.decision, q.fail_status,
                          q.reason, sn.serial_number
                   FROM quality_checks q
                   JOIN stations s ON s.id = q.station_id
                   JOIN serial_numbers sn ON sn.id = q.serial_id
                   WHERE q.operator_id = ? ORDER BY q.id DESC LIMIT 15""", (mid,)):
            detail = f"{r['decision']} {r['serial_number']}"
            if r["fail_status"]:
                detail += f" ({r['fail_status']})"
            events.append({"ts": r["ts"], "station": r["station_code"], "kind": "QC",
                           "detail": detail})
        for r in db.query(
                """SELECT d.started_at AS ts, s.station_code, d.ended_at, d.minutes,
                          r.label
                   FROM downtime_events d
                   JOIN stations s ON s.id = d.station_id
                   LEFT JOIN downtime_reasons r ON r.id = d.reason_id
                   WHERE d.started_by = ? ORDER BY d.id DESC LIMIT 15""", (mid,)):
            detail = (r["label"] or "stoppage")
            detail += (" ended" if r["ended_at"] else " open")
            events.append({"ts": r["ts"], "station": r["station_code"],
                           "kind": "DOWNTIME", "detail": detail})
        for r in db.query(
                """SELECT n.opened_at AS ts, s.station_code, n.ncr_no, n.defect_reason
                   FROM ncr_records n
                   JOIN stations s ON s.id = n.station_id
                   WHERE n.opened_by = ? ORDER BY n.id DESC LIMIT 15""", (mid,)):
            events.append({"ts": r["ts"], "station": r["station_code"], "kind": "NCR",
                           "detail": f"{r['ncr_no']}: {r['defect_reason']}"})
    for r in db.query(
            """SELECT n.created_at AS ts, s.station_code, n.level, n.message
               FROM notifications n LEFT JOIN stations s ON s.id = n.station_id
               WHERE n.category = 'MACHINE' ORDER BY n.id DESC LIMIT 15"""):
        events.append({"ts": r["ts"], "station": r["station_code"], "kind": "ALERT",
                       "detail": f"{r['level']}: {r['message']}"})
    events.sort(key=lambda e: str(e["ts"] or ""), reverse=True)
    events = events[:25]

    today_events = 0
    if mid:
        for sql in (
                "SELECT COUNT(*) c FROM material_transactions "
                "WHERE operator_id = ? AND date(timestamp) = date('now')",
                "SELECT COUNT(*) c FROM quality_checks "
                "WHERE operator_id = ? AND date(timestamp) = date('now')",
                "SELECT COUNT(*) c FROM downtime_events "
                "WHERE started_by = ? AND date(started_at) = date('now')",
                "SELECT COUNT(*) c FROM ncr_records "
                "WHERE opened_by = ? AND date(opened_at) = date('now')"):
            today_events += db.one(sql, (mid,))["c"]
    today_events += db.one("SELECT COUNT(*) c FROM notifications "
                           "WHERE category = 'MACHINE' AND date(created_at) = date('now')")["c"]
    key_stats = db.one(
        """SELECT SUM(CASE WHEN api_key IS NOT NULL THEN 1 ELSE 0 END) AS keys_issued,
                  SUM(CASE WHEN api_key IS NOT NULL AND api_enabled = 1 THEN 1 ELSE 0 END)
                      AS keys_active,
                  MAX(api_last_seen) AS last_seen FROM station_comm""")
    return render_template("admin/api.html", cfg=cfg, stations=stations,
                           events=events, today_events=today_events,
                           key_stats=key_stats, base_url=request.host_url.rstrip("/"))

@app.route("/admin/api/save", methods=["POST"])
@role_required("ADMIN")
def admin_api_save():
    ConfigService.set_many({"api_enabled": request.form.get("api_enabled", "1")})
    flash("Machine API setting saved.", "success")
    return redirect(url_for("admin_api"))

# ------------------------------------------------------ configuration
@app.route("/admin/configuration")
@role_required("ADMIN")
def admin_configuration():
    cfg = ConfigService.all()
    station_rows = db.query(
        """SELECT s.id AS id, s.station_code, s.station_name, s.station_type, s.active,
                  sc.mes_enabled, sc.mes_url, sc.output_enabled, sc.output_driver,
                  sc.output_target, sc.api_key, sc.api_enabled, sc.api_last_seen
           FROM stations s LEFT JOIN station_comm sc ON sc.station_id = s.id
           ORDER BY s.station_code""")
    return render_template("admin/configuration.html", cfg=cfg, stations=station_rows,
                           mes=mes_client.status_snapshot(), log=station_output.recent_log(),
                           drivers=station_output.DRIVERS)

@app.route("/admin/configuration/save", methods=["POST"])
@role_required("ADMIN")
def admin_configuration_save():
    section = request.form.get("section", "")
    if section == "global":
        fields = ["mes_enabled", "mes_base_url", "mes_auth_header", "mes_auth_token",
                  "mes_timeout", "mes_retries", "mes_interval", "mes_stations",
                  "out_enabled", "out_driver", "out_host", "out_port", "out_unit",
                  "out_address", "out_serial_port", "out_baud", "out_message",
                  "dashboard_refresh", "api_enabled"]
        ConfigService.set_many({k: request.form.get(k, "") for k in fields})
        if ConfigService.get_bool("mes_enabled") and not netguard.offline_mode():
            mes_client.start()
        else:
            mes_client.stop()
        if netguard.offline_mode():
            flash("Offline mode is ON — no outbound calls will be made.", "success")
        flash("Global integration settings saved.", "success")
    elif section == "routing":
        fields = ["route_enforce", "route_alert_on_violation",
                  "route_halt_on_violation", "route_require_product"]
        ConfigService.set_many({k: request.form.get(k, "0") for k in fields})
        flash("Routing enforcement settings saved.", "success")
    elif section == "retest":
        try:
            max_retests = max(0, int(request.form.get("qc_retest_max", "2")))
        except (TypeError, ValueError):
            max_retests = 2
        try:
            window_min = max(0, int(request.form.get("qc_retest_window_min", "0")))
        except (TypeError, ValueError):
            window_min = 0
        ConfigService.set_many({
            "qc_retest_enabled": request.form.get("qc_retest_enabled", "0"),
            "qc_retest_max": max_retests,
            "qc_retest_window_min": window_min,
        })
        flash("QC retest rules saved.", "success")
    elif section == "station":
        stations = db.query("SELECT id, station_code FROM stations")
        for st in stations:
            mes_url = request.form.get(f"mes_url_{st['id']}", "").strip()
            target = request.form.get(f"out_target_{st['id']}", "").strip()
            ConfigService.set_station_comm(
                st["id"],
                output_enabled=1 if request.form.get(f"out_enabled_{st['id']}") else 0,
                output_driver=request.form.get(f"out_driver_{st['id']}", "none"),
                output_target=target,
                mes_enabled=1 if request.form.get(f"mes_enabled_{st['id']}") else 0,
                mes_url=mes_url,
            )
        flash("Station communication settings saved.", "success")
    return redirect(url_for("admin_configuration"))

@app.route("/admin/configuration/test-mes", methods=["POST"])
@role_required("ADMIN")
def admin_configuration_test_mes():
    ok, err = mes_client.test_connection()
    flash("MES connection OK." if ok else f"Test failed: {err}", "success" if ok else "error")
    return redirect(url_for("admin_configuration"))

@app.route("/admin/configuration/test-out", methods=["POST"])
@role_required("ADMIN")
def admin_configuration_test_out():
    driver = request.form.get("out_driver", "log")
    target = request.form.get("out_target_t", "").strip()
    if not target:
        if driver == "modbus_rtu":
            target = ConfigService.get("out_serial_port", "COM1")
        else:
            target = f"{ConfigService.get('out_host', '192.168.0.10')}:{ConfigService.get_int('out_port', 502)}"
    ok, msg = station_output.test_connection(driver, target)
    flash("Controller output OK." if ok else f"Test failed: {msg}", "success" if ok else "error")
    return redirect(url_for("admin_configuration"))

@app.route("/admin/api/agent")
@role_required("ADMIN")
def admin_api_agent_download():
    return send_file(str(Path(__file__).resolve().parent / "station_agent.py"),
                     as_attachment=True, download_name="station_agent.py")

@app.route("/admin/api/agent/<int:station_id>.json")
@role_required("ADMIN")
def admin_api_agent_config(station_id):
    row = db.one(
        """SELECT s.station_code, sc.api_key FROM stations s
           LEFT JOIN station_comm sc ON sc.station_id = s.id WHERE s.id = ?""",
        (station_id,))
    if not row:
        abort(404)
    cfg = {
        "url": request.host_url.rstrip("/") + "/api/v1",
        "api_key": row["api_key"] or "",
        "station_code": row["station_code"],
        "heartbeat_seconds": 30,
        "flush_seconds": 5,
        "timeout": 10,
        "max_attempts": 50,
        "queue": "data/station_agent_outbox.db",
    }
    return Response(
        json.dumps(cfg, indent=2), mimetype="application/json",
        headers={"Content-Disposition":
                 f'attachment; filename="station_agent_{row["station_code"]}.json"'})

# ------------------------------------------------------ machine API keys
@app.route("/admin/api/station", methods=["POST"])
@role_required("ADMIN")
def admin_api_station():
    action = request.form.get("action", "save")
    station_id = request.form.get("station_id", type=int)
    row = db.one("SELECT id, station_code FROM stations WHERE id = ?", (station_id,))
    if not row:
        flash("Station not found.", "error")
        return redirect(url_for("admin_api"))
    ConfigService.station_comm(station_id)
    if action == "regen":
        key = "mts_" + secrets.token_hex(16)
        db.execute("UPDATE station_comm SET api_key = ?, updated_at = CURRENT_TIMESTAMP "
                   "WHERE station_id = ?", (key, station_id))
        flash(f"New API key generated for {row['station_code']} — copy it into the "
              "machine now. Anyone with this key can push events as this station.", "success")
    elif action == "clear":
        db.execute("UPDATE station_comm SET api_key = NULL, updated_at = CURRENT_TIMESTAMP "
                   "WHERE station_id = ?", (station_id,))
        flash(f"API key removed for {row['station_code']}. That station can no longer "
              "call the machine API.", "success")
    else:
        enabled = 1 if request.form.get("api_enabled") else 0
        db.execute("UPDATE station_comm SET api_enabled = ?, updated_at = CURRENT_TIMESTAMP "
                   "WHERE station_id = ?", (enabled, station_id))
        flash(f"Machine API access for {row['station_code']} "
              f"{'enabled' if enabled else 'disabled'}.", "success")
    return redirect(url_for("admin_api"))

# ------------------------------------------------------ backup & reset
@app.route("/admin/dbmaint/backup", methods=["POST"])
@role_required("ADMIN")
def admin_dbmaint_backup():
    b = AdminService().backup()
    if b:
        flash(f"Backup created: {b['name']} ({b['size']:,} bytes).", "success")
    else:
        flash("Backup failed.", "error")
    return redirect(url_for("admin_page", page="dbmaint"))

@app.route("/admin/dbmaint/download/<path:name>")
@role_required("ADMIN")
def admin_dbmaint_download(name):
    safe = Path(name).name
    p = BACKUP_DIR / safe
    if not p.is_file():
        abort(404)
    return send_file(BytesIO(p.read_bytes()), as_attachment=True, download_name=safe,
                     mimetype="application/octet-stream")

@app.route("/admin/dbmaint/delete/<path:name>", methods=["POST"])
@role_required("ADMIN")
def admin_dbmaint_delete(name):
    if AdminService().delete_backup(Path(name).name):
        flash("Backup deleted.", "success")
    else:
        flash("Backup not found.", "error")
    return redirect(url_for("admin_page", page="dbmaint"))

@app.route("/admin/dbmaint/reset", methods=["POST"])
@role_required("ADMIN")
def admin_dbmaint_reset():
    confirm = request.form.get("confirm", "").strip()
    if confirm.upper() != "RESET":
        flash("Reset cancelled - type RESET into the box to confirm.", "error")
        return redirect(url_for("admin_page", page="dbmaint"))
    AdminService().reset_all()
    flash("All production data reset to the clean demo state.", "success")
    return redirect(url_for("login"))

# ------------------------------------------------------ genealogy & recall
@app.route("/admin/genealogy/export")
@role_required("ADMIN", "VIEWER")
def genealogy_export():
    f = {k: request.args.get(k, "").strip()
         for k in ("material", "vendor", "batch", "mo", "date_from", "date_to")}
    if not any(f.values()):
        flash("Run a recall search first, then export the results.", "warn")
        return redirect(url_for("admin_page", page="genealogy"))
    rows = GenealogyService().recall_search(f, limit=5000)
    cols = GenealogyService.recall_export_columns()
    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    payload = report_service.export_csv(rows, cols)
    data = BytesIO(payload.encode("utf-8-sig"))
    return send_file(data, mimetype="text/csv", as_attachment=True,
                     download_name=f"mts_recall_{stamp}.csv")

# ------------------------------------------------------ downtime & OEE
@app.route("/admin/downtime/start", methods=["POST"])
@role_required("ADMIN")
def downtime_start_admin():
    station_id = request.form.get("station_id", type=int)
    reason_id = request.form.get("reason_id", type=int) or None
    notes = request.form.get("notes", "")
    ok, payload = DowntimeService().start(station_id, reason_id,
                                          operator_id=session["user"]["id"], notes=notes)
    if ok:
        flash("Stoppage started.", "warn")
    else:
        flash(f"Stoppage not started: {payload['reason_label'] if isinstance(payload, dict) else payload}.",
              "error")
    return redirect(url_for("admin_page", page="downtime"))

@app.route("/admin/downtime/end", methods=["POST"])
@role_required("ADMIN")
def downtime_end_admin():
    ok, msg = DowntimeService().end(request.form.get("event_id", type=int),
                                    operator_id=session["user"]["id"])
    flash(msg, "success" if ok else "error")
    return redirect(url_for("admin_page", page="downtime"))

@app.route("/admin/downtime/reason", methods=["POST"])
@role_required("ADMIN")
def downtime_reason_action():
    action = request.form.get("action", "")
    svc = DowntimeService()
    if action == "add":
        ok, msg = svc.add_reason(request.form.get("code", ""),
                                 request.form.get("label", ""))
    elif action == "toggle":
        svc.toggle_reason(request.form.get("reason_id", type=int))
        ok, msg = True, "Reason updated."
    else:
        ok, msg = svc.delete_reason(request.form.get("reason_id", type=int))
    flash(msg, "success" if ok else "error")
    return redirect(url_for("admin_page", page="downtime"))

@app.route("/admin/downtime/cycle", methods=["POST"])
@role_required("ADMIN")
def downtime_cycle_save():
    code = request.form.get("station_code", "").strip()
    minutes = request.form.get("cycle", "").strip()
    if code:
        try:
            DowntimeService().set_cycle(code, max(0.0, float(minutes or 0)))
            flash(f"Ideal cycle time for {code} saved.", "success")
        except ValueError:
            flash("Cycle time must be a number (minutes per unit).", "error")
    return redirect(url_for("admin_page", page="downtime"))

# ------------------------------------------------------ NCR / rework
@app.route("/admin/ncr/open", methods=["POST"])
@role_required("ADMIN")
def ncr_open():
    ok, payload = NCRService().open_ncr(
        source=request.form.get("source", "MANUAL"),
        defect_reason=request.form.get("defect_reason", ""),
        description=request.form.get("description", ""),
        serial_number=request.form.get("serial_number", "").strip() or None,
        station_id=request.form.get("station_id", type=int) or None,
        operator_id=session["user"]["id"])
    if ok:
        flash(f"{payload['ncr_no']} opened.", "success")
    else:
        flash(payload, "error")
    return redirect(url_for("admin_page", page="ncr"))

@app.route("/admin/ncr/<int:ncr_id>/close", methods=["POST"])
@role_required("ADMIN")
def ncr_close(ncr_id):
    ok, msg = NCRService().close(
        ncr_id,
        request.form.get("disposition", ""),
        operator_id=session["user"]["id"],
        rework_station_id=request.form.get("rework_station_id", type=int) or None,
        notes=request.form.get("notes", ""))
    flash(msg, "success" if ok else "error")
    return redirect(url_for("admin_page", page="ncr"))

# ------------------------------------------------------ operator stoppage
@app.route("/operator/downtime/start", methods=["POST"])
@role_required("OPERATOR", "ADMIN")
def operator_downtime_start():
    station_id = session["user"].get("station_id")
    if not station_id:
        flash("No station assigned to your user.", "error")
        return redirect(url_for("operator"))
    reason_id = request.form.get("reason_id", type=int) or None
    ok, payload = DowntimeService().start(
        station_id, reason_id, operator_id=session["user"]["id"],
        notes=request.form.get("notes", ""))
    if ok:
        flash("Stoppage logged. Resume when the station is running again.", "warn")
    else:
        flash("This station already has an open stoppage.", "error")
    return redirect(url_for("operator"))

@app.route("/operator/downtime/end", methods=["POST"])
@role_required("OPERATOR", "ADMIN")
def operator_downtime_end():
    station_id = session["user"].get("station_id")
    ok, msg = DowntimeService().end_for_station(station_id, session["user"]["id"])
    flash(msg, "success" if ok else "error")
    return redirect(url_for("operator"))


# ------------------------------------------------------ Andon board
@app.route("/andon")
@login_required
def andon():
    return render_template("andon.html", kiosk=True,
                           refresh=ConfigService.get_int("dashboard_refresh", 10))


@app.route("/api/docs")
def api_docs():
    return render_template("api_docs.html")


@app.route("/api/andon")
@app.route("/api/dashboard/station-flow")
@login_required
def api_andon():
    try:
        rows = db.query(
            """SELECT s.id, s.station_code, s.station_name, s.line_code, s.stage_code,
                      s.station_type, COALESCE(s.halted,0) as halted,
                      (SELECT sn2.serial_number FROM material_transactions t2
                         JOIN serial_numbers sn2 ON sn2.id=t2.serial_id
                         WHERE t2.station_id=s.id ORDER BY t2.id DESC LIMIT 1) as last_sn,
                      (SELECT t3.timestamp FROM material_transactions t3
                         WHERE t3.station_id=s.id ORDER BY t3.id DESC LIMIT 1) as last_at,
                      (SELECT q3.decision FROM quality_checks q3
                         WHERE q3.station_id=s.id ORDER BY q3.id DESC LIMIT 1) as last_qc,
                      (SELECT c3.api_last_seen FROM station_comm c3
                         WHERE c3.station_id=s.id) as api_last_seen
               FROM stations s WHERE s.active=1
               ORDER BY COALESCE(s.line_code,''), s.station_code""")
        out = []
        for r in rows:
            out.append({
                'id': r['id'], 'station_code': r['station_code'], 'station_name': r['station_name'],
                'line_code': r['line_code'], 'stage_code': r['stage_code'] or r['station_code'],
                'station_type': r['station_type'], 'halted': bool(r['halted']),
                'last_sn': r['last_sn'], 'last_at': _fmt_dt(r['last_at']), 'last_qc': r['last_qc'],
                'api_last_seen': _fmt_dt(r['api_last_seen']),
            })
        return jsonify({'ok': True, 'stations': out,
                        'open_downtime': DowntimeService().open_events(),
                        'alerts': notification_service.recent(10),
                        'ts': datetime.now().strftime('%Y-%m-%d %H:%M:%S')})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route("/api/notifications")
@login_required
def api_notifications():
    return jsonify({'ok': True, 'unread': notification_service.unread_count(),
                    'items': notification_service.recent(20)})


@app.route("/notifications/read", methods=["POST"])
@login_required
def notifications_read():
    notification_service.mark_read()
    return redirect(request.referrer or url_for("dashboard"))


def create_app():
    """Compatibility factory: routes are registered on the module-level app."""
    return app


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
