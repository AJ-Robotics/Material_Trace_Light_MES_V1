# MES API — Complete Testing Guide

Two API groups are available:

| Group | Prefix | Auth | Intended for |
|-------|--------|------|--------------|
| **Integration (keyless)** | `/api/integration` | none | plant LAN machines / lines / test tools |
| **Machine (keyed)** | `/api/v1` | `X-API-Key` | authenticated station agents |

Server: `http://<host>:5000` — example host `192.168.0.105`.
Health first, always:

```bash
curl http://192.168.0.105:5000/api/integration/health
```

---

## A. Keyless Integration API — `/api/integration`

### 1. Health check — `GET /api/integration/health`
No params. Confirms the MES process is alive and SQLite is reachable.

```bash
curl http://192.168.0.105:5000/api/integration/health
```
```json
{"ok":true,"service":"mts-integration-api","version":"1.0.0",
 "server_time":"2026-10-20 17:46:03","database":"UP"}
```

---

### 2. Get MO — `GET /api/integration/mos/<mo_number>`
Returns the manufacturing order header + serial counts.

```bash
curl http://192.168.0.105:5000/api/integration/mos/MO-12
```
```json
{"ok":true,"mo":{"id":72,"mo_number":"MO-12","description":null,
 "product_code":null,"product_name":null,"planned_qty":5,"status":"RELEASED",
 "active":true,"serials":4,"completed":2,"created_at":"..."}}
```

---

### 3. Serial batch vs MO — `GET /api/integration/mos/<mo_number>/serials`
All serials against an MO, with status + paging.

| Query | Default | Notes |
|-------|---------|-------|
| `status` | (all) | filter e.g. `IN_PROCESS`, `COMPLETED` |
| `limit` | 200 | 1..1000 |
| `offset` | 0 | paging |

```bash
curl "http://192.168.0.105:5000/api/integration/mos/MO-12/serials?status=COMPLETED&limit=50"
```
```json
{"ok":true,"mo":{"mo_number":"MO-12","status":"RELEASED"},
 "total":2,"limit":50,"offset":0,
 "serials":[{"id":239,"serial_number":"SN-3","status":"COMPLETED",
   "created_at":"...","last_station":"ST40","last_activity":"..."}]}
```

---

### 4. Unit status — `GET /api/integration/serials/<serial_number>`
Full unit snapshot: route progress, QC, NCRs, and **child parts**.

Optional `?station_code=` adds `pending_at_station` (work) or `qc_at_station` (QC).

```bash
curl "http://192.168.0.105:5000/api/integration/serials/SN-1?station_code=ST20"
```
```json
{"ok":true,
 "unit":{"serial_number":"SN-1","status":"IN_PROCESS","mo_number":"MO-12","created_at":"..."},
 "route":[{"station_code":"ST10","station_type":"WORK","done":true},
          {"station_code":"ST20","station_type":"WORK","done":false},
          {"station_code":"ST30","station_type":"WORK","done":false},
          {"station_code":"ST40","station_type":"QC","done":false}],
 "route_complete":false,
 "child_parts":[{"serial_number":"SN-2","material_code":"MAT-05","status":"IN_PROCESS",
   "qty":1.0,"station_code":"ST10","line_code":"LINE-01","stage_code":"SMT","bound_at":"..."}],
 "station":"ST20",
 "pending_at_station":[{"material_code":"MAT-06","material_description":"Material B"}],
 "qc_at_station":null,
 "last_scan":{...},"qc_fails":[],"open_ncrs":[]}
```

---

### 5. Update process status — `POST /api/integration/process-status`
The main machine upload. Runs through the **same validation + route enforcement**
as the operator screen. Attributed to the seeded `machine` service user.

**Accepted key aliases (spaced or snake):** `Serial Number`/`serial_number`,
`Station ID`/`station_code`, `Line ID`/`line_code`, `stage code`/`stage_code`,
`Status`/`status`, `operator id`/`operator_code`, `fixture id`, `SW version`, `HW version`,
`cycle time`, `material_barcode`.

**Rules**
- `status = IN_PROCESS` → records a production trace at a **WORK** station (params optional).
- `status = PASS|FAIL` → applies at a **QC** station (`PASS` → unit `COMPLETED`;
  `FAIL` → `HOLD`/`REJECTED`, optional `fail_status`, `reason`). `FAIL` at a WORK station is rejected.
- `parameters[]` → `Key`/`value`/`unit`/`LSL`/`USL` (auto out-of-spec detection).
- `child_parts[]` → components installed into the parent unit at this station.

**Child parts format** — each entry is either a child serial string, or an object:

