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
2. Choose a drone identity and enter a device ID.
3. Grant browser geolocation permission.
4. Watch the telemetry appear on the fleet dashboard.
5. Use `/api/delivery/confirm` to confirm a handover and inspect the deduplicated delivery state.

## Architecture notes

The repository still includes the original Redmine proxy and reliability-lab structure, with the additional drone domain layered on top. The new fleet logic is intentionally small, testable, and safe for local demonstration. See the docs folder for the full implementation roadmap and demo guide.