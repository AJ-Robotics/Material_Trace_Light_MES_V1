# Material Tracking System — User and Technical Guide

This guide explains how to install, configure, and use the Material Tracking System
(MTS), and how its main technical components fit together. It is intended for operators,
administrators, line integrators, and developers.

For the complete feature inventory and API examples, see [README.md](./README.md).
For focused integration examples, see [API_TESTING.md](./API_TESTING.md). The running
Flask application also serves interactive machine API documentation at `/api/docs`.

## 1. What the system does

MTS is an offline-capable manufacturing traceability and Poka-Yoke application. It
records material consumption and quality decisions against serialised units, and checks
that a unit is processed at the correct station in its manufacturing order's route.

The Flask and Streamlit interfaces use the same service layer and SQLite database:

- Flask (`flask_app.py`) is the recommended web interface, including the manufacturing
  dashboard, Andon, administration, and machine integrations.
- Streamlit (`app.py`) provides an alternate interface for operator, dashboard, report,
  configuration, and administrative workflows.
- Machine integrations can post production events to Flask REST APIs. A separate
  `station_agent.py` client can queue events locally while disconnected.

Typical production flow:

1. An administrator configures station types, stations, products/routes, manufacturing
   orders (MOs), materials, serial numbers, assignments, and user accounts.
2. An operator signs in at their assigned station and scans a serial.
3. MTS checks the serial's MO, status, route position, station state, and required
   materials.
4. The operator scans required material barcodes. Accepted scans are recorded against
   the serial, MO, station, and operator. Invalid scans are rejected and audited.
5. At quality stations, the operator records a PASS or FAIL decision. Units can proceed
   only when the configured route and quality rules permit it.
6. Authorized users review live production, genealogy, exceptions, downtime, NCRs, and
   reports.

## 2. Installation and startup

### Requirements

- Windows is supported by the included batch launchers.
- Install Python 3.10 or newer for the app. The station-side `station_agent.py` itself
  supports Python 3.8 or newer and uses only the standard library.
- Install packages listed in `requirements.txt`. A network connection is needed to
  download packages if they are not already installed; normal operation can be offline.

### Run locally

From the project directory:

```powershell
python -m pip install -r requirements.txt
python flask_app.py
```

Open <http://127.0.0.1:5000>. The Flask development server is suitable for local testing;
use the Waitress launcher for a shared LAN installation.

Alternatively, run the Streamlit UI:

```powershell
python -m streamlit run app.py
```

Open <http://127.0.0.1:8501>. On Windows, `start_streamlit.bat` starts it on all network
interfaces at port 8501.

### Run on a trusted LAN

Double-click `start_server.bat` or run it from a terminal. It starts Waitress on
`0.0.0.0:5000` with eight worker threads and prints a local network URL. Users on the
same LAN open `http://<server-PC-IP>:5000`.

The Streamlit launcher also binds to all interfaces. Restrict network and firewall access
to intended users and devices. Do not expose the application or its unauthenticated
integration endpoints directly to the public internet.

### First login and initial setup

The README documents these demo credentials for a clean demo database:

| Username | Password | Role |
|---|---|---|
| `admin` | `admin` | ADMIN |
| `op1` | `pass1` | OPERATOR (ST1) |

Change demo passwords and create named user accounts before using the application for
production. The schema seeds the `admin` account on initialisation; availability of the
operator demo account depends on the database/demo state supplied with the installation.
If `op1` is not present, sign in as admin and create an operator in user administration.

Recommended initial configuration sequence:

1. Create or review station types in **Station Types**.
2. Create active shop-floor stations in **Stations**. Set station type, route/stage,
   line, work center, route order, cycle time, and any machine/scanner identifiers.
3. Create products and their reusable ordered routes in **Products** where applicable.
4. Create MOs in **MO Master**, link products if used, set the MO status to RELEASED, and
   review the effective route. Route precedence is MO-specific route, product route,
   then the default route.
5. Create materials and configure barcode rules.
6. Assign the required materials to each station and MO.
7. Add serial numbers under the correct MO.
8. Create user accounts and assign each operator to a station.
9. Configure routing, QC, integrations, and controller output only as needed.
10. Perform a test serial through the intended route and verify the transaction and
    reports before starting production.

## 3. Roles and access

The supported roles are:

| Role | Intended use |
|---|---|
| ADMIN | Production setup, user/configuration management, station control, backups, and all administrative workflows. |
| OPERATOR | Station-bound scanning, QC decisions, downtime reporting, and personal transaction history. |
| VIEWER | Read-only production visibility and reports, including supported genealogy, downtime, and NCR views. |

