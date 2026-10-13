# StormPilot — Drone delivery operations and reliability lab

StormPilot includes a four-identity fleet monitor, authenticated phone GPS telemetry, delivery evidence, Gmail notification hooks, and the original Redmine/reliability dashboard. The live map, traffic intelligence and virtual digital-twin foundation are built on Google Maps JavaScript API and the existing SQLite backend.

## What is included

- Four stable drone identities: SP-DRONE-01 through SP-DRONE-04; identities without accepted phone GPS have no fabricated location
- Optional Google Maps JavaScript integration, in-place marker updates, observed GPS paths and an offline fallback
- Bounded fleet/traffic polling, stale-position handling, persisted observed distance and configurable traffic grids
- A separate, deterministic virtual corridor/zone scenario with two intersecting routes; this is not flight control or aviation guidance
- Administrator-authorized device registration, revocation, digital-twin configuration and delivery handover
- Delivery handover with server-side arrival enforcement and idempotent completion handling
- Gmail SMTP test and notification configuration with graceful warning mode
- Existing Redmine project workspace, Prometheus metrics, and Docker/Kubernetes tooling remain intact

## Quick start

In PowerShell, set the runtime values (use a generated random administrator secret and your own browser-restricted Maps key):

```powershell
$env:HOST = "127.0.0.1"
$env:PORT = "8081"
$env:APP_DATA_DIR = ".\.stormpilot_data"
$env:STORMPILOT_ADMIN_TOKEN = "<random-secret-of-at-least-32-bytes>"
$env:GOOGLE_MAPS_API_KEY = "<browser-restricted-key-or-empty>"
python app/server.py
```

Open `http://127.0.0.1:8081/ui` for the operations dashboard and `http://127.0.0.1:8081/drone/connect` for phone tracking and administrator-authorized enrollment. Port 8080 is the Compose default and may already be in use; select an unused port rather than stopping another service.

Run tests and the bounded smoke check from another PowerShell window:

```powershell
python -m unittest discover -s tests -v
python scripts/smoke.py --port 8081
```

The `python app/server.py` development server does not load `.env` automatically. Set variables in the shell as above. Compose reads `.env` automatically; copy `.env.example` and set a strong `STORMPILOT_ADMIN_TOKEN`, `GRAFANA_ADMIN_PASSWORD`, and any optional integrations first.

## Milestone 1: durable telemetry and delivery state

This repository includes a SQLite-backed persistence layer for the demonstration app. It persists:

- Drone registration metadata and current telemetry state.
- Accepted and rejected GPS samples with a safe diagnostic reason.
- Delivery lifecycle records and delivery state history.
- Event log entries for telemetry, delivery transitions, and confirmations.
- A durable notification outbox with idempotency, SMTP delivery, and bounded retries.

The persistence layer is configured through `APP_DATA_DIR` and retains telemetry according to `DATA_RETENTION_DAYS` with a default of 30 days. Expired samples are pruned at startup and at most hourly during fleet, traffic or telemetry activity; values outside 1-3650 days fail validation. Data survives a process restart when the same working directory or configured data directory is reused.

## Milestone 2: device identity and delivery evidence

Device enrollment issues a random bearer token and stores only its salted PBKDF2 hash. Telemetry requires that token, checks device-to-drone binding, rejects stale or out-of-order samples, and applies a per-device rate limit. Tokens expire after 30 days and can be revoked. The browser tracking page enrolls the device and keeps its token in session storage.

Accepted telemetry records segment distance. Cumulative observed distance updates only for in-transit deliveries; flagged GPS jumps are retained as samples but excluded from the cumulative value. Arrival is detected using `ARRIVAL_GEOFENCE_RADIUS_M` (default 50 m) and records the reported GPS accuracy and measured distance as evidence. This is an estimate from device-provided GPS, not proof of physical location or guaranteed GPS accuracy.

Delivery completion writes an idempotent outbox item in the same database transaction. Run `python -m app.notification_worker` to poll and send queued email. The worker uses SMTP STARTTLS, retries failures up to three times with backoff, and can recover jobs left processing for over five minutes. Docker Compose starts the API and worker against a shared persistent volume.

Device enrollment, revocation, delivery handover and virtual-twin configuration require `Authorization: Bearer <STORMPILOT_ADMIN_TOKEN>` from the server environment. If it is unset, these operations fail closed with HTTP 503. The phone page asks for the secret only when enrolling a new device and does not persist it; the individual device token continues to authorize GPS telemetry and is stored only for that browser session. Registration and revocation write audit events. Do not distribute the shared administrator secret to ordinary phone users; use HTTPS and an authenticated network boundary for non-local deployments.

