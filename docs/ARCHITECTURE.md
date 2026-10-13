# Architecture

StormPilot follows a layered design that keeps the original reliability lab intact while adding a simplified fleet-operations domain.

## Layers

1. Presentation layer
   - Static dashboard at `/ui`
   - Mobile tracking page at `/drone/connect`
   - Google Maps JavaScript API as the map layer when the browser-restricted key is configured
   - Single bounded five-second polling loop; map markers, polylines and heatmap overlays update in place
   - Distinct configuration, network, API-key and fleet-data error states; backend operation is independent of map availability

2. API layer
   - `/api/fleet` exposes four stable identities and database-backed accepted telemetry, delivery details, observed distance, GPS paths, and live/stale/offline states
   - `/api/traffic` returns bounded accepted-observation aggregates with time, drone, mission, source and cell-size filters
   - `/api/twin` returns simulated corridors, zones and deterministic scenarios; `PUT /api/twin/config` persists operator-defined virtual configuration
   - `/api/device/register` and `/api/device/revoke` manage device credentials only after server-side administrator bearer-token verification
   - `/api/device/telemetry` accepts authenticated, validated GPS samples
   - `/api/delivery/confirm` requires administrator authorization and an existing server state of `ARRIVED`
   - `/api/config` exposes only the intentionally public browser Maps key and readiness state
   - `/api/email/test` validates SMTP setup without blocking the platform
   - `/api/redmine` remains the original read-only project workspace integration

3. Persistence layer
   - SQLite stores the four drone identities, device credential hashes, accepted/rejected telemetry samples, delivery records, event history, notification outbox and validated digital-twin configuration.
   - Database initialization is versioned and runs automatically on startup through a migration-style bootstrap.
   - Telemetry records are accepted or rejected with safe diagnostic metadata instead of silently mutating the fleet view.
   - Delivery transitions and geofence arrival evidence are appended to delivery history and the event log.
   - A separate notification worker claims outbox jobs, sends email with SMTP STARTTLS, and retries failures with bounded backoff.
   - Traffic queries are time-bounded and capped at 5,000 records; map polling does not issue Google Routes requests.

4. Domain logic
   - Four stable drone identities exist even with no phones connected. Missing accepted GPS produces `OFFLINE` and a null position; no placeholder GPS is exposed.
   - The backend validates coordinates, timestamp, and accuracy before accepting incoming GPS updates
   - Delivery confirmation is deterministic and deduplicated at the `delivery_id` level
   - Invalid or out-of-order GPS samples are rejected; GPS jumps are flagged and excluded from cumulative observed distance
   - Arrival detection uses a configurable radius and records device-reported accuracy; it is evidence, not guaranteed location truth
   - A fix is `STALE` when either its server-received time or device-reported time is over 120 seconds old; stale coordinates are not displayed as current
   - Planned map legs join stored pickup/destination coordinates directly; actual paths are accepted phone GPS only
   - Traffic data is persisted GPS, labelled live or historical; virtual twin datasets are separately labelled simulated

5. Infrastructure
   - Docker Compose and monitoring remain available
   - Prometheus metrics and health endpoints are still active
   - The app remains local and safe for university demonstration without production claims

## Design constraints

- No Google Maps key is required for the backend to function; the app degrades gracefully.
- The browser Maps key is restricted by HTTP referrer and API; it is public by design and is not a server-side secret.
- Administrator secrets stay server-side in environment configuration or a secret manager. Missing administrator configuration fails closed for sensitive mutations.
- Registration and revocation write audit events. A shared administrator token is a minimal demo boundary, not individual identity or production IAM.
- The implementation intentionally avoids pretending to control physical aircraft or real delivery infrastructure.
- Digital-twin zones are not official aviation restrictions; simulation results do not assert physical-flight safety.
- Non-local deployments still require HTTPS, an authenticated proxy/network boundary and production administrator identity/role management.