The Flask interface presents navigation according to role. The operator workflow is
station-bound; station assignment is managed by an administrator. Do not share accounts:
operator attribution is part of the traceability record.

## 4. Operator guide

### Material scanning

1. Sign in with the operator account assigned to the correct station.
2. Open **Operator Interface**.
3. Scan or enter the serial number. MTS resolves its MO and shows work required at the
   current station.
4. Scan each requested material barcode. A barcode scanner that types as a keyboard
   normally works without additional device software.
5. Wait for the acceptance or rejection message before scanning the next item. Accepted
   scans are recorded; rejected scans are audited as exceptions.
6. Continue with the next serial when the current station's required work is complete.

The scan field is designed for repeated scanning and normally stays focused. A serial
may need to visit other stations before reaching COMPLETED; completion is based on the
effective route, not just the material scans at one station.

### Quality inspection

At a station whose configured station type is marked as a QC gate, scan the serial and
record PASS or FAIL. A FAIL requires the appropriate configured outcome (HOLD or
REJECTED) and can include a reason.

- HOLD means the unit is blocked pending a permitted retest or disposition.
- REJECTED is a terminal blocked state unless an authorized NCR workflow explicitly
  changes its disposition.
- PASS allows the unit to continue and may release a prior HOLD.
- A unit reaches COMPLETED only after all required route stages, including QC gates, are
  complete.

QC retests are configurable. The default policy allows two additional attempts after a
failure; an optional time window can limit how long the next attempt may wait.

### Downtime

Use the operator downtime controls to select a reason and start a stoppage. Resume the
station when production can continue. Downtime entries contribute to the stoppage
ledger and station OEE reporting.

### Common operator messages

- **Wrong material / barcode rejected:** check the material assignment and scanned
  barcode against the configured rule; do not bypass the check.
- **Wrong station / route violation:** the serial has not reached this station, has
  already passed it, or the station is not part of the MO route. Ask a supervisor or
  administrator to check the route and station assignment.
- **Station halted:** stop scanning and notify an administrator. A halt is cleared from
  station administration after the cause is reviewed.
- **Serial on HOLD or REJECTED:** follow the site's quality/NCR process; do not create a
  replacement serial to bypass the block.

## 5. Administrator guide

### Master data and execution order

MTS configuration records are related. Set them up in this order to avoid incomplete
work definitions:

- **Station Types:** catalogue entries define whether a type is a QC gate and whether it
  consumes material.
- **Stations:** assign a unique station code and name, type, stage, route order, and
  optional line/machine details. An administrator can inspect and clear a station halt.
- **Products and routes:** define reusable product-specific route stages.
- **MO Master:** create an MO and, where applicable, link a product or create a
  per-MO route. The effective route resolves as MO route → product route → default
  station ordering.
- **Materials and assignments:** create material definitions, then assign each required
  material to the relevant station and MO. Assignment-level barcode rules can override
  the material's default rule.
- **Serials:** register serial numbers against an existing MO.
- **Users:** create accounts, select role, and bind operators to their station.

Route enforcement is enabled by default. A route violation can create a critical Andon
alert and halt the station according to configuration. A halt blocks operator and
machine API processing until administratively cleared.

### Barcode rule formats

Rules are checked against the full scanned value. Examples:

| Rule | Meaning |
|---|---|
| `MAT-1001\|VENDOR\|BATCH` | Pipe-delimited exact format; the first segment identifies the material and the required segments must match. |
| `^MT\d{4}$` | Regular expression, matched against the full barcode. |
| `MTR-123` | Literal exact barcode. |
| blank | The barcode must equal the material code. |

Use representative real scanner values to test rules before production.

### Uploads, reports, and traceability

- **Bulk Upload** accepts CSV or Excel for stations, materials, and serial numbers. Use
  the in-page template; duplicate rows are skipped and invalid rows are reported by row.
  Serial uploads require an existing MO.
- **Reports** supports filtered ledgers and CSV/XLSX export. Available report families
  include traceability, serial, station, QC, exception, downtime, and NCR records.
- **Genealogy & Recall** traces a serial's route, consumed lots, QC history, and NCRs;
  reverse search identifies units affected by a material, vendor, batch, MO, or date
  range.
- **Andon** provides station state, output, quality/stoppage indicators, and alert feed.
- **NCR** records can be opened manually or from supported quality/blocked-unit events.
  Closing dispositions include REWORK, SCRAP, and USE AS IS; follow local quality
  approval procedures.

### Backups and reset

Use the administrator **Backup & Reset** workflow to create and download a timestamped
database snapshot. Store backups securely and test restoration procedures periodically.
The reset action deletes production data and restores a demo state; it requires typing
`RESET`. Treat reset as destructive and take a backup first.