```json
"child_parts": [
  { "serial_number": "SN-2", "material_code": "MAT-05", "qty": 1 },
  "RAW-COMP-9"
]
```
If `serial_number` matches an existing serial it is **linked** (`child_serial_id` set);
otherwise the number is **stored as text** (raw component). Bound children are returned by
endpoint **4** under `child_parts`.

#### 5a. Work-station trace with params + children
```bash
curl -X POST http://192.168.0.105:5000/api/integration/process-status \
  -H "Content-Type: application/json" \
  -d '{
    "Serial Number":"SN-1","Line ID":"LINE-01","Station ID":"ST10",
    "stage code":"SMT","Status":"IN_PROCESS","operator id":"OP-5",
    "fixture id":"FX-12","SW version":"1.0.2","HW version":"B","cycle time":12.4,
    "parameters":[
      {"Key":"Temp","value":21.0,"unit of measurement":"C","LSL":18,"USL":25},
      {"Key":"Torque","value":4.85,"unit of measurement":"Nm","LSL":4.5,"USL":5.2}
    ],
    "child_parts":[
      {"serial_number":"SN-2","material_code":"MAT-05"},
      {"serial_number":"RAW-LIVE-1","material_code":"MAT-06"},
      "RAW-LIVE-2"
    ]
  }'
```
```json
{"ok":true,"event":"trace","result":"IN_PROCESS","transaction_id":321,
 "serial_number":"SN-1","station_code":"ST10","stage_code":"SMT","line_code":"LINE-01",
 "serial_status":"IN_PROCESS","parameters_count":2,"bindings_count":3}
```

#### 5b. QC pass (completes the unit)
```bash
curl -X POST http://192.168.0.105:5000/api/integration/process-status \
  -H "Content-Type: application/json" \
  -d '{"serial_number":"SN-1","station_code":"ST40","status":"PASS",
       "parameters":[{"Key":"Leak","value":0.02,"unit":"cc/min","USL":0.05}]}'
```
```json
{"ok":true,"event":"qc","decision":"PASS","serial_status":"COMPLETED","qc_result_id":...}
```

#### 5c. QC fail (holds the unit)
```bash
curl -X POST http://192.168.0.105:5000/api/integration/process-status \
  -H "Content-Type: application/json" \
  -d '{"serial_number":"SN-2","station_code":"ST40","status":"FAIL",
       "fail_status":"REJECTED","reason":"Leak above limit"}'
```

#### 5d. Material consumption (work station)
Provide the assigned material barcode; it must match the MO material assignment.
```bash
curl -X POST http://192.168.0.105:5000/api/integration/process-status \
  -H "Content-Type: application/json" \
  -d '{"serial_number":"SN-1","station_code":"ST10","status":"PASS",
       "material_barcode":"MAT-05"}'
```

Error shape is always `{"ok":false,"error":"...","__status":NNN}` with HTTP 400 (validation),
404 (unknown serial/MO), or 409 (conflict).

---

### OpenAPI
`GET /api/integration/openapi.json`

---

## B. Machine API (key-protected) — `/api/v1`

Auth: `X-API-Key: <station key>` (or `Authorization: Bearer <key>`;
GET also accepts `?api_key=`). Key belongs to a station, so the station is implied.

Current keys (LAN test):
```
ST10 : mts_d224bae278bf6e0602c008e5aa398ee7
```

| Method | Path | Purpose |
|--------|------|---------|
| GET  | `/api/v1/health` | server + DB health |
| GET  | `/api/v1/mo/<mo_number>` | MO details |
| GET  | `/api/v1/serials/<serial_number>` | unit status |
| POST | `/api/v1/events/trace` | trace / QC event (**same body as 5a**, incl. `child_parts`) |
| POST | `/api/v1/events/downtime` | stoppage start/end |
| GET  | `/api/v1/notifications` | station notifications |
| GET  | `/api/v1/openapi.json` | OpenAPI 3.0.3 spec |

### Trace event with children (keyed)
```bash
curl -X POST http://192.168.0.105:5000/api/v1/events/trace \
  -H "Content-Type: application/json" \
  -H "X-API-Key: mts_d224bae278bf6e0602c008e5aa398ee7" \
  -d '{"serial_number":"SN-1","status":"IN_PROCESS","line_code":"L1","stage_code":"SMT",
       "parameters":[{"Key":"Temp","value":21.0,"unit":"C"}],
       "child_parts":[{"serial_number":"SN-4"},"RAW-XYZ"]}'
```
```json
{"ok":true,"event":"trace","transaction_id":314,"serial_number":"SN-1",
 "station_code":"ST10","serial_status":"IN_PROCESS",
 "parameters_count":1,"bindings_count":2}
```

---

## C. End-to-end test sequence (copy/paste)

Route for MO-12: **ST10 → ST20 → ST30 (WORK) → ST40 (QC)**.
Test serial `SN-1` starts `IN_PROCESS`.

