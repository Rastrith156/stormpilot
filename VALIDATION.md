# Validation status — Milestone 5

## Automated tests

- Baseline before Milestone 5: 56 Python tests passed.
- Current full suite: **89 Python tests passed** with `python -m unittest discover -s tests -v`. No existing test was weakened or removed.
- New module `tests/test_milestone5.py` (23 tests), all against isolated temporary databases and synthetic credentials:
  - **Authentication**: bootstrap CLI (`--password-stdin`) creates exactly one administrator and refuses a second; server-side role matrix for `ADMIN`/`OPERATOR`/`VIEWER` plus 401 for anonymous mutations; password rotation revokes sessions and requires the new password; login throttling, CSRF, session expiry and legacy shared-token bypass are covered here and in the Milestone 4 tests; secrets (login password, session cookie, device bearer token, SMTP/Redmine sentinel values) never appear in HTTP logs, API responses, `/metrics` or frontend bundles.
  - **Phone GPS and delivery**: admin-authorized enrollment, unique device-to-drone association (409 on conflict), revoke, rejected telemetry, then recovery/re-enrollment; invalid, duplicate, out-of-order, implausible-speed, stale and future telemetry rejection with persisted rejection reasons; per-device 429 rate limit; `OFFLINE` to `PHONE_GPS` to `STALE` transitions; distance accumulation that ignores rejected samples and GPS jumps; geofence arrival evidence; idempotent handover completion with exactly one queued receipt; the phone page exposes permission, last-fix, accuracy, connection and delivery status and embeds no coordinates.
  - **Email reliability**: `SMTP_LOCAL_TEST_MODE` marks notifications `SENT` without opening SMTP; transient failures retry with 60s/120s bounded backoff and stop at the retry budget; exhausted failures stay `FAILED` and remain visible in `/readyz` counts and `stormpilot_notification_exhausted_total`; duplicate completion requests do not queue duplicate receipts; the email body is built from persisted delivery ID, drone/device ID, observed distance and completion time; the SMTP password never reaches logs, responses or static files.
  - **Google Maps**: keyless and whitespace-only keys produce a clean `map_ready: false` fallback; `/api/config` exposes only `google_maps_api_key`, `demo_mode`, `map_ready`; no Google API key pattern exists in any static bundle; the frontend keyless branch is verified.
  - **Health and observability**: `/readyz` returns 503 on a missing required table and on a storage exception while `/healthz` stays 200; `stormpilot_ready 0` and `stormpilot_storage_available 0` are reported; metric labels are restricted to `route`, `status`, `reason`, `state` from fixed enums (exactly 6 rejection reasons, 8 delivery states, 4 outbox states, no per-device or per-delivery labels); no secrets or session identifiers appear in `/metrics`.
  - **Planning/simulation/replay**: optimizer proposals respect drone availability and configured virtual restricted zones; stale baselines and missing approval are covered in the Milestone 4 tests; replay bounds (limit, 30-day window, exactly one selector), gap reporting without coordinate interpolation, and secret-free replay/report JSON are asserted.

## Local smoke and static checks

- `python scripts/smoke.py --port 8081` against an isolated temporary data directory: **PASS** (readiness, identity, health, login UI, device auth UI, config, four-drone fleet, traffic, twin, dispatch, metrics, Redmine). The server was stopped afterwards. Cleanup of `C:\Users\rastr\AppData\Local\Temp\stormpilot_smoke_3d377b108bf146ac8f1d112390dfd095` was blocked by the command safety wrapper, so that temporary SQLite file remains outside the repository.
- `node --check app/static/app.js`: **PASS**.
- `python scripts/check_inline_js.py`: **PASS** (inline script of `/drone/connect`).
- `git diff --check`: **PASS** (only LF/CRLF conversion notices, no whitespace errors).
- Production gateway tests verify public Caddy TLS (not an internal CA), same-origin phone geolocation, map CSP allowances, and writable non-root database storage in Compose and the Kubernetes smoke manifest.
- Docker Compose configuration validated without starting anything, using a temporary placeholder env file outside the repository:
  - `docker compose -f compose.yaml config --quiet` exits 0.
  - `docker compose -f compose.yaml -f compose.dev.yaml config --quiet` exits 0.
  - `docker compose -f compose.yaml -f compose.production.yaml config --quiet` exits 0.
- Documented startup commands were checked against the repository: `python -m app.admin bootstrap` (exercised by tests), `python app/server.py` (exercised by the smoke run), `docker compose exec api python -m app.admin bootstrap` (the `app` package is copied into the image), and the Makefile `up`/`down` targets match the compose file combinations above.
- No containers were started, stopped or restarted during this validation.

## Not verified against external services (pending)

- **Live Google Maps render — PENDING.** No valid browser key was available and no browser automation is installed, so only the keyless fallback and configuration handling were verified. A real Maps render with HTTP/HTTPS referrer restrictions requires a browser and a key.
- **Real-phone GPS — PENDING.** No physical phone submitted GPS during validation. Enrollment, telemetry validation, arrival and revocation are covered by automated API tests; the phone page status markers are asserted statically. On-device permission prompts and true satellite fixes were not exercised.
- **Live SMTP/Gmail delivery — PENDING.** No Gmail App Password was available. Local test mode and all failure/retry paths were verified with SMTP mocked; a real send was never attempted (automated tests must not send email by design).
- **Browser automation of login, logout, planning, simulation and replay — PENDING.** The in-app browser-control setup failed before opening the local app, and neither Playwright nor Selenium is installed in the repository; no large new dependency was added. These flows were exercised through the HTTP API and static page checks instead.
- **Public-domain deployment — PENDING.** Caddy is configured for automatic public certificates and allows the same-origin phone-GPS permission and external map assets, but DNS, firewall ports 80/443, OIDC callback/login and phone GPS must be verified on the actual host/domain. See [the deployment checklist](docs/DOMAIN_DEPLOYMENT.md).
- **Container/Kubernetes deployment — PENDING.** The GitHub verify job built and scanned the container, but the prior Compose startup and Kubernetes smoke jobs did not complete successfully. Docker is unavailable locally, so the real containers and revised Kubernetes smoke manifest were not run here. The Kubernetes manifest is explicitly single-replica and ephemeral; use the persistent Docker Compose deployment for a domain.

## Database and backup status

- The tracked database `.stormpilot_data/stormpilot.db` was not reset, deleted, or migrated backwards. It already carried an uncommitted tracked change at session start, which was preserved; running the test suite additionally triggers the normal import-time, idempotent initialization against the default data directory. All Milestone 5 tests use isolated temporary data directories that are deleted afterwards.
- No backup file was created or altered during this session. Recommended before any upgrade: `sqlite3 .stormpilot_data/stormpilot.db ".backup '.stormpilot_data/stormpilot.backup.db'"` while running, or copy the file while the application is stopped. To roll back a migration: stop the application, restore the backup, restart; writes made after the backup are lost.

This is an educational fleet-operations and digital-twin foundation, not a verified production deployment or an aviation safety system.
