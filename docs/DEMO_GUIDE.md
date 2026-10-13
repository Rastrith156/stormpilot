# Demo guide

## Local demo

```bash
python -m unittest discover -s tests -v
```

For a local PowerShell session, set `HOST`, `PORT`, a random `STORMPILOT_ADMIN_TOKEN`, and optionally `GOOGLE_MAPS_API_KEY`, then run:

```powershell
$env:HOST = "127.0.0.1"
$env:PORT = "8081"
$env:STORMPILOT_ADMIN_TOKEN = "<random-secret>"
$env:GOOGLE_MAPS_API_KEY = "<browser-restricted-key-or-empty>"
python app/server.py
```

Use an unoccupied port; 8080 may already be in use by Compose. Then open:

- http://127.0.0.1:8081/ui
- http://127.0.0.1:8081/drone/connect

## Scenario

1. Ask an administrator for the enrollment token through a secure channel.
2. Open the drone connect page, choose an identity and device ID, and enter the admin token for first-time registration.
3. Grant browser geolocation permission; only accepted phone GPS is drawn as an observed position.
4. Inspect `PHONE_GPS`, `STALE` and `OFFLINE` states, selected-drone accuracy/distance, traffic cells and delivery records on the fleet dashboard.
5. Complete delivery handover only after backend geofence evidence has moved the delivery to `ARRIVED`; the handover API also requires the administrator token.
6. Toggle the digital-twin layer to show simulated routes/zones. These are demonstrations, not official airspace restrictions or flight-safety guidance.

## Demo notes

- Four drone identities are seeded immediately; drones without phone GPS are `OFFLINE` and have no fabricated coordinates.
- Without a Google Maps key, the fleet APIs and dashboard data remain available with an explicit map configuration state.
- Configure a browser-restricted Maps key with the Maps JavaScript API restriction. Development and production referrers, quotas and possible billing are documented in the README.
- The administrator token is server-configured and is not embedded or persisted by the browser page.
- SMTP warnings appear clearly when Gmail credentials are not configured.
- No real drone control or physical aircraft integration is claimed. Virtual zones are not official aviation restrictions.