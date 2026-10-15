# Public-domain deployment

StormPilot's supported single-server web deployment uses Docker Compose, the
production overlay, OAuth2 Proxy and Caddy. It is an operations/demo application,
not a flight-control or aviation-safety system. A successful CI run does not
verify your DNS, VPS firewall, identity provider, Maps key, phone GPS hardware,
mail provider or disaster-recovery process.

The manifests under `k8s/` are for disposable single-replica smoke testing only:
their `emptyDir` database is lost when the pod is recreated. Do not use those
manifests for a persistent domain deployment.

## Before starting

- Use a maintained Linux server with Docker Engine and the Docker Compose plugin.
- Point the domain's DNS `A` record to the server's public IPv4 address. Add an
  `AAAA` record only if the server is reachable over IPv6. Allow inbound TCP
  ports 80 and 443 in both the cloud firewall and the server firewall. Caddy
  needs public reachability on these ports to obtain and renew trusted HTTPS
  certificates.
- Configure an OIDC provider and a client for the exact callback URL
  `https://<your-domain>/oauth2/callback`. Choose an email domain you control.
- Set a strong, unique Grafana admin password. Configure a Google Maps browser
  key only if you need Google Maps; restrict it to the deployed HTTPS origin and
  Maps JavaScript API.

## Configure secrets and services

1. Clone the repository on the server and check out the release commit you
   intend to deploy. Keep the checkout and data volume on persistent storage.
2. Copy `.env.example` to `.env`, restrict the file to the deployment user
   (`chmod 600 .env`), and replace every example value. In particular set
   `APP_HOSTNAME`, `OIDC_ISSUER_URL`, `OIDC_CLIENT_ID`,
   `OIDC_ALLOWED_EMAIL_DOMAIN`, and `GRAFANA_ADMIN_PASSWORD`. Add optional
   Maps, Redmine and SMTP settings only when they are configured.
3. Create the Compose secret files without adding real credentials to Git.
   The OIDC client secret must match the OIDC client. Create a random OAuth
   cookie secret and a Redmine key file (it may be empty if Redmine is unused):

   ```sh
   umask 077
   mkdir -p secrets
   chmod 700 secrets
   openssl rand -base64 32 > secrets/oauth_cookie_secret
   ```

   Save the OIDC client secret in `secrets/oidc_client_secret`. If Redmine is
   enabled, save its API key in `secrets/redmine_api_key`; otherwise create an
   empty file at that path. The API container runs as UID 10001, so make these
   mounted secret files readable to that container user while keeping the
   parent `secrets` directory private on the host (for example, mode `0644` on
   the individual files inside the mode-`0700` directory). The directory is
   ignored by Git; verify `git status` before deployment.
4. Check that the resolved production configuration is valid, then start the
   production profile:

   ```sh
   docker compose -f compose.yaml -f compose.production.yaml --profile production config --quiet
   docker compose -f compose.yaml -f compose.production.yaml --profile production up --build -d --wait
   ```

   Only Caddy publishes public ports. The API, Prometheus and Grafana host ports
   are bound to loopback; do not change them to public interfaces without an
   explicit access-control plan. Do not put a second proxy in front unless its
   forwarded-header and trusted-proxy behavior is configured and reviewed.

## First login and validation

1. Create the first local StormPilot administrator interactively:

   ```sh
   docker compose -f compose.yaml -f compose.production.yaml --profile production exec api \
     python -m app.admin bootstrap --username <administrator-name>
   ```

   The OIDC proxy is an outer access gate; StormPilot's own named accounts and
   role checks remain enabled behind it. Bootstrap an account and create any
   required operator/viewer accounts separately. Do not assume OIDC login
   alone creates an application account.
2. Open `https://<your-domain>/ui`. Confirm the certificate is publicly trusted,
   OIDC authentication succeeds, and the local StormPilot account can sign in.
   If phone GPS is required, test `/drone/connect` on the actual phone and grant
   geolocation permission; the site must remain on HTTPS.
3. Check `/healthz`, `/readyz`, and `/metrics` from an authorized session or
   from the trusted server network. Confirm the dashboard's Google Maps layer
   or its OpenStreetMap fallback works without browser console CSP errors.
4. Back up the persistent `stormpilot-data` volume before upgrades. Confirm
   that a backup can be restored. SQLite is intended for this single-server,
   single-application deployment; do not scale the API to multiple independent
   replicas with separate local databases.

## Operational requirements

- Caddy uses automatic public HTTPS. If DNS or ports 80/443 are not reachable,
  certificate issuance/renewal will fail; do not substitute an untrusted
  internal certificate for a public site.
- Keep `.env` and `secrets/` out of source control and backups that are not
  encrypted and access-controlled. Rotate OIDC and Redmine credentials using
  the identity provider and deployment process.
- Maintain off-server, encrypted backups and a documented restore procedure.
  A Docker volume is persistence, not a backup.
- Configure host security updates, firewalling, monitoring, log retention and
  alerting. Do not expose Prometheus or Grafana directly to the public Internet.
- The `/drone/connect` page needs same-origin geolocation permission. Browsers
  require a secure context for real device GPS; loopback HTTP is only suitable
  for local testing.
- The Bengaluru routes are simulated. Optimizer estimates and virtual zones
  are not flight plans, official restrictions, or flight-safety guidance.