```text
1) GET  /api/integration/health
2) GET  /api/integration/mos/MO-12
3) GET  /api/integration/mos/MO-12/serials
4) GET  /api/integration/serials/SN-1
5) POST /api/integration/process-status   { ST10, IN_PROCESS, params + child_parts }
6) GET  /api/integration/serials/SN-1     -> route ST10 done, child_parts present
7) POST /api/integration/process-status   { ST20, IN_PROCESS }
8) POST /api/integration/process-status   { ST30, IN_PROCESS }
9) POST /api/integration/process-status   { ST40, PASS }   -> serial_status COMPLETED
10) GET /api/integration/serials/SN-1     -> route_complete true
```

To re-run from scratch, reset a serial back to the start:
```
DELETE material_transactions / quality_checks / process_parameters WHERE serial_id = SN-1
DELETE unit_bindings WHERE parent_serial_number = 'SN-1'
UPDATE serial_numbers SET status='IN_PROCESS' WHERE serial_number='SN-1'
UPDATE stations SET halted=0, halt_reason=NULL, halted_at=NULL
```
(any route violation halts the station automatically — always clear halts before retrying)

---

## D. Route / validation reference (MO-12)

| Order | Station | Type | Assigned material |
|-------|---------|------|-------------------|
| 1 | ST10 | WORK | MAT-05 (Material A) |
| 2 | ST20 | WORK | MAT-06 (Material B) |
| 3 | ST30 | WORK | MAT-07 (Material C) |
| 4 | ST40 | QC | — |

Serials: `SN-1` (IN_PROCESS), `SN-2` (IN_PROCESS), `SN-3` (COMPLETED), `SN-4` (COMPLETED).
Stations `ST10/20/30/40` active; `Test1` deactivated.

---

# E. Complete JSON formats — request + response for every case

All responses include `"ok": true|false` and HTTP status. Captured live from a
fresh seed (route ST10 → ST20 → ST30 → ST40).

## E1. `GET /api/integration/health`

**Request**
```http
GET /api/integration/health
```
**Response 200**
```json
{
  "ok": true,
  "service": "mts-integration-api",
  "version": "1.0.0",
  "server_time": "2026-10-20 19:05:00",
  "database": "UP"
}
```

## E2. `GET /api/integration/mos/<mo_number>`

**Request**
```http
GET /api/integration/mos/MO-12
```
**Response 200**
```json
{
  "ok": true,
  "mo": {
    "id": 72,
    "mo_number": "MO-12",
    "description": "",
    "product_code": null,
    "product_name": null,
    "planned_qty": 5,
    "status": "RELEASED",
    "active": true,
    "serials": 4,
    "completed": 2,
    "created_at": "2026-08-31 03:58:54"
  }
}
```
**Response 404**
```json
{ "ok": false, "error": "MO 'NOPE' not found." }
```

## E3. `GET /api/integration/mos/<mo_number>/serials`

**Request** (paging + status filter)
```http
GET /api/integration/mos/MO-12/serials?status=IN_PROCESS&limit=2&offset=0
```
**Response 200**
```json
{
  "ok": true,
  "mo": { "mo_number": "MO-12", "status": "RELEASED" },
  "total": 4,
  "limit": 2,
  "offset": 0,
  "serials": [
    { "id": 237, "serial_number": "SN-1", "status": "IN_PROCESS",
      "created_at": "2026-08-31 03:59:39", "last_station": null, "last_activity": null },
    { "id": 238, "serial_number": "SN-2", "status": "IN_PROCESS",
      "created_at": "2026-08-31 03:59:39", "last_station": null, "last_activity": null }
  ]
}
```
**Response 404**
```json
{ "ok": false, "error": "MO 'NOPE' not found." }
```

## E4. `GET /api/integration/serials/<serial_number>`

**Request (no station)**
```http
GET /api/integration/serials/SN-1
```
**Response 200**
```json
{
  "ok": true,
  "unit": {
    "serial_number": "SN-1",
    "status": "IN_PROCESS",
    "mo_number": "MO-12",
    "created_at": "2026-08-31 03:59:39"
  },
  "route": [
    { "station_code": "ST10", "station_type": "WORK", "done": false },
    { "station_code": "ST20", "station_type": "WORK", "done": false },
    { "station_code": "ST30", "station_type": "WORK", "done": false },
    { "station_code": "ST40", "station_type": "QC",   "done": false }
  ],
  "route_complete": false,
  "child_parts": [],
  "station": null,
  "pending_at_station": [],
  "qc_at_station": null,
  "last_scan": null,
  "qc_fails": [],
  "open_ncrs": []
}
```

