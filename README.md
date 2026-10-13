# StormPilot — Resilient Drone Dispatch Lab

An advanced portfolio DevOps project: pretend your drone-dispatch API serves emergency supply deliveries. Kill a service replica in a disposable lab, observe Kubernetes restore capacity, generate bounded traffic, and practice safe releases. No real drones, cloud account or paid service are required.

## Architecture

```text
GitHub push → tests + Compose/kind integration → Trivy gate → GHCR SHA-tagged image
                                                         ↓ manual release
Local users → Kubernetes Service → 2–5 API replicas ← HPA (metrics-server)
                                    ↑ probes / Deployment reconciliation
Chaos script → delete ONE lab pod → replacement pod

Separate Docker Compose demo:
Users → API ← Prometheus ← Grafana
```

**Two deployment modes:** Compose includes monitoring of the Compose API. Kubernetes includes replication, probes, disruption budget and optional HPA. The supplied Compose dashboard does NOT monitor Kubernetes. Integrating kube-prometheus-stack is an extension, not preconfigured functionality.

## Included
- Dependency-free Python API with structured logs and Prometheus metrics.
- Six HTTP integration tests; non-root, read-only container configuration.
- GitHub Actions tests, vulnerability gate, GHCR publishing, SBOM and provenance.
- Kubernetes rolling deployments, readiness/liveness/startup probes and resource budgets.
- CPU HPA manifest (requires metrics-server), PodDisruptionBudget, restricted pod security.
- A namespace- and context-restricted pod-deletion experiment.
- Provisioned Grafana dashboard and Prometheus alert rules in Compose.
- Read-only OpenProject API integration with server-side credentials and a responsive project workspace.
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
docker compose up --build -d
curl http://localhost:8080/dispatch
python3 scripts/load.py --seconds 30 --workers 4
```
- API: http://localhost:8080
- Prometheus: http://localhost:9090 — inspect Targets and Alerts.
- Grafana: http://localhost:3000 — username `admin`, password from `.env`; open StormPilot Reliability.
- Metrics: http://localhost:8080/metrics

To trigger the availability alert: `docker compose stop api`, wait at least 40 seconds, inspect Prometheus Alerts, then `docker compose start api`. Alertmanager is NOT configured: rules appear in Prometheus, but no email or Slack is sent. The example server does not normally emit 5xx; the server-error rule is a future instrumentation example.

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
Target repository: `Rastrith156/stormpilot` (private by default). See docs/GITHUB_SETUP.md for secure publishing instructions. Create the repository and push this folder to `main`. The workflow tests and scans PRs, runs Compose and disposable kind integration tests, then publishes on main pushes only after all gates pass. The Kubernetes test checks API readiness before and after one-pod deletion; HPA receives server-side manifest validation, but actual scaling is not tested. Ensure Actions has package write permission. Pull requests do not receive publishing credentials. A fixable CRITICAL vulnerability blocks publication: update the affected base image rather than disabling the check. Versions in these files are starting pins, not claims that the images are currently vulnerability-free. For stricter supply-chain controls, pin images by digest and Actions by reviewed commit SHA.

Publishing is configured; automatic cluster deployment is intentionally NOT configured. After a successful build, use the actual lowercase owner/repository and full commit SHA:
```bash
kubectl --context kind-stormpilot -n stormpilot-lab set image deployment/stormpilot api=ghcr.io/OWNER/REPOSITORY:FULL_COMMIT_SHA
kubectl --context kind-stormpilot -n stormpilot-lab rollout status deployment/stormpilot --timeout=120s
```
Replace placeholders before running. The image must be publicly pullable, or configure a namespace-scoped registry pull secret securely. Never commit a token. For a local offline release, build a new unique local tag and load it into kind first. See docs/RUNBOOK.md for rollback.

## 6. OpenProject workspace
Open `http://localhost:8080/ui`. Without OpenProject configuration the dispatch console still works and the workspace shows setup guidance. To connect an instance, copy `.env.example` to `.env`, set `OPENPROJECT_URL` to the instance base URL and `OPENPROJECT_API_TOKEN` to a read-only API token, then restart Compose. The token is sent only from the StormPilot server to OpenProject and is never returned to the browser. The workspace displays visible projects and recently updated work packages with links to their OpenProject pages.

The integration uses OpenProject's REST API v3 and performs read-only requests. It does not provide OpenProject authentication for StormPilot users, mutate work packages, or make the local app suitable for public exposure. In Kubernetes, set `OPENPROJECT_URL` in `k8s/base.yaml`; optionally create a Secret with `kubectl --context kind-stormpilot -n stormpilot-lab create secret generic openproject-credentials --from-literal=api-token="$OPENPROJECT_API_TOKEN"`. Store the token in the shell environment first. Use a trusted local environment and protect the secret through your cluster's normal controls.

## Project status and limitations
The archive provides implementation and instructions, not a deployed cloud service. See VALIDATION.md for checks actually executed. Kubernetes recovery, autoscaling, image scans and GitHub publishing need your environment for end-to-end verification. This is an educational HTTP server, not production-ready drone software. It has no authentication, TLS ingress, durable dispatch storage, multizone failover or automatic rollback. Do not expose it publicly.

## Cleanup
```bash
docker compose down -v
kind delete cluster --name stormpilot
```
Cleanup removes the local demo environment; save screenshots or evidence first. GHCR images and the GitHub repository are unaffected.

## Portfolio extensions (not implemented)
Argo CD GitOps; Argo Rollouts canary analysis; OpenTelemetry traces; kube-prometheus-stack with ServiceMonitor; Terraform for a budget-controlled cloud environment; signed images and admission policies; persistent dispatch queues; external uptime probes and measured recovery SLOs.

## Convenience commands
`make test`, `make run`, `make smoke`, `make up`, `make down`, `make kind-up`, and `make chaos` are provided. `make up` requires a configured `.env`. Run `make run` in a separate terminal before `make smoke`. The direct Python and Docker commands above work without Make.
