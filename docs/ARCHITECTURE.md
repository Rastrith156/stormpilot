# Architecture

StormPilot follows a layered design that keeps the original reliability lab intact while adding a simplified fleet-operations domain.

## Layers

1. Presentation layer
   - Static dashboard at `/ui`
   - Mobile tracking page at `/drone/connect`
   - Google Maps as the main visual layer when a browser key is configured

2. API layer
   - `/api/fleet` exposes the four seeded demo drones
   - `/api/device/telemetry` accepts validated GPS samples
   - `/api/delivery/confirm` provides idempotent delivery confirmation
   - `/api/email/test` validates SMTP setup without blocking the platform
   - `/api/redmine` remains the original read-only project workspace integration

3. Domain logic
   - Each drone has a seeded identity, route summary, status, and mission metadata
   - The backend validates coordinates, timestamp, and accuracy before accepting incoming GPS updates
   - Delivery confirmation is deterministic and deduplicated at the `delivery_id` level

4. Infrastructure
   - Docker Compose and monitoring remain available
   - Prometheus metrics and health endpoints are still active
   - The app remains local and safe for university demonstration without production claims

## Design constraints

- No Google Maps key is required for the backend to function; the app degrades gracefully.
- Secrets stay in environment variables or files only.
- The implementation intentionally avoids pretending to control physical aircraft or real delivery infrastructure.