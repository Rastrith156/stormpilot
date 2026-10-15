# Architecture

StormPilot layers an operations dashboard, persistent Agile board and deterministic planning tools over its existing fleet, reliability lab and Redmine integration.

## Presentation

- `/ui` serves the responsive operations dashboard, Google Maps layer, role-aware administrator screens, assignment strategy comparison, what-if history and route replay.
- The map includes five Bengaluru sample routes from `/api/demo/bengaluru`. These are labelled `SIMULATED`, never enter the live fleet database, and render as separate drone/pickup/drop-off pins and a sample-density layer. When Google Maps is not configured, an attributed OpenStreetMap embed displays the same sample overlay; accessing that fallback requires a network connection.
- The Agile board on `/ui` persists sprint and backlog work items in SQLite. It supports a single-active-sprint lifecycle, status, assignee, priority and story-point updates; `VIEWER` accounts are read-only, while task deletion is restricted to `ADMIN`.
- `/drone/connect` uses an administrator session for enrollment and a distinct opaque device bearer token for GPS upload. It exposes browser permission, device connection, last-fix, accuracy and delivery-state signals, and it does not prompt for a shared admin credential.
- The fleet view polls at one bounded five-second interval, refuses overlapping requests, updates persistent map overlays in place and retains last-good data during outages.
- Replay limits requests to a bounded time interval/sample count and is read-only. Playback changes only a local cursor and map marker.

## Identity and authorization

- `app/security.py` handles PBKDF2-HMAC-SHA256 password hashes, opaque server-side sessions, account/rate-limit state, CSRF validation and audit events.
- Session IDs are random and stored only as SHA-256 hashes. Session cookies are `HttpOnly`, `SameSite=Strict`, path `/api`, and expire after eight hours. The CSRF token has a separate non-HttpOnly cookie and must be duplicated into `X-CSRF-Token` on cookie-authenticated mutations.
- `STORMPILOT_COOKIE_SECURE` defaults to `true`; local HTTP demos must explicitly set it to `false`. HTTPS deployments must retain `true`.
- `ADMIN` manages accounts, devices and virtual-twin configuration; `OPERATOR` can propose/apply assignments, run/apply scenarios and oversee deliveries; `VIEWER` can read protected scenario and replay data. Routes resolve identity and role from the database session, not client-supplied claims.
- The first admin is provisioned only through `python -m app.admin bootstrap --username <name>`, which prompts for a password and refuses when any account already exists. Public registration is not provided.
- Login throttles account and network keys. Disabling a user or rotating their password revokes their sessions. The final enabled ADMIN cannot be disabled or demoted.
- Device credentials remain separately hashed and bearer-authenticated for telemetry. Enrollment and revocation require an ADMIN session and CSRF token. Active devices cannot be rebound to a different drone without revocation; revoked devices fail authentication and may be recovered by administrator re-enrollment. Privileged actions and failures are audited without recording passwords, tokens or session secrets.
- `STORMPILOT_ADMIN_TOKEN` does not bypass this authorization system. Existing deployments should back up data and bootstrap the first individual administrator after migration 004 before exposing mutation workflows.

## API and persistence

- SQLite migrations 001-005 persist stable drone identities, hashed phone credentials, accepted/rejected telemetry, deliveries and event history, notification outbox, twin config, administrator users/sessions/login attempts, simulated scenario/proposal snapshots, and Agile sprints/work items.
- `/api/fleet` and `/api/traffic` provide bounded actual telemetry and historical aggregates. `/api/config` exposes only the public browser Maps key/readiness.
- `/api/device/telemetry` validates the separate phone bearer token, device/drone binding, coordinates, timestamps, accuracy, ordering and rate. Accepted GPS remains the sole source for current positions and observed distance.
- `/api/delivery/confirm` preserves server-authoritative arrival and handover checks. Delivery completion and its idempotent email outbox stay in the same existing lifecycle.
- `/healthz` is process liveness. `/readyz` checks required SQLite schema/table availability and reports notification backlog counts without exposing secrets. `/metrics` keeps existing request counters and adds bounded operational families for telemetry rejection reasons, stale devices, delivery states, notification states/exhaustion, optimizer rejections, simulation runs and login throttling. It avoids per-device, per-delivery, credential, session or token labels.
- `/api/redmine` and original static/service routes remain compatible.

