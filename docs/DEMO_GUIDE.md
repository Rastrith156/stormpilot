# Demo guide

## Local demo

```bash
python -m unittest discover -s tests -v
python app/server.py
```

Then open:

- http://127.0.0.1:8080/ui
- http://127.0.0.1:8080/drone/connect

## Scenario

1. Open the drone connect page.
2. Choose a demo drone and device ID.
3. Grant browser geolocation permission.
4. Watch location data arrive in the fleet dashboard.
5. Inspect the active/delivery metrics and confirm a delivery handover.

## Demo notes

- The four drone identities are seeded and visible immediately.
- Without a Google Maps key, the app remains in safe offline demo mode.
- SMTP warnings appear clearly when Gmail credentials are not configured.
- No real drone control or physical aircraft integration is claimed.