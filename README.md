# Material Tracking System

> **New to MTS?** Start with the [User and Technical Guide](./PROJECT_GUIDE.md) for
> installation, operator and administrator workflows, integrations, architecture, and
> maintenance. This README remains the detailed feature and API reference.

Lightweight offline material traceability / Poka-Yoke app for manufacturing. An operator scans a serial number, the system resolves the MO and the materials required at that station, then each material barcode is validated against its configured rule (exact match) before being recorded. Administrators configure stations, materials, MOs, station-wise material assignments, serial numbers and users.

Two UIs share the same backend service layer and SQLite database (`data/mts.db`):

- `flask_app.py` — **Flask** app with a dark "smart industry" command-center web GUI (recommended)
- `app.py` — **Streamlit** app (`streamlit run app.py`)

## Features

- Role-based access: `ADMIN`, `OPERATOR`, `VIEWER`
- **Live dashboard** (`/dashboard` or sidebar) — KPIs, per-station activity, work in
  process and recent scans; auto-refreshes in the web UI and polls on demand in Streamlit
- **Reports** (ADMIN/VIEWER → Reports) — traceability ledger, serial ledger, station
  summary, QC audit and exception reports with filters and **CSV / XLSX export**
- **MES integration** — every scan / QC decision is pushed **asynchronously** (background
  worker + outbox with retries/backoff) to a master MES via REST, station-wise and fully
  configurable in ADMIN → Configuration. JSON payload per event includes
  `serial_number`, `material_code`, `operator`, `operator_code`, `code` (barcode),
  `station_code`, `mo_number`, `result`/`decision` and `timestamp`. A built-in
  `POST /api/demo-mes` receiver records payloads for testing without a real MES
- **Machine REST API (this system as MES server)** — machines authenticate with a
  **per-station API key** and push scans, QC, downtime, NCRs, alerts and heartbeats to
  `/api/v1/events/*`; they can also query serial status, station work, MOs and materials.
  Interactive offline docs at `/api/docs`, OpenAPI 3.0.3 at `/api/v1/openapi.json`, keys
  managed per station in ADMIN → Configuration. A stdlib-only **station agent**
  (`station_agent.py`, downloadable from that page) runs on each machine PC with an offline
  outbox + auto-retry and a built-in *automatic station* simulation mode
- **Station controller output** — forward every result to the station controller over
  **RS485 / Modbus RTU**, **Modbus TCP**, **raw TCP or UDP** (configurable per station,
  with a `log` driver for testing). PASS drives the configured coil/message ON, any
  FAIL/HOLD/REJECTED drives it OFF. Communication failures never block production
- **Station master**: admins define stations with a full set of shop-floor attributes —
  type, **stage code**, line, work center, required skill, cycle time, machine/scanner id,
  route order and description (`Stations` page). Station **types** are a managed catalogue
  (`Station Types` page: code, label, QC gate flag, requires-material flag); QC behaviour
  is data-driven from this catalogue rather than a hard-coded type name
- **Route control**: a serial cannot be scanned at a routing stage until all earlier
  stages are complete, can never move backwards, and a station that is not part of the
  MO's route cannot process its serials. Scanning a serial at a **halted** station is
  blocked until an admin clears it
- **Product master + reusable routes**: each **product** (`Products` page) owns an ordered
  route of stages (station + stage code). An MO that references a product inherits that
  route automatically; a per-MO route can still be pinned. Route resolution precedence is
  **MO route → product route → global default order**, shown on the MO Master page
- **Per-MO route (MO Master)**: every released MO can get its own ordered station route with
  per-stage codes (`MO Master` page), or **copy the linked product's route** in one click.
  The default route is derived automatically (assigned stations + all active QC stations by
  route order). The Material Assignment page only offers stations in the selected MO's route,
  so a line can run multiple MOs mixed together