## Optimization and scenarios

- `app/optimizer.py` captures a bounded persisted baseline and returns current-assignment, nearest-eligible and deterministic greedy optimized plans.
- Eligibility requires recent accepted `PHONE_GPS`; offline/stale drones are unavailable. Constraints include deadline, operator-configured nominal maximum/remaining range, persisted virtual restricted zones and virtual corridor constraints. No battery state exists.
- Stored planned distance is preferred; otherwise the engine labels a 1.3x straight-line estimate. Drone repositioning is straight-line. It is a planning heuristic, not AI, road routing, a flight path or an assertion of aviation safety.
- Proposals preserve a persisted baseline and are rejected if live delivery state has changed before application. Only eligible `CREATED` assignments can change after explicit ADMIN/OPERATOR approval.
- Scenario input, baseline and result are persisted in `simulation_runs` with `SIMULATED` origin. Hypothetical changes run against copied snapshot values and cannot mutate GPS, delivery transitions or outbox state. Replaying stored scenarios returns the original deterministic result.
- A hypothetical high-priority delivery is not inserted into live delivery records. Scenario application can only affect eligible persisted deliveries.

## Route replay and forensics

- `/api/replay` accepts exactly one drone or delivery, an ISO 8601 time range of at most 30 days, and a capped request of at most 2,000 points; relevant event history is capped at 500 rows.
- Only accepted persisted GPS is returned. Samples are sorted by timestamp; gaps over 120 seconds are identified and never interpolated.
- `/api/replay/report` returns concise JSON metadata and historical event context. Neither endpoint has a delivery mutation or notification side effect.

## Notifications

- Delivery completion enqueues an idempotent receipt in `notification_outbox`. Duplicate completion requests return the existing confirmed state and do not create duplicate receipts.
- The worker retries retryable SMTP failures with bounded exponential backoff and stops after the configured retry budget, leaving exhausted `FAILED` rows visible for operators and metrics.
- Email content is built from persisted delivery data: delivery ID, drone/device identifier when known, observed distance, completion time and recorded handover evidence. SMTP credentials remain server-side and are never rendered in API responses, metrics, logs or frontend bundles.
- `SMTP_LOCAL_TEST_MODE=true` exercises the outbox without opening SMTP, for local demos and automated validation. Live Gmail delivery requires a Gmail App Password and remains unverified unless a real account is configured and tested intentionally.

## Maps, traffic and virtual twin

- Google Maps JavaScript API is optional. `/api/config` only returns `GOOGLE_MAPS_API_KEY`, intended as a browser-public, HTTP-referrer- and API-restricted key. Missing keys/errors do not block backend service.
- Maps JavaScript marker, line, rectangle and circle objects are reused. No billable routing API is called during GPS updates. Google Maps usage may incur cost; quotas and billing alerts are operator responsibilities.
- Traffic grids and path intersections are based on accepted live/historical backend observations and labelled as such.
- Twin corridors, zones, restrictions and route intersections are simulated demonstration objects. They are neither official restrictions nor flight-safety guidance. No real flight-control commands exist.

## Deployment and limitations

- Docker Compose keeps the API, notification worker and monitoring configuration; do not restart existing services for routine validation.
- Before non-local deployment, review HTTPS termination, proxy header trust, cookie security, network exposure, backups, secret handling, account recovery and operational monitoring.
- Range/speed values are nominal assumptions without battery telemetry; route distances/repositioning are estimates; fresh GPS is not proof of a physical aircraft's true location.
- Restore of a pre-migration backup is the rollback method and discards any writes after that backup. Do not reset an existing database during upgrade.
