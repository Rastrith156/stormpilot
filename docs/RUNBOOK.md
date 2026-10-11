# Incident and release runbook

## Failed rollout
1. Inspect `kubectl --context kind-stormpilot -n stormpilot-lab get pods`.
2. Inspect `kubectl --context kind-stormpilot -n stormpilot-lab describe deployment stormpilot` and describe any failed pod.
3. Read `kubectl --context kind-stormpilot -n stormpilot-lab logs deployment/stormpilot --tail=100`.
4. ImagePullBackOff: verify tag, package visibility, registry authentication and network.
5. CrashLoopBackOff: inspect previous logs with `kubectl logs POD --previous -n stormpilot-lab --context kind-stormpilot`.
6. For an unsuccessful NEW release, run `kubectl --context kind-stormpilot -n stormpilot-lab rollout undo deployment/stormpilot`.
7. Run rollout status again and verify `/readyz` and `/dispatch`. Rollback needs a previous deployment revision; it cannot repair the first deployment.

## HPA shows unknown
Check `kubectl top pods`, metrics-server availability, and CPU requests. Wait for samples. No metrics means no CPU-based autoscaling. The HPA is reactive and cannot guarantee no errors during bursts.

## Monitoring unavailable
Compose: run `docker compose ps` and `docker compose logs --tail=100 prometheus grafana api`. Check Prometheus Targets. A green API health endpoint checks the process only, not a database or downstream dependency.

## Safe chaos checklist
- Disposable `kind-stormpilot` context only; inspect current-context before any manual mutation.
- Two ready replicas before deletion; one pod only.
- Capture pod names, experiment time, replacement time and logs.
- Verify a replacement becomes ready; stop if recovery fails.
- Never describe rollout-wait duration as user-visible downtime.
- PDB helps eviction-based voluntary disruptions, not direct deletions or node loss.
