# StormPilot — Drone delivery operations and reliability lab

StormPilot now includes a live four-drone fleet monitor, GPS telemetry ingestion, delivery confirmation, Gmail notification hooks, and the original reliability-lab dashboard for Redmine and service health.

## What is included

- Four demo drones: SP-DRONE-01, SP-DRONE-02, SP-DRONE-03, SP-DRONE-04
- Optional Google Maps JavaScript integration with a clear offline fallback
- Browser geolocation registration and telemetry submission endpoint
- Delivery confirmation flow with idempotent completion handling
- Gmail SMTP test and notification configuration with graceful warning mode
- Existing Redmine project workspace, Prometheus metrics, and Docker/Kubernetes tooling remain intact

## Quick start

```bash
python -m unittest discover -s tests -v
python app/server.py
```

Open `http://127.0.0.1:8080/ui` for the operations dashboard and `http://127.0.0.1:8080/drone/connect` for the phone registration demo.

## Milestone 1: durable telemetry and delivery state

This repository includes a SQLite-backed persistence layer for the demonstration app. It persists:

- Drone registration metadata and current telemetry state.
- Accepted and rejected GPS samples with a safe diagnostic reason.
- Delivery lifecycle records and delivery state history.
- Event log entries for telemetry, delivery transitions, and confirmations.
- A durable notification outbox with idempotency, SMTP delivery, and bounded retries.

The persistence layer is configured through `APP_DATA_DIR` and retains telemetry according to `DATA_RETENTION_DAYS` with a default of 30 days. Data survives a process restart when the same working directory or configured data directory is reused.

## Milestone 2: device identity and delivery evidence

Device enrollment issues a random bearer token and stores only its salted PBKDF2 hash. Telemetry requires that token, checks device-to-drone binding, rejects stale or out-of-order samples, and applies a per-device rate limit. Tokens expire after 30 days and can be revoked. The browser tracking page enrolls the device and keeps its token in session storage.

Accepted telemetry records segment distance. Cumulative observed distance updates only for in-transit deliveries; flagged GPS jumps are retained as samples but excluded from the cumulative value. Arrival is detected using `ARRIVAL_GEOFENCE_RADIUS_M` (default 50 m) and records the reported GPS accuracy and measured distance as evidence. This is an estimate from device-provided GPS, not proof of physical location or guaranteed GPS accuracy.

Delivery completion writes an idempotent outbox item in the same database transaction. Run `python -m app.notification_worker` to poll and send queued email. The worker uses SMTP STARTTLS, retries failures up to three times with backoff, and can recover jobs left processing for over five minutes. Docker Compose starts the API and worker against a shared persistent volume.

This is demo-grade security, not a production identity system. Device enrollment and revocation endpoints do not yet have a separate administrator authorization mechanism; restrict them at a trusted network or authenticated proxy boundary. Use HTTPS for all bearer-token traffic, and do not expose the demo enrollment flow directly to untrusted users.

## Environment configuration

Copy `.env.example` to `.env` and update the values before launching the full demo. The Google Maps key is optional for backend service operation; if it is missing, the dashboard still works in offline demo mode.

```bash
copy .env.example .env
```

## Google Maps setup

1. Create a browser-restricted Google Maps JavaScript API key.
2. Restrict it to your app origins and the Maps JavaScript API.
3. Set `GOOGLE_MAPS_API_KEY` in `.env` or the runtime environment.
4. If no key is set, the dashboard remains useful and shows the demo view without a map.

## Gmail notification setup

1. Enable Gmail App Passwords for the account.
2. Set `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_APP_PASSWORD`, and `DELIVERY_NOTIFICATION_TO`.
3. The app continues to operate even if credentials are missing and shows a clear warning instead of failing the delivery flow.

## Demo workflow

1. Open the tracking page at `/drone/connect`.
2. Choose a drone identity and enter a device ID. The page enrolls the device and stores its token for the browser session.
3. Grant browser geolocation permission.
4. Watch accepted telemetry appear on the fleet dashboard.
5. Confirm a handover through `/api/delivery/confirm`; configured notifications are delivered by the worker.

## Architecture notes

The repository still includes the original Redmine proxy and reliability-lab structure, with the additional drone domain layered on top. The new fleet logic is intentionally small, testable, and safe for local demonstration. See the docs folder for the full implementation roadmap and demo guide.