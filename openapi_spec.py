"""
OpenAPI 3.0 specification for the machine REST API (served at
/api/v1/openapi.json, rendered by /api/docs — both offline, no CDN).
"""


def _ref(name):
    return {"$ref": f"#/components/schemas/{name}"}


def _key_param():
    return {
        "name": "X-API-Key",
        "in": "header",
        "required": True,
        "schema": {"type": "string"},
        "description": "Per-station API key (Admin → API page). "
                       "Authorization: Bearer <key> also accepted; "
                       "GET endpoints accept ?api_key= for browser testing.",
    }


def _json_body(properties, required=None):
    return {
        "required": True,
        "content": {"application/json": {
            "schema": {"type": "object", "properties": properties,
                       "required": required or []}}},
    }


def _resp(description, example=None, status="200"):
    content = {"application/json": {"schema": {"type": "object"}, }}
    if example is not None:
        content["application/json"]["example"] = example
    return {status: {"description": description, "content": content}}


def build_spec(base_url=""):
    ok_props = {"ok": {"type": "boolean", "example": True}}
    return {
        "openapi": "3.0.3",
        "info": {
            "title": "MTS Machine API",
            "description": "Material Tracking System acting as the MES server. "
                           "Machines authenticate with a per-station API key and "
                           "push production events (scans, QC, downtime, NCR, alerts) "
                           "and query work data (serials, materials, MOs).",
            "version": "1.0.0",
        },
        "servers": [{"url": base_url or "/", "description": "This MES server"}],
        "tags": [
            {"name": "system", "description": "Health and spec"},
            {"name": "events", "description": "Machine → MES production events"},
            {"name": "queries", "description": "MES → machine work data"},
        ],
        "paths": {
            "/api/v1/health": {
                "get": {"tags": ["system"], "summary": "API health (no auth)",
                        "security": [],
                        "responses": _resp("Alive", {"ok": True, "version": "1.0.0"})},
            },
            "/api/v1/events/heartbeat": {
                "post": {"tags": ["events"], "summary": "Heartbeat — keeps the link alive "
                                                        "and returns server state",
                         "parameters": [_key_param()],
                         "responses": _resp("State snapshot", {
                             "ok": True, "server_time": "2026-10-19 12:00:00",
                             "station": {"station_code": "ST10", "station_type": "WORK",
                                         "stage_code": "OP10", "halted": False},
                             "state": "RUNNING", "open_stoppage": None,
                             "unread_alerts": 0})},
            },
            "/api/v1/events/trace": {"post": {"tags": ["events"], "summary": "Trace event (serial/line/station/stage/status/operator/fixture/sw/hw/cycle_time/parameters)"}},
        "/api/v1/events/material_scan": {
                "post": {"tags": ["events"],
                         "summary": "Record a material barcode scan at the calling station "
                                    "(same validation + route enforcement as the operator UI; "
                                    "scans are rejected when the station is halted or the "
                                    "serial has not reached this routing stage)",
                         "parameters": [_key_param()],
                         "requestBody": _json_body({
                             "serial_number": {"type": "string", "example": "SN-001"},
                             "material_barcode": {"type": "string",
                                                  "example": "MAT-1001|VEND1|B24"}},
                             ["serial_number", "material_barcode"]),
                         "responses": {
                             "200": {"description": "Scan recorded", "content":
                                     {"application/json": {"example": {
                                         "ok": True, "transaction_id": "TXN-…",
                                         "serial_number": "SN-001",
                                         "material_code": "MAT-1001",
                                         "remaining": [], "station_complete": True,
                                         "route_complete": False,
                                         "serial_status": "IN_PROCESS"}}}},
                             "400": _resp("Validation / route-enforcement rejection",
                                         {"ok": False, "error": "Previous stage ST10 is not complete…"}),
                             "401": _resp("Missing key"), "403": _resp("Invalid key"),
                             "503": _resp("API disabled")}},
            },
            "/api/v1/events/qc": {
                "post": {"tags": ["events"], "summary": "Record a QC inspection "
                                                        "(QC stations only)",
                         "parameters": [_key_param()],
                         "requestBody": _json_body({
                             "serial_number": {"type": "string"},
                             "decision": {"type": "string", "enum": ["PASS", "FAIL"]},
                             "fail_status": {"type": "string",
                                             "enum": ["HOLD", "REJECTED"]},
                             "reason": {"type": "string"}},
                             ["serial_number", "decision"]),
                         "responses": {
                             "200": _resp("Decision recorded", {
                                 "ok": True, "decision": "FAIL", "result": "HOLD",
                                 "serial_status": "HOLD"}),
                             "400": _resp("Rejection", {"ok": False, "error": "…"})}},
            },
            "/api/v1/events/downtime": {
                "post": {"tags": ["events"], "summary": "Machine-driven stoppage "
                                                        "(start / end)",
                         "parameters": [_key_param()],
                         "requestBody": _json_body({
                             "action": {"type": "string", "enum": ["start", "end"]},
                             "reason_code": {"type": "string", "example": "MACHINE_FAULT"},
                             "notes": {"type": "string"}}, ["action"]),
                         "responses": {
                             "200": _resp("State changed", {"ok": True, "state": "DOWN"}),
                             "409": _resp("Stoppage already open",
                                         {"ok": False, "error": "Station already has an open stoppage."})}},
            },
            "/api/v1/events/ncr": {
                "post": {"tags": ["events"], "summary": "Raise a non-conformance",
                         "parameters": [_key_param()],
                         "requestBody": _json_body({
                             "serial_number": {"type": "string"},
                             "defect_reason": {"type": "string", "example": "dimensional out of spec"},
                             "description": {"type": "string"}}, ["defect_reason"]),
                         "responses": _resp("NCR opened", {
                             "ok": True, "ncr_no": "NCR-20261019-0001",
                             "disposition": "OPEN"})},
            },
            "/api/v1/events/alert": {
                "post": {"tags": ["events"], "summary": "Push an alarm into the MES feed "
                                                        "(bell + Andon ticker)",
                         "parameters": [_key_param()],
                         "requestBody": _json_body({
                             "level": {"type": "string", "enum": ["INFO", "WARN", "CRIT"]},
                             "message": {"type": "string"}}, ["message"]),
                         "responses": _resp("Alert stored", {"ok": True, "alert_id": 42})},
            },
            "/api/v1/stations/me": {
                "get": {"tags": ["queries"], "summary": "Calling station profile, work "
                                                        "assignments and state",
                        "parameters": [_key_param()],
                        "responses": _resp("Station data", {
                            "ok": True,
                            "station": {"station_code": "ST10", "station_type": "WORK",
                                        "stage_code": "OP10", "halted": False},
                            "state": "RUNNING",
                            "materials_by_mo": {"MO-001": [
                                {"material_code": "MAT-1001",
                                 "material_description": "Housing",
                                 "barcode_rule": "MAT-1001|VENDOR|BATCH"}]},
                            "work_queue": [{"serial_number": "SN-001",
                                            "status": "IN_PROCESS"}],
                            "last_scans": []})},
            },
            "/api/v1/serials/{serial_number}": {
                "get": {"tags": ["queries"], "summary": "Serial detail: status, route "
                                                        "progress, pending work at the "
                                                        "calling station, QC / NCR history",
                        "parameters": [_key_param(),
                                       {"name": "serial_number", "in": "path",
                                        "required": True, "schema": {"type": "string"}}],
                        "responses": {
                            "200": _resp("Serial detail", {"ok": True, "serial": {
                                "serial_number": "SN-001", "status": "IN_PROCESS",
                                "mo_number": "MO-001"}}),
                            "404": _resp("Unknown serial", {"ok": False, "error": "Serial not found."})}},
            },
            "/api/v1/mos": {
                "get": {"tags": ["queries"], "summary": "Manufacturing orders with progress",
                        "parameters": [_key_param()],
                        "responses": _resp("MO list", {"ok": True, "mos": [{
                            "mo_number": "MO-001", "status": "RELEASED",
                            "planned_qty": 100, "serials": 42, "completed": 12}]})},
            },
            "/api/v1/materials": {
                "get": {"tags": ["queries"], "summary": "Material master with barcode rules",
                        "parameters": [_key_param()],
                        "responses": _resp("Materials", {"ok": True, "materials": [{
                            "material_code": "MAT-1001", "material_description": "Housing",
                            "barcode_rule": "MAT-1001|VENDOR|BATCH", "active": 1}]})},
            },
            "/api/v1/downtime/reasons": {
                "get": {"tags": ["queries"], "summary": "Active stoppage reason codes",
                        "parameters": [_key_param()],
                        "responses": _resp("Reasons", {"ok": True, "reasons": [
                            {"code": "MACHINE_FAULT", "label": "Machine fault"}]})},
            },
            "/api/v1/notifications": {
                "get": {"tags": ["queries"], "summary": "Recent MES alerts (for machine "
                                                        "displays)",
                        "parameters": [_key_param()],
                        "responses": _resp("Alerts", {"ok": True, "unread": 2, "items": []})},
            },
            "/api/v1/heartbeat": {
                "post": {"tags": ["system"], "summary": "Machine health check — MES "
                         "acknowledges with server time (no auth; GET also accepted)",
                         "security": [],
                         "requestBody": _json_body({
                             "timestamp": {"type": "string", "example": "2026-10-20T21:02:00Z"},
                             "status": {"type": "string", "example": "UP"},
                             "currentWorkOrder": {"type": "string", "example": "MO-2026-0045"},
                             "station_code": {"type": "string", "example": "ST10"}}),
                         "responses": _resp("Acknowledged", {
                             "status": "ACK", "serverTime": "2026-10-20T21:02:01Z",
                             "service": "mts-machine-api", "database": "UP",
                             "stationCode": "ST10"})},
            },
            "/api/v1/orders/{moNumber}/serials": {
                "get": {"tags": ["queries"], "summary": "Serials generated for a MO "
                         "(no auth; LAN use)", "security": [],
                        "parameters": [{"name": "moNumber", "in": "path", "required": True,
                                        "schema": {"type": "string"}}],
                        "responses": {
                            "200": _resp("Serials", {
                                "moNumber": "MO-12", "totalSerials": 2,
                                "serials": [{"serialNumber": "SN-1", "status": "IN_PROCESS"},
                                            {"serialNumber": "SN-3", "status": "COMPLETED"}]}),
                            "404": _resp("Unknown MO", {"error": "MO not found."})}},
            },
            "/api/v1/orders/{moNumber}/product": {
                "get": {"tags": ["queries"], "summary": "Product master (code/name/revision) "
                         "linked to a MO (no auth)", "security": [],
                        "parameters": [{"name": "moNumber", "in": "path", "required": True,
                                        "schema": {"type": "string"}}],
                        "responses": {
                            "200": _resp("Product", {"moNumber": "MO-12",
                                                     "productCode": "PROD-WIDGET-X",
                                                     "productName": "Premium Widget X",
                                                     "revision": "B"}),
                            "404": _resp("Unknown MO", {"error": "MO not found."})}},
            },
            "/api/v1/units/{serialNumber}/status": {
                "get": {"tags": ["queries"], "summary": "Lifecycle state of a unit before "
                         "the machine touches it (no auth)", "security": [],
                        "parameters": [{"name": "serialNumber", "in": "path", "required": True,
                                        "schema": {"type": "string"}}],
                        "responses": {
                            "200": _resp("Unit status", {
                                "serialNumber": "SN-1", "moNumber": "MO-12",
                                "productCode": "PROD-WIDGET-X", "currentStatus": "IN_PROCESS",
                                "isScrapped": False, "isRework": False}),
                            "404": _resp("Unknown serial", {"error": "Serial not found."})}},
            },
            "/api/v1/units/{serialNumber}/routing-validation": {
                "get": {"tags": ["queries"],
                        "summary": "Interlocking check — has the unit passed the previous "
                                   "routing step? Pass ?station_code= to validate the "
                                   "caller's position (read-only, never halts; no auth)",
                        "security": [],
                        "parameters": [
                            {"name": "serialNumber", "in": "path", "required": True,
                             "schema": {"type": "string"}},
                            {"name": "station_code", "in": "query", "required": False,
                             "schema": {"type": "string"}}],
                        "responses": {
                            "200": _resp("Routing validation", {
                                "serialNumber": "SN-1", "allowedToProcess": False,
                                "previousStation": {"stationId": "ST10", "status": "PASS",
                                                    "timestamp": "2026-10-20 20:45:00"},
                                "nextExpectedStationId": "ST20"}),
                            "404": _resp("Unknown serial", {"error": "Serial not found."})}},
            },
            "/api/v1/orders/active": {
                "get": {"tags": ["queries"], "summary": "Find active/released MOs by master "
                         "criteria (?lineId= / ?productCode=) (no auth)", "security": [],
                        "parameters": [
                            {"name": "lineId", "in": "query", "required": False,
                             "schema": {"type": "string"}},
                            {"name": "productCode", "in": "query", "required": False,
                             "schema": {"type": "string"}}],
                        "responses": _resp("Active orders", {
                            "lineId": "LINE-01",
                            "activeOrders": [{"moNumber": "MO-12",
                                              "scheduleDate": "2026-08-31",
                                              "priority": 1, "status": "RELEASED"}]})},
            },
        },
        "components": {
            "securitySchemes": {
                "apiKey": {"type": "apiKey", "in": "header", "name": "X-API-Key",
                           "description": "Per-station key from Configuration → "
                                          "Machine REST API."},
            },
            "schemas": {"Error": {"type": "object", "properties": {
                "ok": {"type": "boolean", "example": False},
                "error": {"type": "string"}}}},
        },
        "security": [{"apiKey": []}],
    }