The active database is `data/mts.db`; snapshots are stored in `backups/`. Do not replace,
delete, or copy a live SQLite database while the application is writing to it.

## 6. Configuration and integrations

### System defaults

Configuration is persisted in the `system_config` SQLite table. Defaults in
`services/system_config.py` include:

| Setting | Default | Description |
|---|---:|---|
| `offline_mode` | enabled | Prevents outbound network calls. |
| `mes_enabled` | disabled | Enables asynchronous MES event delivery when configured. |
| `api_enabled` | enabled | Enables the keyed machine API. |
| `route_enforce` | enabled | Enforces route order and station membership. |
| `route_alert_on_violation` | enabled | Raises an alert on route violations. |
| `route_halt_on_violation` | enabled | Halts the station on a route violation. |
| `route_require_product` | disabled | If enabled, requires an MO to reference a product. |
| `qc_retest_enabled` | enabled | Applies configured QC/test-station retest rules. |
| `qc_retest_max` | 2 | Additional attempts allowed after the initial fail. |
| `qc_retest_window_min` | 0 | No retest time limit when zero. |
| `dashboard_refresh` | 10 seconds | Dashboard refresh interval. |

Configure these through the administration UI rather than editing the SQLite table
directly.

### Outbound MES push

Outbound event delivery is configured from **Configuration**. When enabled, material
scans and QC decisions are queued in `mes_outbox` and delivered asynchronously with
retries. Configure endpoint, authentication header/token, timeout, retries, poll
interval, and optional station-specific endpoints. The built-in `/api/demo-mes` endpoint
is available for local integration testing. Offline mode is enabled by default; review
the network policy before enabling outbound delivery.

### Inbound machine APIs

The Flask server exposes several API contracts:

1. **Key-protected machine API:** `/api/v1/events/*` and query routes. Use a unique
   per-station key managed from the admin API/configuration page. Send it as
   `X-API-Key`. Docs are at `/api/docs`; the OpenAPI document is at
   `/api/v1/openapi.json`.
2. **Keyless integration API:** `/api/integration/*`, intended for a trusted LAN. It
   includes health, MO/serial queries, and process-status updates.
3. **Keyless machine contract:** selected `/api/v1` routes such as
   `/api/v1/heartbeat`, `/api/v1/orders/*`, and `/api/v1/units/*`. These are distinct
   from the API-key-protected `/api/v1/events/*` routes.

Because the latter two contracts do not require an API key, restrict them to trusted
networks and do not publish them to the internet. Validate machine payloads and route
behavior in a non-production environment first. The full route list, request examples,
and response details are in [README.md](./README.md) and [API_TESTING.md](./API_TESTING.md).

### Station agent

`station_agent.py` is a standard-library client for machines or edge PCs. Download the
script and per-station configuration from the admin API page. The config contains the
server URL, station code, and station key; keep it private. The agent can send scans,
QC decisions, downtime, NCRs, alerts, and heartbeats. Its local SQLite outbox retries
events after connectivity interruptions; permanent server-side validation errors are
reported instead of being silently retried.

### Controller output

Optional station controller output can use a log driver (for testing), TCP, UDP,
Modbus/TCP, or Modbus RTU over serial/RS485. Configure output per station or globally.
Use the UI's test action and verify controller behavior with the equipment owner before
connecting it to production. Controller communication failures are designed not to block
production recording.

## 7. Technical overview

### Architecture

```text
Browser / barcode scanner / machine client
                  |
       Flask GUI and REST APIs
       or Streamlit GUI
                  |
     Shared services and validation
                  |
         SQLite database
       data/mts.db by default
          /           \
 async MES outbox   station output
```

Both UI entry points initialize the same database and call the shared services. Flask
registers the machine and integration API blueprints and is the interface used by the
Waitress LAN launcher. A keyboard-emulating USB scanner works as an input device; no
scanner-specific client software is required.

### Repository map

| Path | Responsibility |
|---|---|
| `flask_app.py` | Flask application, web routes, authentication/session checks, and blueprint registration. |
| `app.py` | Streamlit application entry point and navigation. |
| `machine_api.py` | Keyed machine API and related documentation routes. |
| `integration_api.py` | LAN integration endpoints. |
| `openapi_spec.py` | OpenAPI definition for the keyed machine API. |
| `station_agent.py` | Standalone edge client and its local retry queue. |
| `auth/` | Password hashing, login, and authorization helpers. |
| `database/db.py` | SQLite connection and query helpers. |
| `database/schema.py` | Database schema creation, migrations, and seed data. |
| `services/` | Domain logic for validation, transactions, products, materials, reports, genealogy, QC, downtime, NCRs, notifications, backups, and integrations. |
| `ui/` | Streamlit screens and theme. |
| `templates/`, `static/` | Flask HTML templates and static assets. |
| `reports/` | PDF and Excel report generation. |
| `data/` | Active database and runtime station-agent/output data. |
| `backups/` | Database snapshots created by the application. |
| `logs/` | Application log output. |