- **Route enforcement**: configurable (`Configuration → Routing & stage enforcement`; keys
  `route_enforce`, `route_alert_on_violation`, `route_halt_on_violation`, `route_require_product`).
  A non-conforming scan is rejected, raises a CRIT **Andon** alert (`ROUTE_VIOLATION`) and, if
  configured, **halts the station** until an admin releases it (`Stations` page → *Clear halt*)
- **Quality (QC) stations**: a station can be typed `WORK` or `QC` (or any catalogue type
  flagged as a QC gate). QC stations are inspection gates — scanning a serial there presents
  **PASS** / **FAIL** (`FAIL → HOLD` or `FAIL → REJECTED`) with an optional reason. A `PASS`
  lets the unit continue (and clears a previous `HOLD`); a `REJECTED` serial is permanently
  blocked. A serial only becomes `COMPLETED` after the whole route — including QC — is done.
  Inspections are recorded in the admin **QC Checks** page
- **QC / test-station retest rules** (configurable in `Configuration → QC / test-station retest
  rules`; keys `qc_retest_enabled`, `qc_retest_max`, `qc_retest_window_min`). A unit that fails a
  QC/test station may be **retested up to `qc_retest_max` extra times** (default 2). Any **PASS**
  within those retests becomes the final result; once every retest is used and the unit still
  fails it is **REJECTED**. When `qc_retest_window_min` > 0 each retest must be submitted within
  that many minutes of the previous attempt at the same station, otherwise the unit is rejected.
  Example with max=2: `FAIL, PASS` → passed; `FAIL, FAIL, PASS` → passed; `FAIL, FAIL, FAIL` →
  rejected. Enforced uniformly across the operator screen and every API (keyless + keyed).
- Admin full CRUD for every entity: **stations**, **station types**, **products**, **materials**, **MO**s, **material assignments**, **serial numbers** and **users** — add, edit and delete
- Safe deletes: entities in active use (assigned, scanned, or owning a station) are protected and a clear reason is shown
- **Backup & Reset** (admins only): create timestamped snapshots of the whole database in `backups/` and download or delete them, or wipe all production data (stations, serials, MOs, materials, users, history) and restore the clean demo state — guarded by typing `RESET` to confirm
- **Bulk upload from Excel/CSV** for stations, materials and serial numbers, with downloadable format templates
- Station-wise material assignment against each MO; a serial always belongs to one MO
- Exact barcode validation per rule type:
  - pipe format e.g. `MAT-1001|VENDOR|BATCH` (exact segments, must include material code)
  - regex e.g. `^MT\d{4}$` (full match)
  - literal code, or blank = whole barcode must equal the material code
- Continuous scanning: after a scan the input stays focused for the next barcode
- Full audit: material transactions + exceptions log (rejected scans)
- SQLite database, no internet required, USB barcode scanners work (keyboard emulation)

## Manufacturing feature set (Flask GUI)

- **Genealogy & Recall** (`/admin/genealogy`, ADMIN + VIEWER): as-built tree for one
  serial (route progress, every material lot consumed with vendor/batch parsed from the
  barcode, QC history, NCRs) and the reverse **recall search** by material code /
  vendor / batch / MO / date range showing every impacted unit and its live status,
  with **CSV export** of the affected list
- **Downtime & OEE** (`/admin/downtime`, ADMIN + VIEWER): operators log stoppages with
  a reason from the operator screen (station banner + one-tap Resume); admins manage
  the reason catalogue and open stoppages; per-station **OEE** (Availability x
  Performance x Quality) over any date range with configurable planned time and ideal
  cycle time; stoppage ledger exported via Reports (`Downtime Ledger`)
- **Alerts & Andon**: a topbar alert bell (unread count, live feed, mark-all-read) fed
  by QC HOLD/REJECTED, QC release, scan rejections, stoppages, NCR events and
  permanent MES push failures; the full-screen **Andon Board** (`/andon`) shows live
  station states (RUNNING / DOWN / idle), 24h output, QC pass/fail, holds, open
  stoppages/NCRs and the alert ticker, auto-refreshing from `GET /api/andon`