**Request (with station → pending / QC view)**
```http
GET /api/integration/serials/SN-1?station_code=ST10
```
**Response 200 (excerpt)**
```json
{
  "ok": true,
  "station": "ST10",
  "pending_at_station": [
    { "material_code": "MAT-05", "material_description": "Material A" }
  ],
  "qc_at_station": null
}
```

**Response 200 — completed unit with child parts (after full route)**
```json
{
  "ok": true,
  "unit": { "serial_number": "SN-1", "status": "COMPLETED", "mo_number": "MO-12",
            "created_at": "2026-08-31 03:59:39" },
  "route": [
    { "station_code": "ST10", "station_type": "WORK", "done": true },
    { "station_code": "ST20", "station_type": "WORK", "done": true },
    { "station_code": "ST30", "station_type": "WORK", "done": true },
    { "station_code": "ST40", "station_type": "QC",   "done": true }
  ],
  "route_complete": true,
  "child_parts": [
    { "serial_number": "SN-2", "material_code": "MAT-05", "status": "IN_PROCESS",
      "qty": 1.0, "station_code": "ST10", "line_code": "LINE-01",
      "stage_code": "SMT", "bound_at": "2026-10-20 13:33:09" },
    { "serial_number": "RAW-LIVE-1", "material_code": "MAT-06", "status": null,
      "qty": 1.0, "station_code": "ST10", "line_code": "LINE-01",
      "stage_code": "SMT", "bound_at": "2026-10-20 13:33:09" },
    { "serial_number": "RAW-LIVE-2", "material_code": null, "status": null,
      "qty": 1.0, "station_code": "ST10", "line_code": "LINE-01",
      "stage_code": "SMT", "bound_at": "2026-10-20 13:33:09" }
  ],
  "last_scan": { "material_code": "MAT-07", "station_code": "ST30",
                 "timestamp": "2026-10-20 13:35:17" },
  "qc_fails": [],
  "open_ncrs": []
}
```

**Response 404**
```json
{ "ok": false, "error": "Serial 'NOPE' not found." }
```

## E5. `POST /api/integration/process-status`

### E5.1 Work trace with parameters + child parts — 200
**Request**
```json
{
  "Serial Number": "SN-1",
  "Line ID": "LINE-01",
  "Station ID": "ST10",
  "stage code": "SMT",
  "Status": "IN_PROCESS",
  "operator id": "OP-5",
  "fixture id": "FX-12",
  "SW version": "1.0.2",
  "HW version": "B",
  "cycle time": 12.4,
  "material_barcode": "MAT-05",
  "parameters": [
    { "Key": "Temp",   "value": 21.0, "unit of measurement": "C",  "LSL": 18,  "USL": 25 },
    { "Key": "Torque", "value": 4.85, "unit of measurement": "Nm", "LSL": 4.5, "USL": 5.2 }
  ],
  "child_parts": [
    { "serial_number": "SN-2", "material_code": "MAT-05" },
    { "serial_number": "RAW-LIVE-1", "material_code": "MAT-06" },
    "RAW-LIVE-2"
  ]
}
```
**Response 200**
```json
{
  "ok": true,
  "event": "trace",
  "result": "IN_PROCESS",
  "transaction_id": 345,
  "serial_number": "SN-1",
  "station_code": "ST10",
  "stage_code": "SMT",
  "line_code": "LINE-01",
  "serial_status": "IN_PROCESS",
  "parameters_count": 2,
  "bindings_count": 3
}
```

### E5.2 Minimal work trace (snake_case aliases) — 200
**Request**
```json
{ "serial_number": "SN-1", "station_code": "ST20", "status": "IN_PROCESS",
  "material_barcode": "MAT-06" }
```
**Response 200**
```json
{ "ok": true, "event": "trace", "result": "IN_PROCESS", "transaction_id": 346,
  "serial_number": "SN-1", "station_code": "ST20", "stage_code": null,
  "line_code": null, "serial_status": "IN_PROCESS",
  "parameters_count": 0, "bindings_count": 0 }
```

### E5.3 QC PASS (completes the unit) — 200
**Request**
```json
{ "serial_number": "SN-1", "station_code": "ST40", "status": "PASS",
  "parameters": [ { "Key": "Leak", "value": 0.02, "unit": "cc/min", "USL": 0.05 } ] }
```
**Response 200**
```json
{ "ok": true, "event": "qc", "decision": "PASS", "qc_result_id": 152,
  "serial_number": "SN-1", "station_code": "ST40", "stage_code": null,
  "line_code": null, "serial_status": "COMPLETED",
  "parameters_count": 1, "bindings_count": 0 }
```

