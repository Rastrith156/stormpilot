# Ten-minute portfolio demo

1. Explain the story: emergency delivery dispatch must survive a process failure.
2. Run the six tests and show the non-root Dockerfile.
3. Start Compose, generate traffic and show requests/second in Grafana.
4. Stop/start the Compose API and show the Prometheus availability alert.
5. Show two Kubernetes replicas, run the guarded one-pod chaos experiment, and show the replacement pod.
6. Explain why one-node kind and port-forward do not prove production availability.
7. Show the CI workflow: test → build → critical vulnerability gate → publish with SBOM/provenance.
8. Explain manual release and rollback. Demonstrate HPA only if metrics-server has been configured.

## Evidence to collect
Save screenshots of passing CI, scan result, image digest, Grafana panels, original/replacement pod names, and HPA observations if enabled. Record actual results, not invented reliability claims.

## Suggested project abstract
StormPilot is a local reliability-engineering lab for a simulated drone-dispatch API. It combines container hardening, CI security gates, metrics, Kubernetes reconciliation and controlled fault injection to demonstrate how delivery pipelines and platform controls contribute to service resilience.

## Honest resume wording (after you run it)
Built a containerized reliability lab with GitHub Actions, image scanning, Prometheus/Grafana and Kubernetes; demonstrated pod replacement using a guarded chaos experiment and documented release rollback procedures.

## Stretch goal
Measure request success rate through a real Service client during repeated experiments, publish the raw evidence and derive an evidence-based recovery objective. Do not claim 99.9% uptime from a single short demo.