- **NCR / Rework loop** (`/admin/ncr`, ADMIN + VIEWER): non-conformance records opened
  manually or in one click for any blocked unit (auto `NCR-YYYYMMDD-NNNN` numbers);
  disposition on close: **REWORK** returns the serial to `IN_PROCESS` (rework station
  recorded, rework count incremented), **SCRAP** keeps it `REJECTED`, **USE AS IS**
  releases it; NCR ledger exported via Reports (`NCR Ledger`)

New tables (created automatically on first start): `downtime_reasons`,
`downtime_events`, `notifications`, `ncr_records`.

## Quick start (Flask)

```
pip install -r requirements.txt
python flask_app.py
```

Open http://127.0.0.1:5000

Demo logins (created automatically in the demo database):

| Username | Password | Role   |
|----------|----------|--------|
| admin    | admin    | ADMIN  |
| op1      | pass1    | OPERATOR (station ST1) |

## Running on your LAN (double-click)

Run **`start_server.bat`** — it starts the app with the **Waitress** production server on
`0.0.0.0:5000` and prints the URLs. Other PCs on the same network open
`http://<this-PC-IP>:5000` (e.g. `http://192.168.1.50:5000`) and log in normally.

Run **`start_streamlit.bat`** for the Streamlit UI on `0.0.0.0:8501` instead.

## Reports

ADMIN/VIEWER → **Reports**. Choose a report type and filter by MO / serial / station /
date range, then export **CSV** or **XLSX**:

| Report | Contents |
|--------|----------|
| Traceability Ledger | every scan: time, station, serial, MO, material, barcode, operator, result |
| Serial Ledger | per serial: MO, status, block state, scan count, QC fails, activity window |
| Station Summary | activity per station: scans, serials, operators, route order |
| QC Audit Ledger | every inspection: decision, fail status, reason, inspector |
| Exception Ledger | every rejected scan with expected vs scanned value |

## MES integration

ADMIN → **Configuration** → *MES push*.

1. Enable MES push, set the **base URL** (e.g. `http://mes-server:5000/api/v1/trace`),
   optional auth header/token, timeout, retries and poll interval.
2. Optionally restrict to specific station codes (empty = all stations) and give a single
   station its own endpoint under *Station-wise communication*.
3. Every material scan and QC decision is queued and POSTed by the background worker.
   `mes_outbox` records each delivery (pending/sent/failed), so nothing is lost on a
   network blip — failed rows are retried with backoff and visible on the page.

Sample payload (one JSON object, async POST, `application/json`):

```json
{
  "event_type": "material_recorded",
  "station_code": "ST1", "station_name": "Station One", "station_type": "WORK",
  "mo_number": "MO-2026-001", "serial_number": "SN1001",
  "material_code": "MAT-1001", "barcode": "MAT-1001|VEN1|B1", "code": "MAT-1001|VEN1|B1",
  "operator": "Operator One", "operator_code": "op1",
  "result": "IN_PROCESS", "decision": "",
  "timestamp": "2026-08-31T05:10:00"
}
```

To simulate a MES locally for testing, POST to the built-in receiver
`http://<host>:5000/api/demo-mes` (records every payload, viewable on the Configuration page).

## Machine REST API (inbound)

This system also acts as the **MES server** for the machines themselves: station PCs, PLC
gateways and robot controllers call REST endpoints with a **per-station API key**, and the
events they send appear immediately in the GUI (dashboard, Andon, genealogy, NCR, downtime).

ADMIN → **API** (sidebar; `Configuration` links to it):

1. Global switch **Machine API enabled** (config `api_enabled`, default on).
2. Per station: **Generate key** (`mts_…`, 32 hex chars), enable/disable API access, see the
   station's **last machine call**, and download the pre-filled agent config.
3. Interactive docs at **`/api/docs`** (offline, no CDN) and machine-readable spec at
   **`/api/v1/openapi.json`** — both public, so a machine integration can be built from them.
4. KPI row + **recent machine events** table (scans, QC, downtime, NCRs, alerts received over
   the API), plus a **Download `station_agent.py`** link.