### E5.4 QC FAIL (holds/rejects the unit) — 200
**Request**
```json
{ "serial_number": "SN-2", "station_code": "ST40", "status": "FAIL",
  "fail_status": "REJECTED", "reason": "Leak above limit" }
```
**Response 200**
```json
{ "ok": true, "event": "qc", "decision": "REJECTED", "qc_result_id": 153,
  "serial_number": "SN-2", "station_code": "ST40", "stage_code": null,
  "line_code": null, "serial_status": "REJECTED",
  "parameters_count": 0, "bindings_count": 0 }
```
The held unit then shows in `GET /serials/SN-2`:
```json
{ "ok": true,
  "unit": { "serial_number": "SN-2", "status": "REJECTED", "mo_number": "MO-12" },
  "route_complete": true,
  "qc_fails": [ { "timestamp": "2026-10-20 13:35:17", "station_code": "ST40",
                  "fail_status": "REJECTED", "reason": "Leak above limit" } ] }
```

### E5.5 Wrong barcode at a work station (`process-status`)
`process-status` treats `status` = `PASS`/`IN_PROCESS` at a **WORK** station as a production
trace; the material is matched only when `material_barcode` is supplied. A non-matching
barcode is **not** rejected here — it records the trace without material consumption (use the
keyed `material_scan` endpoint, E6.4, for strict material validation). Real response:
```json
{ "ok": true, "event": "trace", "result": "PASS", "transaction_id": 352,
  "serial_number": "SN-1", "station_code": "ST10", "serial_status": "IN_PROCESS",
  "parameters_count": 0, "bindings_count": 0 }
```

### E5.6 FAIL reported at a WORK station — 400
**Request**
```json
{ "serial_number": "SN-2", "station_code": "ST10", "status": "FAIL" }
```
**Response 400**
```json
{ "ok": false, "error": "ST10 is a production station — report FAIL at a QC station (status FAIL) instead." }
```

### E5.7 Route violation (skipping a stage) — 400 + station halted
**Request**
```json
{ "serial_number": "SN-1", "station_code": "ST40", "status": "PASS" }
```
**Response 400**
```json
{ "ok": false,
  "error": "Previous stage ST10 (ST10) is not complete for 'SN-1'. Finish it before scanning at ST40. STATION ST40 IS NOW HALTED — an administrator must clear it." }
```

### E5.8 Unknown serial — 400
```json
{ "ok": false, "error": "Serial 'NOPE' is not registered." }
```

### E5.9 Missing required field — 400
```json
{ "ok": false, "error": "serial_number is required." }
```

### E5.10 Unknown station — 404
```json
{ "ok": false, "error": "Unknown station_code 'NOPE'." }
```

### E5.11 Serial already rejected — 400
```json
{ "ok": false, "error": "Serial 'SN-2' was REJECTED by QC and cannot be processed further." }
```

## E6. Machine API (keyed) — `/api/v1`

### E6.1 `POST /api/v1/events/trace` — 200
**Request**
```http
POST /api/v1/events/trace
X-API-Key: mts_<station key>
Content-Type: application/json
```
```json
{ "serial_number": "SN-1", "status": "IN_PROCESS", "line_code": "L1",
  "stage_code": "SMT",
  "parameters": [ { "Key": "Temp", "value": 21.0, "unit": "C" } ],
  "child_parts": [ { "serial_number": "SN-4" }, "RAW-XYZ" ] }
```
**Response 200**
```json
{ "ok": true, "event": "trace", "transaction_id": 314, "serial_number": "SN-1",
  "station_code": "ST10", "stage_code": "SMT", "line_code": "L1",
  "serial_status": "IN_PROCESS", "parameters_count": 1, "bindings_count": 2 }
```

### E6.2 Missing key — 401
```json
{ "ok": false, "error": "Missing API key. Send X-API-Key: <station key> (or Authorization: Bearer <key>)." }
```

### E6.3 Invalid key — 403
```json
{ "ok": false, "error": "Invalid API key." }
```

### E6.4 `POST /api/v1/events/material_scan` — strict material consumption
**Request (valid)**
```http
POST /api/v1/events/material_scan
X-API-Key: mts_<station key>
Content-Type: application/json
```
```json
{ "serial_number": "SN-1", "material_barcode": "MAT-05" }
```
**Response 200**
```json
{ "ok": true, "serial_number": "SN-1", "material_code": "MAT-05",
  "serial_status": "IN_PROCESS", "station_complete": true, "route_complete": false,
  "remaining": [], "transaction_id": "TXN-20261020190550-F0D8A1" }
```
**Request (wrong barcode)**
```json
{ "serial_number": "SN-1", "material_barcode": "NOPE-XYZ" }
```
**Response 400**
```json
{ "ok": false, "error": "Barcode 'NOPE-XYZ' does not match any required material. Required: MAT-05 (Material A)" }
```

