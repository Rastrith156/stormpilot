# StormPilot — Resilient Drone Dispatch Lab

An advanced portfolio DevOps project: pretend your drone-dispatch API serves emergency supply deliveries. Kill a service replica in a disposable lab, observe Kubernetes restore capacity, generate bounded traffic, and practice safe releases. No real drones, cloud account or paid service are required.

## Architecture

```text
Team users → Caddy HTTPS → OAuth2 Proxy (OIDC) → private StormPilot API → Redmine REST API
GitHub push → tests + Compose/kind integration → Trivy gate → GHCR SHA-tagged image
                                                         ↓ manual lab release
Kubernetes Service → 2–5 API replicas ← HPA (metrics-server)
                    ↑ probes / Deployment reconciliation
Chaos script → delete ONE lab pod → replacement pod

Separate Docker Compose demo:
Users → API ← Prometheus ← Grafana
```

**Two deployment modes:** Compose includes monitoring of the Compose API. Kubernetes includes replication, probes, disruption budget and optional HPA. The supplied Compose dashboard does NOT monitor Kubernetes. Integrating kube-prometheus-stack is an extension, not preconfigured functionality.

## Included
- Dependency-free Python API with structured logs and Prometheus metrics.
- HTTP service and mocked Redmine integration tests; non-root, read-only container configuration.
- GitHub Actions tests, vulnerability gate, GHCR publishing, SBOM and provenance.
- Bounded Waitress WSGI production server, HTTPS gateway and OIDC authentication profile.
- Kubernetes rolling deployments, readiness/liveness/startup probes and resource budgets for the lab.
- CPU HPA manifest (requires metrics-server), PodDisruptionBudget, restricted pod security.
- A namespace- and context-restricted pod-deletion experiment.
- Provisioned Grafana dashboard and Prometheus alert rules in Compose.
- Read-only Redmine REST API integration with server-side credentials and a responsive project workspace.
- Bounded local load generator, demo guide, rollback and incident runbook.

## Prerequisites
Python 3.11+; Docker with Compose v2. Kubernetes path additionally needs kubectl and kind installed. Image pulls need internet. Use a disposable laptop cluster; allow roughly 4 GB of available RAM. Examples use Bash (Linux/macOS/WSL2). No software is installed by this archive.

## 1. Run the tests
```bash
python3 -m unittest discover -s tests -v
```

## 2. Local monitoring demo
```bash
cp .env.example .env
# Edit .env and choose a strong, unique local Grafana password.
docker compose -f compose.yaml -f compose.dev.yaml up --build -d
curl http://localhost:8080/dispatch
python3 scripts/load.py --seconds 30 --workers 4
```
- API: http://localhost:8080
- Prometheus: http://localhost:9090 — inspect Targets and Alerts.
- Grafana: http://localhost:3000 — username `admin`, password from `.env`; open StormPilot Reliability.
- Metrics: http://localhost:8080/metrics

To trigger the availability alert: `docker compose -f compose.yaml -f compose.dev.yaml stop api`, wait at least 40 seconds, inspect Prometheus Alerts, then `docker compose -f compose.yaml -f compose.dev.yaml start api`. Alertmanager is NOT configured: rules appear in Prometheus, but no email or Slack is sent. The example server does not normally emit 5xx; the server-error rule is a future instrumentation example.

## 3. Kubernetes self-healing demo
```bash
kind create cluster --name stormpilot
docker build -t stormpilot:local .
kind load docker-image stormpilot:local --name stormpilot
kubectl --context kind-stormpilot apply -f k8s/base.yaml
kubectl --context kind-stormpilot -n stormpilot-lab rollout status deployment/stormpilot
kubectl --context kind-stormpilot -n stormpilot-lab get pods
CONFIRM_CHAOS=yes bash scripts/chaos.sh
```
The script refuses to act unless the current context is `kind-stormpilot` and at least two replicas are ready. Review before running. Deleting a pod is intentional downtime for that replica; do not run against production. Direct pod deletion bypasses PodDisruptionBudget protection, so the script limits the experiment to one pod. A single-node kind cluster does not demonstrate node or zone resilience.

For API access, run in a separate terminal:
```bash
kubectl --context kind-stormpilot -n stormpilot-lab port-forward service/stormpilot 8081:80
```
Then `curl http://localhost:8081/dispatch`. Port-forward selects a single pod; it can break if that pod is deleted and is NOT a valid end-to-end availability or load-balancing benchmark. Restart it if necessary.

## 4. Optional autoscaling
Install a compatible metrics-server in the disposable cluster following its official installation instructions. Verify `kubectl --context kind-stormpilot top pods -n stormpilot-lab` succeeds before enabling HPA. Some local kind setups need lab-only kubelet TLS adjustments; do not weaken production TLS.
```bash
kubectl --context kind-stormpilot apply -f k8s/hpa.yaml
python3 scripts/load.py --port 8081 --seconds 120 --workers 8
kubectl --context kind-stormpilot -n stormpilot-lab get hpa,pods -w
```
Run the watch in another terminal, Ctrl+C to stop. Scaling depends on actual CPU and sampling windows, and is not guaranteed by a fixed load. For a realistic test use an in-cluster load client through the Service; port-forward targets just one pod. After enabling HPA, avoid repeatedly applying `replicas: 2`, which can temporarily reset HPA-managed scale.