Authentication — send the key one of these ways (validated on every call, station must be
active and API-enabled, and `api_last_seen` is updated). Machine events run through the same
validation and **route enforcement** as the operator screen, so a scan at a halted station or
out of routing sequence is rejected even over the API:

```
X-API-Key: mts_...               (preferred)
Authorization: Bearer mts_...    (also accepted)
?api_key=mts_...                 (GET only, for quick browser tests)
```

Every response is JSON; failures are `{"ok": false, "error": "…"}` with HTTP `400`
(validation / route enforcement), `401` (missing key), `403` (bad/disabled key or inactive
station), `404` (unknown serial), `409` (conflict), `503` (API disabled server-wide).

| Method | Endpoint | Purpose |
|--------|----------|---------|
| GET  | `/api/v1/health` | liveness + version (no auth) |
| GET  | `/api/v1/openapi.json` | OpenAPI 3.0.3 spec |
| POST | `/api/v1/events/heartbeat` | keep-alive; returns station state, **stage code** + **halted** flag, open stoppage |
| POST | `/api/v1/events/material_scan` | record a barcode scan (same validation + route enforcement as the operator screen; rejected when halted or out of sequence) |
| POST | `/api/v1/events/qc` | QC decision **PASS** / **FAIL** (`fail_status` HOLD or REJECTED) — allowed at any QC-catalogue station |
| POST | `/api/v1/events/downtime` | machine-driven stoppage `start` / `end` (optional `reason_code`, `notes`) |
| POST | `/api/v1/events/ncr` | raise a non-conformance (optional `serial_number`) |
| POST | `/api/v1/events/alert` | push an alarm into the MES feed (bell + Andon ticker) |
| GET  | `/api/v1/stations/me` | calling station profile (incl. `stage_code`, `halted`), work assignments by MO, recent scans |
| GET  | `/api/v1/serials/<serial_number>` | status, route progress, pending work at this station, QC/NCR history |
| GET  | `/api/v1/mos` | manufacturing orders with progress |
| GET  | `/api/v1/materials` | material master incl. barcode rules |
| GET  | `/api/v1/downtime/reasons` | active stoppage reason codes |
| GET  | `/api/v1/notifications` | recent MES alerts for machine displays |

Example — a machine at `ST10` records a material scan:

```bash
curl -X POST http://<host>:5000/api/v1/events/material_scan \
  -H "X-API-Key: mts_..." -H "Content-Type: application/json" \
  -d '{"serial_number": "SN1001", "material_barcode": "MAT-05"}'
```

```json
{"ok": true, "transaction_id": 512, "serial_number": "SN1001", "material_code": "MAT-05",
 "remaining": ["MAT-06"], "station_complete": false, "route_complete": false,
 "serial_status": "IN_PROCESS"}
```

Machine events are attributed to the automatically seeded **`machine` service user**
(role OPERATOR, shown as "Machine API (system)"); it is hidden from the Users page. Rotating
or removing a key on the Configuration page immediately cuts that station's access.

### Station agent (automatic station)

`station_agent.py` is a **stdlib-only** client you copy to any machine PC / PLC gateway
(Python 3.8+, no `pip install`). It pushes data to the REST API and keeps working through
network blips with a local SQLite outbox.

Download it from the **API** admin page → **Download station_agent.py**, and the
per-station pre-filled config via **Download agent config** (contains URL + that station's
key). Then on the station PC:

```bash
python station_agent.py run                 # daemon: heartbeat + flush offline queue
python station_agent.py scan  --serial SN1001 --barcode MAT-05
python station_agent.py qc    --serial SN1001 --decision FAIL --fail-status HOLD --reason "burr"
python station_agent.py downtime start --reason CHANGEOVER --notes "tool change"
python station_agent.py downtime end
python station_agent.py ncr   --reason "dimensional out of spec" --serial SN1001
python station_agent.py alert --message "conveyor jam" --level CRIT
python station_agent.py status              # station profile + work queue
python station_agent.py show  --serial SN1001
```

