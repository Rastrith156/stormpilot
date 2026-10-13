# Implementation plan

## Milestone 1: Foundation and durable state (completed in this iteration)

- Kept the existing HTTP server and Redmine proxy intact.
- Added a SQLite-backed persistence layer for drone registrations, telemetry samples, delivery records, event history, and notification outbox state.
- Added database initialization and migration-style bootstrapping with explicit table creation and indexes.
- Preserved the existing fleet, config, DNS/Redmine, and health endpoints while adding safe server-side validation for GPS samples and delivery confirmation.
- Added milestone tests for database initialization, delivery state transitions, and idempotent confirmation.

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