# StormPilot operational demo

## Start locally

Use PowerShell and a data directory separate from any existing tracked/local database. Choose a free port; do not stop an existing service to claim 8080.

```powershell
$env:HOST = "127.0.0.1"
$env:PORT = "8081"
$env:APP_DATA_DIR = ".\.stormpilot_m4_data"
$env:STORMPILOT_COOKIE_SECURE = "false"
$env:GOOGLE_MAPS_API_KEY = "<optional browser-restricted key>"
& .\.venv\Scripts\python.exe -m app.admin bootstrap --username opsadmin
& .\.venv\Scripts\python.exe app/server.py
```

Open `http://127.0.0.1:8081/ui`. Bootstrap prompts for the first password twice and refuses to run when an administrator account already exists.

## Operator walkthrough

1. Sign in on the dashboard with an individual account. The session cookie is HTTP-only; mutations mirror the CSRF cookie through `X-CSRF-Token`. Use ADMIN/OPERATOR accounts only for the operations they are permitted to perform.
2. Review four stable drones. New/no-phone identities remain OFFLINE; accepted phone GPS is shown as `PHONE_GPS`; stale positions are explicitly stale, not current.
3. Open `/drone/connect` in the phone browser. An ADMIN dashboard session is required for new enrollment. Existing device bearer tokens continue to authorize only their bound phone telemetry. Grant geolocation permission to send actual phone GPS, then watch the page's permission, connection, last-fix, accuracy and backend status fields. If permission is denied, unavailable, timed out, stale or inaccurate, treat the phone as unverified until a fresh accepted fix is recorded.
4. Compare current, nearest-eligible and optimized assignment strategies. Review deadline, nominal-range, zone/corridor constraints and estimates. Only select the explicit approval action when prepared to apply a proposal to eligible persisted `CREATED` deliveries.
5. Run a what-if case, inspect its baseline and proposed plan, affected IDs, warnings and rationale. Repeat from scenario history to replay the persisted deterministic result. Scenario changes remain isolated until an eligible proposal is explicitly approved.
6. Use route replay to select a drone or delivery and a bounded time window. Step, play, pause and adjust speed; export the JSON report. Gaps represent absent GPS samples, not interpolated points.
7. Inspect traffic origin and filters, and optionally enable virtual twin overlays. Live/historical observations and `SIMULATED` virtual objects are distinct datasets.
8. Delivery handover remains server-authoritative and requires recorded arrival evidence. Replay and hypothetical execution cannot complete deliveries or send notifications.
9. Check `/readyz` and `/metrics` before the demo. Readiness should be `ok`; metrics should show bounded fleet, telemetry, notification, optimizer and auth signals without device IDs, delivery IDs or secrets.

## Honest constraints

- If Maps is not configured, login, fleet data, optimizer, scenario and replay APIs remain usable with an explicit map fallback.
- Google Maps key must be HTTP-referrer restricted and API restricted to Maps JavaScript API. Development origins should be limited to the actual local ports; production must allow only the deployed HTTPS origin.
- Google Maps billing may apply beyond applicable free allowances. Configure project quotas and budgets. StormPilot does not request Google Routes for each GPS update.
- Optimizer estimates use nominal operator-configured range/speed and straight-line repositioning, not battery/flight telemetry or a safe physical route. The deterministic heuristic is not AI.
- Virtual routes/zones are not official aviation restrictions. No physical flight-control command is implemented.
- Run tests and smoke checks against isolated data. Do not restart Docker Desktop or existing containers for ordinary verification.
- Use `SMTP_LOCAL_TEST_MODE=true` for a safe local email pipeline check. A live Gmail receipt is pending unless a Gmail App Password and recipient are configured and intentionally tested.
- Back up `.stormpilot_data/stormpilot.db` before migrating or before a high-value demo. Restore by stopping the app, replacing the database from the backup and restarting; do not reset migrations on a populated database.