- **`run`** sends a heartbeat every `heartbeat_seconds` and flushes the outbox every
  `flush_seconds`; everything is also printed to the console.
- Events that fail with a network error / 5xx / 401 / 403 / 503 are **queued and retried**
  (up to `max_attempts`); permanent rejections (400/404/409) are reported and dropped, so a
  bad barcode is never silently replayed forever.
- **`simulate`** turns any station into an *automatic station demo*: it reads the work queue
  from `/api/v1/stations/me`, scans the pending materials with correctly built barcodes, and
  occasionally sends alerts / stoppages — useful to see the whole pipeline live:
  `python station_agent.py simulate --interval 3 --count 20`.

Config file `station_agent.json` (next to the script or `--config PATH` / `MTS_AGENT_CONFIG`):

```json
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
```

`--url`, `--key` and `--station` override the file, so a single command can be tested
without a config file.

## Integration REST API (keyless, LAN-only)

For simple local line/machine integration there is a second, **open** API group under
**`/api/integration`** that requires **no API key** — it is meant to be called by machines,
PLC gateways and line controllers on the same trusted LAN. Caller identity is taken from the
`station_code` sent in the request. Every response is JSON; failures are
`{"ok": false, "error": "…"}`.

Docs page: `/api/docs` (section "Keyless integration API") · machine-readable spec:
`/api/integration/openapi.json`.

| # | Method | Endpoint | Purpose |
|---|--------|----------|---------|
| 1 | GET  | `/api/integration/health` | health check — server alive + database `UP`/`DOWN`, version, server time |
| 2 | GET  | `/api/integration/mos/<mo_number>` | get a manufacturing order by MO number (status, planned qty, serial counts) |
| 3 | GET  | `/api/integration/mos/<mo_number>/serials` | serial numbers as a batch against the MO; filters `status`, `limit` (≤1000), `offset` |
| 4 | GET  | `/api/integration/serials/<serial_number>` | unit status: serial + MO, route progress, last scan, QC fails, open NCRs; add `?station_code=` to also get pending materials / QC state for a station |
| 5 | POST | `/api/integration/process-status` | update process status reported by a machine for a unit |

Examples:

```bash
# 1 - health
curl http://<host>:5000/api/integration/health
# {"ok": true, "service": "mts-integration-api", "version": "1.0.0",
#  "server_time": "2026-10-20 17:31:28", "database": "UP"}

# 2 - get MO
curl http://<host>:5000/api/integration/mos/MO-12

# 3 - serial batch for the MO
curl "http://<host>:5000/api/integration/mos/MO-12/serials?status=IN_PROCESS&limit=50"

# 4 - unit status (optionally scoped to a station)
curl "http://<host>:5000/api/integration/serials/SN-1?station_code=ST10"

# 5 - update process status
curl -X POST http://<host>:5000/api/integration/process-status \
  -H "Content-Type: application/json" \
  -d '{"serial_number":"SN-1001","station_code":"ST10","status":"IN_PROCESS",
       "line_code":"LINE-01","cycle_time":12.5,
       "parameters":[{"Key":"Torque","value":4.85,"unit":"Nm","LSL":4.5,"USL":5.2}],
       "childParts":[{"PartBarcode1":"MAT-06"},{"PartBarcode2":"MAT-05"}]}'
```

**`POST /api/integration/process-status`** body (single call for machine integration):

- **Required:** `serial_number`, `station_code`, `status`.
- **`status`** = `IN_PROCESS` / `PASS` records a production trace at a **work** station
  (optional `parameters[]`); `FAIL` records a failed trace and puts the unit on **HOLD**
  (or `REJECTED` when `fail_status: "REJECTED"`). At a **QC station**, `PASS` completes the
  unit and `FAIL` applies the QC hold/reject.
- **Optional:** `stage_code`, `line_code`, `operator_code`, `fixture_id`, `sw_version`,
  `hw_version`, `cycle_time`, `material_barcode`, and `parameters[]` with
  `Key` / `value` / `unit` / `LSL` / `USL`.
