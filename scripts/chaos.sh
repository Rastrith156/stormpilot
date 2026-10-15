#!/usr/bin/env bash
set -euo pipefail
# Refuse every cluster except the explicitly named disposable kind lab.
context="$(kubectl config current-context)"
[[ "$context" == "kind-stormpilot" ]] || { echo "Refusing: use context kind-stormpilot only."; exit 1; }
[[ "${CONFIRM_CHAOS:-}" == "yes" ]] || { echo "Run with CONFIRM_CHAOS=yes after reviewing this script."; exit 1; }
ready="$(kubectl -n stormpilot-lab get deployment stormpilot -o jsonpath='{.status.readyReplicas}')"
[[ "${ready:-0}" -ge 1 ]] || { echo "Need at least one ready replica."; exit 1; }
pod="$(kubectl -n stormpilot-lab get pods -l app=stormpilot -o jsonpath='{.items[0].metadata.name}')"
echo "Deleting the single lab pod: $pod"
start=$SECONDS
kubectl -n stormpilot-lab delete pod "$pod" --wait=true --timeout=60s
kubectl -n stormpilot-lab rollout status deployment/stormpilot --timeout=120s
echo "Pod replacement + rollout wait: $((SECONDS-start)) seconds; this is NOT measured user-visible downtime."
kubectl -n stormpilot-lab get pods -l app=stormpilot
