# Implementation plan

## Milestone 1: Foundation and tests

- Keep the existing HTTP server and Redmine proxy intact.
- Add fleet regression tests for demo drones, telemetry validation, idempotent confirmation, and SMTP warnings.
- Confirm the tests fail before implementing the new path.

## Milestone 2: Fleet backend

- Seed four demo drones with route, status, and mission metadata.
- Add `/api/fleet` and `/api/config` responses.
- Add `/api/device/telemetry` validation logic and safe state persistence.
- Add `/api/delivery/confirm` with duplicate protection.

## Milestone 3: Frontend and map experience

- Rebuild the main dashboard as a fleet operations interface.
- Add a geolocation registration page for the four demo devices.
- Use Google Maps when available and give a clear offline fallback when not.

## Milestone 4: Operational polish

- Refresh docs and environment setup.
- Support Gmail SMTP warnings and a verified test-email path.
- Validate the CI test suite and local smoke checks.

## Milestone 5: Final verification

- Run the full project tests and ensure no regressions in the Redmine paths.
- Record the exact working features and remaining limitations.