- **`childParts[]`** — components installed into the parent unit at this station. **Always
  included; send `[]` (empty) when there are no child parts.** Each entry may be:
  - a **dynamic-key barcode object** `{"PartBarcode1": "MAT-06"}` — the value is the part
    **barcode/material code**; when it matches a material assigned at the station it is
    **consumed** as a material scan, and it is always stored as a child binding;
  - an object `{"serial_number": …, "material_code": …, "qty": …}` (child serial);
  - a plain child serial string `"SN-1003"`.
  Bound children are returned (with status) under `child_parts` by
  `GET /api/integration/serials/<serial>`. The response reports
  `child_parts_consumed` and `child_parts_unmatched`.
- Runs through the same validation and **route enforcement** as the operator screen, and is
  attributed to the seeded `machine` service user.
- **Duplicate-scan protection:** a serial may be processed **only once per station**. A second
  trace (`/api/v1/events/trace` or `process-status` `IN_PROCESS`) for the same serial at the
  same station returns **409**. A WORK station still accepts repeat serial scans while assigned
  materials remain pending (legitimate multi-material scanning); a QC station blocks a repeat
  **PASS** but allows a retest after a **FAIL → HOLD**.

Because this group is **unauthenticated**, only expose it on a trusted LAN. The key-protected
`/api/v1` machine API above remains available for internet-adjacent / stricter deployments.

## Machine / edge-gateway contract (keyless, `/api/v1`)

Six additional **unauthenticated** endpoints under `/api/v1` implement the simpler machine
contract. Responses use the documented shapes (no `ok` envelope); errors are
`{"error": "…"}` with a `404` for unknown MOs/serials.

| # | Method | Endpoint | Purpose |
|---|--------|----------|---------|
| 1 | POST/GET | `/api/v1/heartbeat` | MES online check; acknowledges with `{status:"ACK", serverTime}` and (when `station_code` given) stamps the station alive |
| 2 | GET | `/api/v1/orders/<moNumber>/serials` | serials generated for a MO (`serialNumber`, `status` + `totalSerials`) |
| 3 | GET | `/api/v1/orders/<moNumber>/product` | product master linked to the MO (`productCode`, `productName`, `revision`) |
| 4 | GET | `/api/v1/units/<serialNumber>/status` | unit lifecycle state (`currentStatus`, `isScrapped`, `isRework`) before a machine touches it |
| 5 | GET | `/api/v1/units/<serialNumber>/routing-validation` | interlocking check — previous station status + `nextExpectedStationId`; add `?station_code=` to validate the caller's own position (read-only, never halts) |
| 6 | GET | `/api/v1/orders/active` | find active/released MOs by master criteria (`?lineId=`, `?productCode=`) |

```bash
# 1 - heartbeat
curl -X POST http://<host>:5000/api/v1/heartbeat -H "Content-Type: application/json" \
  -d '{"timestamp":"2026-10-20T21:02:00Z","status":"UP","currentWorkOrder":"MO-12","station_code":"ST10"}'

# 2 - serials for a MO
curl http://<host>:5000/api/v1/orders/MO-12/serials

# 3 - product for a MO
curl http://<host>:5000/api/v1/orders/MO-12/product

# 4 - unit status
curl http://<host>:5000/api/v1/units/SN-1/status

# 5 - routing validation (which station is asking?)
curl "http://<host>:5000/api/v1/units/SN-1/routing-validation?station_code=ST20"

# 6 - active orders by line / product
curl "http://<host>:5000/api/v1/orders/active?lineId=LINE-01&productCode=PROD-WIDGET-X"
```

`isScrapped` maps from status `REJECTED`, `isRework` from status `HOLD`. In the active-orders
response `scheduleDate` is the MO creation date and `priority` is a fixed `1` (the schema has
no priority field yet).

## Station controller output


ADMIN → **Configuration** → *Controller output*. Driver per station (or global default):

