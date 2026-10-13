# Implementation plan

## Completed foundations

- Preserved the original Redmine/reliability-lab endpoints and monitoring surfaces.
- Added SQLite-backed fleet registrations, hashed device credentials, accepted/rejected telemetry, delivery lifecycle evidence and an idempotent email outbox.
- Added four stable fleet identities, Google Maps with a no-key fallback, bounded traffic aggregates and a simulated digital-twin baseline.

## Milestone 4: authenticated operational planning

### Individual administrator authentication

- Migration 004 adds admin identities, session state, login throttling and simulation records without discarding existing fleet data.
- PBKDF2 password hashes, opaque hashed sessions, HTTP-only SameSite cookies, CSRF validation, eight-hour expiry, revocation and role-based protected mutations replace the shared-token authorization path.
- Roles are `ADMIN`, `OPERATOR` and `VIEWER`; the first ADMIN is created only by the one-time interactive CLI bootstrap.
- Account creation, disabling, role change, credential rotation and privileged-action decisions are auditable without secret values.
- Local HTTP requires the explicit `STORMPILOT_COOKIE_SECURE=false` setting; HTTPS deployments retain `true`.

### Mission optimizer

- Compare current assignments, nearest eligible and deterministic greedy optimized assignments using persisted drones/deliveries and recent accepted phone GPS.
- Report route, reposition and duration estimates; nominal range/deadline/virtual-zone/corridor constraints; unassigned reasons; workload and distance savings.
- Preserve the proposal baseline and recheck live delivery state before explicit ADMIN/OPERATOR-approved changes to eligible `CREATED` assignments.
- Limitations: no battery telemetry, road routing or physical flight-safety model; straight-line estimates are explicitly labelled. The deterministic heuristic is not AI.

### What-if simulation

- Capture baseline, scenario inputs and `SIMULATED` result snapshots without mutating live telemetry, delivery states or notifications.
- Cover offline drone, missed deadline, unavailable virtual corridor, remaining range, shared-corridor approach, infeasible mission and hypothetical high-priority arrival.
- Persist and replay the exact result; require explicit operator/admin approval before applying proposals for eligible persisted delivery rows. Hypothetical deliveries remain virtual.

### Route replay and forensics

- Query one drone or delivery over a time range of at most 30 days; cap accepted GPS at 2,000 points and event history at 500.
- Sort real accepted samples, identify long recording gaps, and never synthesize/interpolate coordinates.
- Provide read-only step/playback controls and concise JSON incident-report export.

## Upgrade / rollback

1. Preserve the existing database and make a separate SQLite backup before applying migration 004.
2. Apply the additive migration through normal application initialization; never delete or reset the existing database.
3. Bootstrap the first named ADMIN with `python -m app.admin bootstrap --username <name>` and an interactively entered strong password. Shared-token environment variables do not authorize protected routes.
4. Verify login, session/CSRF settings, roles, existing Redmine/device/delivery behavior, and backups before exposing the service.
5. Rollback only by stopping the application and restoring the pre-migration backup. This removes migration-004 data and all subsequent writes.

## Quality gates

Run `python -m unittest discover -s tests -v`, JavaScript syntax checks, `python scripts/smoke.py --port <free-port>`, `git diff --check`, and `docker compose config --quiet`. Use isolated test data and an available port; do not stop Docker Desktop or existing containers for ordinary checks.
