# StormPilot — Drone delivery operations and reliability lab

StormPilot is a local reliability-engineering lab with a four-identity fleet monitor, authenticated phone GPS, delivery evidence, optional Gmail notifications, an in-site Agile planning board, and the original Redmine integration.

## Included

- Four stable drone identities; missing accepted phone GPS produces `OFFLINE` with no fabricated position.
- Optional Google Maps JavaScript integration, in-place marker updates, observed paths, planned delivery legs and a no-key fallback.
- Bounded fleet and traffic polling; stale-position handling; persisted observed distance and configurable geographic aggregation.
- Password-backed individual administrator accounts and `ADMIN`, `OPERATOR`, and `VIEWER` roles.
- Deterministic mission-assignment comparisons, approval-gated application, isolated what-if scenarios and read-only bounded route replay.
- Simulated corridors, zones and route scenarios; these are not official aviation restrictions or flight-safety guidance.
- Existing delivery handover enforcement, idempotent email outbox, Gmail SMTP worker, Redmine proxy, health endpoints and Prometheus metrics.
- Persistent Planbox-style Agile backlog and sprint planning with a four-column Kanban board, sprint start/complete lifecycle, assignees, priorities, story points, and role-protected work-item updates.
- Optional Bengaluru map demo with five simulated drone deliveries, pickup/drop-off pins and a sample-density heat layer; the samples are visibly separated from real GPS and are never inserted into live fleet records.

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
- Any signed-in role can read the Agile board. `ADMIN` and `OPERATOR` can create sprints and work items and update assignments or status; only `ADMIN` can delete work items.
- Bootstrap the first administrator using `python -m app.admin bootstrap --username <name>`. There is no public administrator registration endpoint.
- Migrations 004 and 005 are additive and preserve existing data. Version 005 adds Agile sprints and work items. Existing shared-token configuration no longer authorizes mutations; there is no legacy-token bypass.

## Database backup and migration recovery

- Back up before every upgrade: `sqlite3 .stormpilot_data/stormpilot.db ".backup '.stormpilot_data/stormpilot.backup.db'"` while the app is running, or copy `stormpilot.db` while the app is stopped.
- Migrations run automatically at import and are tracked in `schema_migrations` (versions 001-005). `/readyz` reports `unready` if the schema is behind or a required table is missing.
- To roll back: stop the application, restore the pre-migration backup, restart. Changes made since the backup are lost. Never delete or recreate a populated database to "fix" an upgrade, and never reset migrations on an existing deployment.

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
6. Verify the dashboard only after loading it in a browser with a valid key. The expected fallback messages are "Google Maps is not configured", "Google Maps rejected the key", or "Google Maps network/API error"; API smoke tests do not prove live map rendering.

## Phone GPS integration

- Browser geolocation requires a secure context. `localhost` and `127.0.0.1` are acceptable for local testing; a phone on another device normally needs HTTPS on the origin that serves `/drone/connect`.
- New phone enrollment requires an `ADMIN` dashboard session and CSRF cookie. The phone then stores a device-specific bearer token in browser session storage; revocation invalidates that token and active devices cannot be rebound to a different drone without revocation.
- The phone page displays browser permission, device connection, delivery status, latitude/longitude, reported accuracy, last fix time and backend acceptance/rejection. Stale browser fixes older than 30 seconds are not sent; stale backend positions are shown as `STALE`, not current.
- Real phone GPS is separate from `SIMULATED` digital-twin data. The dashboard labels `PHONE_GPS`, `STALE`, `OFFLINE` and `SIMULATED` states, and planned route overlays are distinct from observed GPS paths.
- Practical verification: sign in as an administrator on the dashboard, open `/drone/connect` on one phone for one demo drone, optionally use a second phone for another drone, grant location permission, watch accepted fixes and accuracy on the phone page, then verify the dashboard shows `PHONE_GPS`, observed path/distance, and eventual `STALE` behavior when updates stop.

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
& .\.venv\Scripts\python.exe scripts/check_inline_js.py
node --check app/static/app.js
git diff --check
docker compose -f compose.yaml config --quiet
docker compose -f compose.yaml -f compose.dev.yaml config --quiet
docker compose -f compose.yaml -f compose.production.yaml config --quiet
```

The smoke script targets an already running local server; use a free port and isolated `APP_DATA_DIR` for test deployments. Do not stop or restart existing Docker services for ordinary validation. Compose files are validated with `config` only; `compose.dev.yaml` and `compose.production.yaml` are overrides used together with `compose.yaml` (as in the Makefile `up` target). [VALIDATION.md](VALIDATION.md) holds the verification matrix that separates automated tests, local smoke checks and unverified live integrations (live Maps render, real-phone GPS, live SMTP, browser automation).

## Gmail notifications

Configure `SMTP_HOST` (`smtp.gmail.com` for Gmail), `SMTP_PORT` (587), `SMTP_USERNAME` (the Gmail address), `SMTP_APP_PASSWORD` (a Google App Password, not the account password) and `DELIVERY_NOTIFICATION_TO` to use Gmail SMTP. The application always uses STARTTLS before login; there is no `SMTP_USE_TLS` setting. The message `From` value is `SMTP_USERNAME`; there is no separate `SMTP_FROM_EMAIL` setting. `SMTP_PASSWORD` is not read by the application.

The application continues operating if email is not configured; sending remains asynchronous in the notification worker, retries transient failures with bounded backoff, stops after the retry budget, and keeps exhausted messages visible in the outbox, `/readyz` and `/metrics`.

Gmail App Password setup:

1. Enable 2-Step Verification on the Google account (required for App Passwords).
2. Open Google Account → Security → App passwords, create one named `stormpilot`, and copy the 16-character value.
3. Put it only in `SMTP_APP_PASSWORD` (or a Docker/Compose secret). Never commit it, never paste it into the UI, and never log it — the server never returns it through any API.

Safe local test mode: set `SMTP_LOCAL_TEST_MODE=true` to exercise the entire outbox pipeline without opening any network connection (notifications are marked `SENT` locally, and `POST /api/email/test` as an `ADMIN` reports `local_test`). Automated tests use this mode or mocked SMTP, so no real email is ever sent during validation.

Safe live receipt verification:

1. Leave `SMTP_LOCAL_TEST_MODE=true` for normal development and automated tests.
2. For an intentional live Gmail test only, set `SMTP_LOCAL_TEST_MODE=false`, `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=587`, `SMTP_USERNAME`, `SMTP_APP_PASSWORD` and `DELIVERY_NOTIFICATION_TO` in `.env` or equivalent secret storage.
3. Start only the intended isolated service instance, sign in as `ADMIN`, and call `POST /api/email/test` or complete one delivery with arrival evidence.
4. If delivery fails, check Gmail App Password validity, 2-Step Verification, recipient address, network egress to port 587, and `/readyz`/`/metrics` notification counts. Do not print or commit the App Password. If SMTP credentials are unavailable, `/api/email/test` returns a warning, notifications stay `PENDING`/`FAILED` in the outbox, and live delivery is recorded as unverified in VALIDATION.md.

## Further documentation

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md), [docs/DEMO_GUIDE.md](docs/DEMO_GUIDE.md), and [docs/RUNBOOK.md](docs/RUNBOOK.md).