| Driver        | Transport / notes                                  |
|---------------|----------------------------------------------------|
| `log`         | writes lines to `data/station_output.log` (testing) |
| `tcp` / `udp` | ASCII line to `host:port`                          |
| `modbus_tcp`  | Modbus/TCP coil write (IP:port + unit + coil address) |
| `modbus_rtu`  | Modbus/RTU over RS485 (COM port + baud + address)  |

The message template supports `{event}`, `{station_code}`, `{serial_number}`,
`{material_code}`, `{operator_code}` and `{result}`. `PASS`/`COMPLETED` → coil ON,
`FAIL`/`HOLD`/`REJECTED` → coil OFF. Use "Test controller output" to verify a transport
without affecting production.

## Typical operator flow

1. Operator signs in and is bound to the station assigned by the admin.
2. Scan the serial number → the system locks the serial and lists the required materials for that station/MO.
3. Scan each material barcode → exact-match validation; wrong/partial scans are rejected and logged as exceptions.
4. When the last required material is scanned the serial is marked `COMPLETED`.

## Quick start (Streamlit)

```
streamlit run app.py
```

## Bulk upload (Excel / CSV)

ADMIN → **Bulk Upload** in the sidebar. You must have at least one MO registered if you
upload serial numbers. Download the ready-made template from the page — it includes an
*Instructions* sheet — fill it in and upload (`.xlsx` or `.csv`). Duplicate rows are
skipped; invalid rows are reported with their row number.

| Kind | Columns (header row, lower-case with `_`) | Notes |
|------|------------------------------------------|-------|
| Stations | `station_code` (`required`), `station_name` | e.g. `ST3`, `Final Assembly`; station type/route order are set from the admin Stations page |
| Materials | `material_code` (`required`), `material_description`, `barcode_rule` | blank `barcode_rule` = whole barcode must equal the material code |
| Serial Numbers | `mo_number` (`required`), `serial_number` (`required`), `status` | `mo_number` must already exist; `status` is `AVAILABLE`, `IN_PROCESS`, `COMPLETED`, `HOLD` or `REJECTED` (blank = `AVAILABLE`) |

Example `serial_numbers` upload:

```
mo_number     | serial_number | status
MO-2026-001   | SN1003        |
MO-2026-001   | SN1004        | HOLD
```

## Typical operator flow

1. Operator signs in and is bound to the station assigned by the admin.
2. Scan the serial number → the system locks the serial and lists the required materials for that station/MO.
3. Scan each material barcode → exact-match validation; wrong/partial scans are rejected and logged as exceptions.
4. When the last required material is scanned the serial is marked `COMPLETED`.

## Barcode rules

Set a barcode format per material (or override per assignment). Examples:

- `MAT-1001|VENDOR|BATCH` → the scanned barcode must be exactly three parts separated by `|`, the first part must equal `MAT-1001`.
- `^MT\d{4}$` → full regex match.
- `MTR-123` → the whole barcode must equal `MTR-123`.
- blank → the whole barcode must equal the material code.

## Project layout

```
app.py                 Streamlit entry (use: streamlit run app.py)
flask_app.py           Flask entry (use: python flask_app.py)
machine_api.py         Machine REST API blueprint (/api/v1) + /api/docs
openapi_spec.py        OpenAPI 3.0.3 spec for the machine API
station_agent.py       station-side agent (stdlib only): heartbeat + offline queue + simulate
start_server.bat       Waitress LAN server launcher (0.0.0.0:5000)
start_streamlit.bat    Streamlit LAN launcher (0.0.0.0:8501)
auth/                  authentication (pbkdf2 hashing)
database/              SQLite helpers + schema
services/              barcode / validation / transaction / admin / product /
                       reports / dashboard / MES push (mes_client) / station
                       output / events + system_config services
ui/                    Streamlit screens + theme
templates/ static/     Flask dark command-center GUI (KPIs, LED status badges, SVG icons)
                       (station master, station types, products + product routes)
data/mts.db            SQLite database (created on first run)
```