This remains a demonstration identity model, not production administrator IAM: the shared secret does not distinguish named administrators, provide per-user roles or manage rotation. Production deployments should use a server-verified administrator identity/role behind the existing authenticated proxy and keep any shared token in a secret manager.

## Environment configuration

Copy `.env.example` to `.env` and update the values before launching the full demo. The Google Maps key is optional for backend service operation; if it is missing, the dashboard still works in offline demo mode.

```bash
copy .env.example .env
```

## Google Maps setup

1. Enable billing and the **Maps JavaScript API** in the Google Cloud project.
2. Create a browser key with **HTTP referrer** application restrictions and an API restriction to **Maps JavaScript API**. This browser key is intentionally public and returned by `/api/config`; never put server-side credentials there.
3. For local development, allow only the origins you use, for example `http://localhost:8081/*` and `http://127.0.0.1:8081/*`. Add port-8080 origins only if using that port.
4. For production, allow only the deployed HTTPS origin, for example `https://fleet.example.com/*`; replace the example hostname. Do not use broad wildcard referrers.
5. Set `GOOGLE_MAPS_API_KEY` in Compose's `.env` or the application environment. Missing config, referrer/API restrictions, or network errors leave backend fleet APIs available and show a map-specific error.
6. Set suitable quotas and billing budgets/alerts in Google Cloud and monitor usage. Maps usage may incur charges beyond the current free allowances; prices and allowances change, so check current Google Maps Platform pricing and quota documentation for the project and region. StormPilot does not call Google Routes or another billable routing API for each GPS update; planned legs are simple overlays between persisted pickup and destination points. Traffic density is rendered as cached Maps JavaScript rectangle overlays rather than Google's retired Heatmap Layer ([deprecation notice](https://developers.google.com/maps/deprecations)).

## Gmail notification setup

1. Enable Gmail App Passwords for the account.
2. Set `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_APP_PASSWORD`, and `DELIVERY_NOTIFICATION_TO`.
3. The app continues to operate even if credentials are missing and shows a clear warning instead of failing the delivery flow.

## Demo workflow

1. Have an administrator securely provide the server-configured enrollment secret.
2. Open `/drone/connect`, choose an identity and device ID, enter the administrator token once to enroll, and grant browser geolocation permission. The separate device token is held in session storage.
3. Accepted GPS appears on `/ui`. A position is `STALE` when either its server receipt or device-reported fix time is more than two minutes old; stale positions are not treated as current.
4. Delivery handover requires an existing `ARRIVED` delivery and administrator authorization at `/api/delivery/confirm`; browser code cannot directly set delivery state.
5. Configure virtual corridors/zones with authorized `PUT /api/twin/config`, inspect `/api/twin`, and enable the dashboard's virtual-twin layer.

## Fleet API, traffic and digital twin

- `GET /api/fleet` returns the four drone identities with latest accepted persisted GPS, actual observed distance, current delivery details and a bounded observed path. A drone without GPS is `OFFLINE` with a null position; a fix is `STALE` when either its server-received time or device-reported time is more than 120 seconds old.
- `GET /api/traffic?window_hours=24&cell_size_deg=0.01&source=PHONE_GPS` aggregates up to 5,000 accepted samples into configurable 0.005°, 0.01° or 0.02° cells. Supported time windows are 1, 6, 24 and 168 hours; optional `drone_id` and `mission` filters are supported. Results identify `live` versus `historical` data and separate exact observed-path crossings from shared grid cells. The heatmap rectangles are updated in place.
- The map's dashed planned leg joins stored pickup and destination coordinates directly. It is not a routed course. Observed paths and heatmap values come only from persisted, accepted phone GPS.
- `GET /api/twin` returns a deterministic simulated two-route corridor scenario and configured virtual zones. Authorized `PUT /api/twin/config` accepts bounded `corridors`, `zones`, `restrictions` and `scenarios` collections and stores validated config in SQLite. Twin routes/zones are simulated demonstrations, not official aviation restrictions or a claim that a route is safe for physical aircraft.

## Architecture notes

The repository still includes the original Redmine proxy and reliability-lab structure, with the additional drone domain layered on top. The new fleet logic is intentionally small, testable, and safe for local demonstration. See the docs folder for the full implementation roadmap and demo guide.