## 5. CI and releases
Target repository: `Rastrith156/stormpilot` (private by default). See docs/GITHUB_SETUP.md for secure publishing instructions. Create the repository and push this folder to `main`. The workflow tests and scans PRs, validates both Compose profiles, runs the local Compose and disposable kind integration checks, then publishes on main pushes only after all gates pass. The Kubernetes test checks API readiness before and after one-pod deletion; HPA receives server-side manifest validation, but actual scaling is not tested. Ensure Actions has package write permission. Pull requests do not receive publishing credentials. A fixable CRITICAL vulnerability blocks publication: update the affected base image rather than disabling the check. Versions in these files are starting pins, not claims that the images are currently vulnerability-free. For stricter supply-chain controls, pin images by digest and Actions by reviewed commit SHA.

Publishing is configured; automatic cluster deployment is intentionally NOT configured. After a successful build, use the actual lowercase owner/repository and full commit SHA:
```bash
kubectl --context kind-stormpilot -n stormpilot-lab set image deployment/stormpilot api=ghcr.io/OWNER/REPOSITORY:FULL_COMMIT_SHA
kubectl --context kind-stormpilot -n stormpilot-lab rollout status deployment/stormpilot --timeout=120s
```
Replace placeholders before running. The image must be publicly pullable, or configure a namespace-scoped registry pull secret securely. Never commit a token. For a local offline release, build a new unique local tag and load it into kind first. See docs/RUNBOOK.md for rollback.

## 6. Redmine workspace
Open `http://localhost:8080/ui`. The development Compose overlay binds StormPilot only to loopback. Without Redmine credentials the dispatch console still works and the workspace shows setup guidance. Enable REST API access in Redmine, copy `.env.example` to `.env`, set `REDMINE_URL` to the instance base URL and `REDMINE_API_KEY` to an API key, then restart Compose. The key is sent only from the StormPilot server to Redmine and is never returned to the browser. The workspace displays visible projects and recently updated open issues with links to their Redmine pages.

The integration uses Redmine's JSON REST API and performs read-only requests. It does not mutate issues. Kubernetes manifests and the direct local development server are for the disposable lab only; do not expose either directly to a team network.

## 7. Private team deployment
The production Compose profile places the API on a private container network behind Caddy HTTPS and OAuth2 Proxy. Sign-in uses your existing OIDC provider; access is restricted by an allowed email domain. Before starting it, configure an internal DNS name for the host, a confidential OIDC client with redirect URI `https://YOUR_HOSTNAME/oauth2/callback`, and a Redmine API key with only the read permissions required by the team. The team hostname must resolve to this host and client devices must trust Caddy's internal CA. Do not port-forward or publish the API container port. Choose a Docker subnet that does not overlap your LAN, VPN, or other container networks and set `DOCKER_PRIVATE_SUBNET` consistently; OAuth2 Proxy trusts forwarded headers only from that gateway subnet.

Copy `.env.example` to `.env`, replace every example hostname/domain, and set `APP_HOSTNAME`, `OIDC_ISSUER_URL`, `OIDC_CLIENT_ID`, `OIDC_ALLOWED_EMAIL_DOMAIN`, and `REDMINE_URL`. Create the secret files named by the `*_FILE` settings. Store the Redmine API key and OIDC client secret as plain text files with restricted permissions and no trailing newline. Generate the OAuth cookie secret as exactly 32 random bytes, for example with `openssl rand 32 > secrets/oauth_cookie_secret`; that file is binary and must not have a newline. Keep the `secrets` directory out of backups that are not encrypted and never commit it.

Run the production gateway:
```bash
docker compose -f compose.yaml -f compose.production.yaml --profile production config --quiet
docker compose -f compose.yaml -f compose.production.yaml --profile production up --build -d
```
Caddy uses its internal CA for this private deployment. Export its root certificate with `docker compose -f compose.yaml -f compose.production.yaml exec gateway cat /data/caddy/pki/authorities/local/root.crt` and distribute it to team devices through a trusted device-management channel. Keep the Caddy data volume, which holds the CA private key, backed up and access-controlled. For a public-facing deployment, use a publicly trusted certificate and have the security team review the network, identity-provider, and secret-management configuration first.

The private profile authenticates requests at the gateway and the app remains stateless. Configure your OIDC provider and Redmine instance before relying on the service; this repository does not provision either external system. The Kubernetes files remain a local reliability lab and are not the production deployment path.

## Project status and limitations
StormPilot is an internal Redmine portal and educational reliability lab, not drone-control software. Production access requires the documented OIDC gateway; the direct development server and Kubernetes lab do not provide user authentication. The project does not configure your OIDC provider, Redmine permissions, DNS, or device trust for you. Review current vulnerability scans and your organization's deployment controls before production use.

## Cleanup
```bash
docker compose -f compose.yaml -f compose.dev.yaml down -v
kind delete cluster --name stormpilot
```
Cleanup removes the local demo environment; save screenshots or evidence first. GHCR images and the GitHub repository are unaffected.

## Portfolio extensions (not implemented)
Argo CD GitOps; Argo Rollouts canary analysis; OpenTelemetry traces; kube-prometheus-stack with ServiceMonitor; Terraform for a budget-controlled cloud environment; signed images and admission policies; persistent dispatch queues; external uptime probes and measured recovery SLOs.

## Convenience commands
`make test`, `make run`, `make smoke`, `make up`, `make down`, `make kind-up`, and `make chaos` are provided. `make up` starts the loopback-only development Compose overlay and requires a configured `.env`. Run `make run` in a separate terminal before `make smoke`. Use the production profile above for team access.