> The `X-API-Key` belongs to a **station**, so `station_code` is implied; it rotates on
> server restart — read the current value from the Admin → API page or the `station_comm` table.

## E7. Child-parts payload variants (all equivalent)

```json
"child_parts": [ "SN-2", "SN-4" ]
```
```json
"child_parts": [ { "serial_number": "SN-2" }, { "Serial Number": "SN-4" } ]
```
```json
"child_parts": [ { "serial_number": "SN-2", "material_code": "MAT-05", "qty": 2 } ]
```
Aliases accepted for the array: `child_parts`, `children`, `components`, `Child Parts`,
`child serial numbers`, `child_serials`.
Aliases accepted per item: `serial_number`, `Serial Number`, `child_serial`,
`child_serial_number`, `child`, `sn`, `SN`.
Matching rule: existing serial → linked (`status` populated in E4); otherwise stored as text
(`status: null`).

## E8. Error envelope

Every failure uses the same shape, with the HTTP status shown above:
```json
{ "ok": false, "error": "<human readable message>" }
```

---

# F. Duplicate-scan protection (same serial, same station)

A serial can be **processed only once at a given station**. This applies to the
**web/operator screen and both APIs** (they all run through `ValidationService`).

Rules:
- **WORK station** — the serial is blocked from re-scanning once all required
  materials for that station are recorded. While materials are still pending, the
  serial **may** be re-scanned (to record the remaining assigned materials) — this
  is legitimate multi-material scanning, not a duplicate.
- **QC station** — blocked after a recorded **PASS**. A **FAIL → HOLD** may be
  **retested**; only a further decision after a PASS is refused.
- **API trace** (`/api/v1/events/trace`, `/api/integration/process-status` with
  `IN_PROCESS`/`PASS` at a work station) — the **second** trace for the same serial
  at the same station is rejected with **409**, regardless of pending materials.

## F1. API duplicate trace — first accepted, second rejected
**Request (sent twice)**
```json
{ "serial_number": "SN-1", "station_code": "ST10", "status": "IN_PROCESS" }
```
**First response 200**
```json
{ "ok": true, "event": "trace", "transaction_id": 377, "serial_number": "SN-1",
  "station_code": "ST10", "serial_status": "IN_PROCESS",
  "parameters_count": 0, "bindings_count": 0 }
```
**Second response 409**
```json
{ "ok": false,
  "error": "Serial 'SN-1' has already been processed at ST10. It cannot be scanned at the same station again." }
```

## F2. Web/operator re-scan after station complete
The serial-scan validation returns the same block:
```json
{ "ok": false,
  "error": "Serial 'SN-1' has already been processed at ST10. It cannot be scanned at the same station again." }
```

## F3. Multi-material WORK station (still allowed until complete)
Station ST10 with two assigned materials (MAT-05, MAT-99):

| Step | Action | Result |
|------|--------|--------|
| 1 | scan serial | allowed, `pending = [MAT-05, MAT-99]` |
| 2 | scan MAT-05 | recorded, `pending = [MAT-99]` |
| 3 | re-scan serial | **allowed** (MAT-99 still pending) |
| 4 | scan MAT-99 | recorded, `pending = []` |
| 5 | re-scan serial | **blocked** — "already been processed at ST10" |

## F4. QC retest then re-PASS
| Step | Action | Result |
|------|--------|--------|
| 1 | QC `FAIL` → `HOLD` | `serial_status = HOLD` |
| 2 | QC `PASS` (retest) | allowed → `serial_status = COMPLETED` |
| 3 | QC `PASS` again | blocked — serial is `COMPLETED` / already passed |

## F5. Machine API duplicate trace — 409
```http
POST /api/v1/events/trace
X-API-Key: mts_<station key>
```
```json
{ "serial_number": "SN-1", "status": "IN_PROCESS" }
```
Second call:
```json
{ "ok": false,
  "error": "Serial 'SN-1' has already been processed at ST10. It cannot be scanned at the same station again." }
```


---

# G. Single machine-integration POST with `childParts`

`POST /api/integration/process-status` is the one call a machine/line sends per unit
per station. `childParts` is **always included** � send `[]` when there are none.

