# StormPilot — Drone delivery operations and reliability lab

StormPilot is a local reliability-engineering lab with a four-identity fleet monitor, authenticated phone GPS, delivery evidence, optional Gmail notifications, and the original Redmine/reliability dashboard.

## Included

- Four stable drone identities; missing accepted phone GPS produces `OFFLINE` with no fabricated position.
- Optional Google Maps JavaScript integration, in-place marker updates, observed paths, planned delivery legs and a no-key fallback.
- Bounded fleet and traffic polling; stale-position handling; persisted observed distance and configurable geographic aggregation.
- Password-backed individual administrator accounts and `ADMIN`, `OPERATOR`, and `VIEWER` roles.
- Deterministic mission-assignment comparisons, approval-gated application, isolated what-if scenarios and read-only bounded route replay.
- Simulated corridors, zones and route scenarios; these are not official aviation restrictions or flight-safety guidance.
- Existing delivery handover enforcement, idempotent email outbox, Gmail SMTP worker, Redmine proxy, health endpoints and Prometheus metrics.

## Local setup

Use a separate data directory during development so existing database files remain untouched. Start the API on an available loopback port; do not stop an existing service to free a port.

```powershell
$env:HOST = "127.0.0.1"
$env:PORT = "8081"
$env:APP_DATA_DIR = ".\.stormpilot_m4_data"
$env:STORMPILOT_COOKIE_SECURE = "false"
$env:GOOGLE_MAPS_API_KEY = "<browser-restricted-key-or-empty>"
& .\.venv\Scripts\python.exe -m app.admin bootstrap --username opsadmin
& .\.venv\Scripts\python.exe app/server.py
```

Bootstrap prompts for a password twice, creates only the first administrator in the selected data directory, and has no default credentials. Local HTTP development must set `STORMPILOT_COOKIE_SECURE=false`; use `true` behind HTTPS. Open `http://127.0.0.1:8081/ui` and sign in. Phone enrollment is at `http://127.0.0.1:8081/drone/connect`; existing phone bearer tokens are separate from administrator sessions.

The development server does not load `.env`. For Compose, copy `.env.example` to `.env`, set `GRAFANA_ADMIN_PASSWORD` and optional service integrations, then use `docker compose up --build` if no existing services need protection. Create the first administrator in a running Compose API container with:

```powershell
docker compose exec api python -m app.admin bootstrap --username opsadmin
```

Do not expose the app to a public network until HTTPS, the proxy boundary and authentication configuration have been reviewed.

## Authentication and migration

- Passwords use salted PBKDF2-HMAC-SHA256 hashes; opaque session identifiers are hashed at rest. Sessions are HTTP-only, SameSite Strict, expire after eight hours, and are revocable. Cookie-authenticated state changes require a CSRF cookie/header pair.
- Login attempts are rate-limited by account and network key. Admins can create users, change roles, disable accounts and rotate passwords. Disablement and password rotation revoke that user's sessions; the last enabled administrator cannot be demoted or disabled.
- `ADMIN` manages users, devices and virtual-twin configuration. `OPERATOR` can propose/apply assignments, run/apply scenarios and oversee deliveries. `VIEWER` is read-only for scenario and replay endpoints. API authorization derives identity and role from the server-side session.
- Bootstrap the first administrator using `python -m app.admin bootstrap --username <name>`. There is no public administrator registration endpoint.
- Migration 004 is additive and preserves fleet data. Back up SQLite before deployment. Existing shared-token configuration no longer authorizes mutations; there is no legacy-token bypass. To roll back, stop the application and restore the pre-migration backup; changes made since the backup will be lost.

## Optimizer, scenarios and replay

- Mission comparison returns current assignments, nearest eligible drone and a deterministic greedy heuristic. It uses persisted deliveries and fresh phone GPS, range budgets, deadlines and configured virtual restrictions. A proposal is rechecked against live state and requires explicit administrator/operator approval before eligible `CREATED` delivery assignments change.
- Stored planned route distance is preferred. Missing planned distance uses a labelled 1.3× straight-line estimate; drone-to-pickup repositioning is straight-line. There is no battery telemetry. This is not AI, road routing, physical flight planning or safety guidance. Configure `STORMPILOT_MAX_DRONE_RANGE_KM`, `STORMPILOT_REMAINING_RANGE_KM` and `STORMPILOT_ESTIMATED_SPEED_KMH` as demonstration assumptions.
- What-if runs persist a `SIMULATED` baseline/input/result and can be replayed deterministically. Offline drones, missed deadlines, unavailable virtual corridors, range limits, shared-corridor approaches, infeasible missions and hypothetical high-priority deliveries are supported. Simulation does not mutate GPS, delivery status or email state. Hypothetical new deliveries remain hypothetical.
- Replay queries accepted persisted GPS and relevant delivery events for one drone or delivery. Results are bounded (up to 2,000 points, 500 events, 30 days); missing samples remain gaps with no coordinate interpolation. Replay/report APIs are read-only.

## Google Maps

1. Enable billing and **Maps JavaScript API** in Google Cloud.
2. Use a browser key with HTTP-referrer application restrictions and an API restriction to **Maps JavaScript API**. This key is intentionally public in `/api/config`; never return server-side credentials.
3. For local development, restrict to the actual origins, for example `http://localhost:8081/*` and `http://127.0.0.1:8081/*`. Production should allow only the deployed HTTPS origin (for example `https://fleet.example.com/*`); avoid broad wildcards.
4. Set `GOOGLE_MAPS_API_KEY`. Missing configuration, API-key/network errors or billing/restriction problems do not stop backend operation.
5. Set suitable project quotas, budgets and billing alerts. Maps usage may incur charges beyond applicable free allowances; verify current regional pricing and allowances. StormPilot does not call Google Routes per GPS update. Planned legs are direct overlays, and traffic cells are rendered as in-place Maps rectangle overlays.

## Data and operational endpoints

- `/api/fleet` returns four identities, accepted persisted locations, actual observed distance, bounded observed paths, and delivery details; stale/offline locations are not treated as current.
- `/api/traffic` supports bounded 1/6/24/168-hour windows, grid sizing, and optional drone, mission and source filters. Each response labels live or historical origin.
- `/api/twin` and admin-protected `PUT /api/twin/config` expose virtual corridors/zones and deterministic simulated scenarios.
- `/api/device/register` and `/api/device/revoke` require an `ADMIN` session with CSRF validation. `/api/device/telemetry` remains bearer-authenticated and rejects revoked devices.
- `/api/delivery/confirm` retains server-side arrival evidence and authenticated handover enforcement. Delivery completion uses the idempotent notification outbox.
- The Redmine proxy, `/healthz`, `/readyz`, `/metrics` and original dashboard paths remain available.

## Tests and verification

```powershell
& .\.venv\Scripts\python.exe -m unittest discover -s tests -v
& .\.venv\Scripts\python.exe scripts/smoke.py --port 8081
git diff --check
docker compose config --quiet
```

The smoke script targets an already running local server; use a free port and isolated `APP_DATA_DIR` for test deployments. Do not stop or restart existing Docker services for ordinary validation.

## Gmail notifications

Configure `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_APP_PASSWORD` and `DELIVERY_NOTIFICATION_TO` to use Gmail SMTP with STARTTLS. The application continues operating if email is not configured; sending remains asynchronous in the notification worker.

## Further documentation

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md), [docs/DEMO_GUIDE.md](docs/DEMO_GUIDE.md), and [docs/RUNBOOK.md](docs/RUNBOOK.md).
