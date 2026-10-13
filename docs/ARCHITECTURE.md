# Architecture

StormPilot follows a layered design that keeps the original reliability lab intact while adding a simplified fleet-operations domain.

## Layers

1. Presentation layer
   - Static dashboard at `/ui`
   - Mobile tracking page at `/drone/connect`
   - Google Maps as the main visual layer when a browser key is configured

2. API layer
   - `/api/fleet` exposes the four seeded demo drones
   - `/api/device/register` and `/api/device/revoke` manage demo device credentials
   - `/api/device/telemetry` accepts authenticated, validated GPS samples
   - `/api/delivery/confirm` provides idempotent delivery confirmation
   - `/api/email/test` validates SMTP setup without blocking the platform
   - `/api/redmine` remains the original read-only project workspace integration

3. Persistence layer
   - SQLite stores the demo fleet state, device credential hashes, telemetry samples, delivery records, event history, and notification outbox.
   - Database initialization is versioned and runs automatically on startup through a migration-style bootstrap.
   - Telemetry records are accepted or rejected with safe diagnostic metadata instead of silently mutating the fleet view.
   - Delivery transitions and geofence arrival evidence are appended to delivery history and the event log.
   - A separate notification worker claims outbox jobs, sends email with SMTP STARTTLS, and retries failures with bounded backoff.

4. Domain logic
   - Each drone has a seeded identity, route summary, status, and mission metadata
   - The backend validates coordinates, timestamp, and accuracy before accepting incoming GPS updates
   - Delivery confirmation is deterministic and deduplicated at the `delivery_id` level
   - Invalid or out-of-order GPS samples are rejected; GPS jumps are flagged and excluded from cumulative observed distance
   - Arrival detection uses a configurable radius and records device-reported accuracy; it is evidence, not guaranteed location truth

5. Infrastructure
   - Docker Compose and monitoring remain available
   - Prometheus metrics and health endpoints are still active
   - The app remains local and safe for university demonstration without production claims

## Design constraints

- No Google Maps key is required for the backend to function; the app degrades gracefully.
- Secrets stay in environment variables or files only.
- The implementation intentionally avoids pretending to control physical aircraft or real delivery infrastructure.
- Device enrollment and revocation do not yet have separate administrator authorization; enforce a trusted network or authenticated proxy boundary and HTTPS before any non-demo exposure.