### Database concepts

The database is SQLite. Schema creation and compatibility changes are handled by
`database/schema.py`; the main domain entities are:

- **Users, stations, station types:** accounts and shop-floor station catalogue.
- **Products, product routes, MOs, MO routes:** production orders and route inheritance.
- **Materials, assignments, serials:** material requirements and units tied to an MO.
- **Material transactions, exceptions, QC checks:** traceability and quality audit.
- **Process parameters and unit bindings:** machine measurements and parent/child part
  genealogy.
- **System config, station communication:** global and station-specific integration
  configuration.
- **MES outbox and received messages:** asynchronous outbound delivery and the demo
  receiver.
- **Downtime events/reasons, notifications, NCR records:** operational and quality
  workflows.

Connections enable SQLite foreign keys and a five-second busy timeout. The application
uses separate short-lived database connections for operations.

### Validation and lifecycle rules

- Every serial is associated with one MO.
- Material requirements are resolved for the current station and MO; already-recorded
  materials are not offered again for the same station/serial.
- Route resolution order is explicit MO route, linked product route, then default
  station ordering (assigned stations plus QC stations).
- With enforcement enabled, out-of-sequence, out-of-route, or halted-station work is
  rejected. A violation may also alert and halt the station according to configuration.
- Station type catalogue flags determine whether a station is a QC gate or requires
  material.
- Serial lifecycle values are `AVAILABLE`, `IN_PROCESS`, `COMPLETED`, `HOLD`, and
  `REJECTED`.
- Transactions, quality decisions, and exceptions are retained for traceability.

### Security and deployment notes

- Passwords are stored as PBKDF2-HMAC-SHA256 hashes with per-password random salts
  (100,000 iterations in the current implementation).
- Machine API keys are station-specific. Rotate or disable a key if it is exposed.
- The keyed machine API is separate from keyless LAN integration routes. Use a firewall
  and trusted network boundary; the application is not a substitute for a deployment
  security review.
- The development Flask server is not the recommended shared-LAN server. Use
  `start_server.bat`/Waitress for LAN access.
- Protect database files, agent configuration files, logs, and backups with appropriate
  operating-system permissions. They may contain operational or credential data.
- Review `services/netguard.py` and the configuration UI before permitting outbound
  MES traffic. Keep offline mode enabled if no outbound connection is needed.

### Maintenance and troubleshooting

| Symptom | Checks |
|---|---|
| App will not start | Confirm Python is on PATH, install `requirements.txt`, and review console output. |
| Login fails | Confirm the account is active, username/password are correct, and demo credentials have not been changed. |
| Serial or material is not offered | Verify the MO is released, serial belongs to it, station is active, and material assignment matches the MO/station. |
| Route scan is rejected | Review MO/product route order, current serial progress, station halt state, and route enforcement settings. |
| Machine API returns auth errors | Confirm server-wide API is enabled, the station is active/API-enabled, and the correct current station key is sent. |
| Machine events do not arrive | Check network/firewall, API response, `api_last_seen`, agent config, and the agent's local outbox. |
| MES pushes remain pending/failed | Check offline mode, MES enable switch, target URL/network policy, endpoint credentials, and outbox error details. |
| Controller output is missing | Check station/global output settings, transport target, serial/Modbus parameters, and station output log. |
| Reports show no data | Review selected date filters and ensure events have been recorded for the selected MO/station. |

Application logs are written to `logs/app.log`; the log directory is created at startup.
The local station-agent queue is separate from the central database. Back up both when
agent events are important and have not yet synchronized.

## 8. Developer workflow

1. Install dependencies with `python -m pip install -r requirements.txt`.
2. Run Flask locally with `python flask_app.py`, or Streamlit with
   `python -m streamlit run app.py`.
3. Use a disposable database or a backup when testing schema or reset behavior.
4. Exercise relevant user flows and machine endpoints; API examples are in
   [API_TESTING.md](./API_TESTING.md).
5. Keep domain rules in `services/` so Flask and Streamlit use the same behavior.
6. Update this guide and the API reference when user workflows, schema, or contracts
   change.

The Windows `build.bat` script builds the Streamlit app as a PyInstaller onedir bundle.
The standard deployment path for the Flask web application is the Python/Waitress
launcher described above.
