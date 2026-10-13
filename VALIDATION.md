# Validation status

## Milestone 3 checks completed
- Pre-change baseline: 30 Python tests passed.
- Current full suite: 45 Python tests passed with `python -m unittest discover -s tests -v`.
- `node --check app/static/app.js` and `git diff --check` passed.
- Pylance reported no problems in the API, database and HTTP transport modules.
- Development and production Docker Compose configurations validated. The local config check used a temporary placeholder for the required Grafana password; no services were started or restarted.
- The expanded smoke script passed on `http://127.0.0.1:8081`: readiness, identity, health, UI, map configuration, four-drone fleet, traffic, digital twin, dispatch, metrics and Redmine.
- Browser verification confirmed the no-key fallback, API-driven offline state for all four drones without fabricated coordinates, selected-drone details and traffic/twin panels.
- The smoke server used an isolated temporary data directory, which was removed after validation. Docker Desktop and existing containers were left running.
- Import-time initialization advanced the tracked `.stormpilot_data/stormpilot.db` through migration 003 and refreshed the four seeded registration timestamps. Comparison with the original file found no other changed pre-existing table contents.

## Not verified against external services
- A live Google Maps JavaScript API key was not available, so a Google-rendered map, browser-key restrictions and billable usage were not exercised.
- No phone submitted live GPS during validation; GPS handling was covered by automated API tests.
- Gmail SMTP delivery was not live-tested.
- Compose configuration was validated only; the changed image was not built or deployed. The pre-existing local container on port 8080 was not restarted and returned 404 for the new `/api/config` and `/api/fleet` endpoints.

This is an educational fleet-operations and digital-twin foundation, not a verified production deployment or an aviation safety system.