## G1. Exact sample payload (FAIL at a WORK station)
```json
{
  "Serial Number": "SN-1",
  "Line ID": "LINE-01",
  "Station ID": "ST20",
  "stage code": "ASSY",
  "Status": "FAIL",
  "operator id": "OP-5",
  "fixture id": "FX-12",
  "SW version": "1.0.2",
  "HW version": "B",
  "cycle time": 12.4,
  "childParts": [
    { "PartBarcode1": "MAT-06" },
    { "PartBarcode2": "MAT-05" }
  ],
  "parameters": [
    { "Key": "Torque", "value": 4.85, "unit of measurement": "Nm", "LSL": 4.5, "USL": 5.2 },
    { "Key": "Angle",  "value": 90.1, "unit of measurement": "deg", "LSL": 89,  "USL": 91 }
  ]
}
```
**Response 200**
```json
{
  "ok": true,
  "event": "trace",
  "result": "FAIL",
  "fail_status": "HOLD",
  "transaction_id": 389,
  "serial_number": "SN-1",
  "station_code": "ST20",
  "stage_code": "ASSY",
  "line_code": "LINE-01",
  "serial_status": "HOLD",
  "parameters_count": 2,
  "bindings_count": 2,
  "child_parts_consumed": [ "MAT-06" ],
  "child_parts_unmatched": [ "MAT-05" ]
}
```
Meaning: `MAT-06` matched a material assigned at ST20 ? consumed as a material scan;
`MAT-05` belongs to an earlier station and is stored as an (unmatched) child binding.
Both appear under `unit_bindings` / `child_parts`.

## G2. Empty childParts (accepted)
```json
{
  "Serial Number": "SN-2", "Line ID": "LINE-01", "Station ID": "ST20",
  "stage code": "ASSY", "Status": "IN_PROCESS", "childParts": []
}
```
**Response 200**
```json
{ "ok": true, "event": "trace", "result": "IN_PROCESS", "serial_number": "SN-2",
  "station_code": "ST20", "serial_status": "IN_PROCESS",
  "parameters_count": 0, "bindings_count": 0,
  "child_parts_consumed": [], "child_parts_unmatched": [] }
```

## G3. Child-part entry formats (all accepted)
```json
"childParts": [ { "PartBarcode1": "MAT-06" }, { "PartBarcode2": "MAT-05" } ]
```
```json
"childParts": [ { "serial_number": "SN-1002", "material_code": "MAT-05", "qty": 1 } ]
```
```json
"childParts": [ "SN-1003", "SN-1004" ]
```
Accepted array keys: `childParts`, `child_parts`, `children`, `components`, `Child Parts`.
Accepted object keys: `serial_number`, `Serial Number`, `child_serial`, `material_code`,
`material`, `barcode`, `qty`, plus **any dynamic key** (e.g. `PartBarcode1`) whose value is
taken as the part barcode.

## G4. Status semantics at each station type

| Status | WORK station | QC station |
|--------|--------------|------------|
| `IN_PROCESS` | production trace recorded | trace recorded |
| `PASS` | production trace recorded | unit **COMPLETED** |
| `FAIL` | failed trace + **HOLD** (`fail_status: "REJECTED"` ? REJECTED) | QC fail ? HOLD / REJECTED |

> The unit is blocked from a second scan at the same station (see section F).

## G5. Notes
- JSON must be well-formed (each element separated by a comma � a missing comma after the
  `childParts` array makes the body invalid and yields a 400).
- A serial must be at the correct route position; scanning ahead is a route violation that
  HALTs the station until an administrator clears it.

---

# H. Machine / edge-gateway contract (keyless, /api/v1)

Six **unauthenticated** endpoints (LAN-only) that mirror the machine contract.
Responses are returned in the documented shape (no `ok` envelope).

## H1. `POST /api/v1/heartbeat`
```json
{ "timestamp": "2026-10-20T21:02:00Z", "status": "UP",
  "currentWorkOrder": "MO-12", "station_code": "ST10" }
```
**200**
```json
{ "status": "ACK", "serverTime": "2026-10-20T21:02:01Z",
  "service": "mts-machine-api", "database": "UP", "stationCode": "ST10" }
```
`GET /api/v1/heartbeat` also works. When `station_code` is a known station the
caller is stamped as alive (`station_comm.api_last_seen`).

## H2. `GET /api/v1/orders/{moNumber}/serials`
```json
{ "moNumber": "MO-12", "totalSerials": 4,
  "serials": [ { "serialNumber": "SN-1", "status": "IN_PROCESS" },
               { "serialNumber": "SN-3", "status": "COMPLETED" } ] }
```
Statuses are the raw MES values (`IN_PROCESS` / `COMPLETED` / `HOLD` / `REJECTED`).
Unknown MO ? **404** `{"error": "MO 'X' not found."}`.

## H3. `GET /api/v1/orders/{moNumber}/product`
```json
{ "moNumber": "MO-12", "productCode": "PROD-WIDGET-X",
  "productName": "Premium Electronic Widget X", "revision": "B" }
```
Fields are `null` when the MO has no linked product. Unknown MO ? **404**.

## H4. `GET /api/v1/units/{serialNumber}/status`
```json
{ "serialNumber": "SN-1", "moNumber": "MO-12", "productCode": "PROD-WIDGET-X",
  "currentStatus": "IN_PROCESS", "isScrapped": false, "isRework": false }
```
`isScrapped` = status `REJECTED`; `isRework` = status `HOLD`. Unknown serial ? **404**.

## H5. `GET /api/v1/units/{serialNumber}/routing-validation`
Optional `?station_code=` tells the MES which station is asking.
```json
{ "serialNumber": "SN-1", "allowedToProcess": false,
  "previousStation": { "stationId": "ST10", "status": "PASS",
                       "timestamp": "2026-10-20 20:45:00" },
  "nextExpectedStationId": "ST20" }
```
- **Read-only** � this endpoint never halts a station.
- With `station_code`: `allowedToProcess` is true only when every earlier route
  stage is complete **and** the caller's own stage is not yet done.
- Without `station_code`: `previousStation` = last completed stage and
  `nextExpectedStationId` = first stage not yet done.
- `previousStation` is `null` until the unit has a record at an earlier stage.

## H6. `GET /api/v1/orders/active?lineId=&productCode=`
```json
{ "lineId": "LINE-01",
  "activeOrders": [ { "moNumber": "MO-12", "scheduleDate": "2026-08-31",
                      "priority": 1, "status": "RELEASED" } ] }
```
- Filters are optional and combine (AND).
- `scheduleDate` = MO creation date (yyyy-mm-dd); `priority` is a fixed `1`
  (the schema has no priority field yet).
- Only MOs with `status` in `RELEASED` / `IN_PROGRESS` / `ACTIVE` and `active = 1`.
- `lineId` matches any station on the MO route with that `line_code`.

---

# I. QC / test-station retest rules

Configurable in `Configuration ? QC / test-station retest rules`:

| Key | Meaning | Default |
|-----|---------|---------|
| `qc_retest_enabled` | master switch | `1` |
| `qc_retest_max` | extra retests allowed after the first FAIL | `2` |
| `qc_retest_window_min` | minutes allowed between attempts (0 = no limit) | `0` |

## I1. Policy
- The **first** result at a QC/test station is the initial test.
- After a **FAIL** (HOLD) the unit may be **retested `qc_retest_max` times**.
- **Any PASS within the allowed retests becomes the final result** and the unit continues.
- When every retest is used and the unit still **FAILs**, it is forced to **REJECTED**.
- If `qc_retest_window_min` > 0, each retest must arrive within that many minutes of the
  previous attempt at the same station; otherwise the unit is **REJECTED** and the retest
  is refused with **409**.

With `qc_retest_max = 2` (3 attempts total):

| Sequence | Outcome |
|----------|---------|
| `FAIL, PASS` | **PASS** (completed) |
| `FAIL, FAIL, PASS` | **PASS** (completed) |
| `FAIL, FAIL, FAIL` | **REJECTED** (retest budget exhausted) |
| `FAIL` then a retest after the window expired | **REJECTED** (window missed) |

Identical rules apply through:
- the operator screen (`/operator` QC form),
- keyless `POST /api/integration/process-status` (PASS/FAIL at a QC station),
- keyed `POST /api/v1/events/qc` and `/api/v1/events/trace` (PASS/FAIL).

A refused retest returns **409** (API) or a "SCAN BLOCKED" banner (operator), and the unit
is set to **REJECTED**.

## I2. Reading retest state
`GET /api/integration/serials/<sn>?station_code=<qc>` (and the keyed
`GET /api/v1/serials/<sn>`) returns a `qc_retest` object:

```json
{
  "enabled": true,
  "attempts": 1,
  "retests_used": 0,
  "max_retests": 2,
  "window_min": 0,
  "last_decision": "FAIL",
  "last_fail_status": "HOLD",
  "last_at": "2026-10-20 21:00:00",
  "can_retest": true,
  "within_window": true,
  "budget_exhausted": false,
  "deadline": null,
  "block_reason": null
}
```

## I3. Example � FAIL then PASS (max=2)
```
POST /api/integration/process-status
{ "serial_number": "SN-1", "station_code": "ST40", "status": "FAIL",
  "fail_status": "HOLD", "reason": "leak" }
-> 200  { "decision": "HOLD", "serial_status": "HOLD", ... }

POST /api/integration/process-status
{ "serial_number": "SN-1", "station_code": "ST40", "status": "PASS" }
-> 200  { "decision": "PASS", "serial_status": "COMPLETED", ... }
```

## I4. Example � exhausted budget
```
FAIL -> HOLD
FAIL -> HOLD        (2nd retest taken; still within budget for max=2? no: this is retest 2)
FAIL -> REJECTED    (terminal failure)
PASS -> 409 { "ok": false, "error": "Retest limit reached (3 attempts: 1 initial + 2 retests). The unit is rejected." }
```
