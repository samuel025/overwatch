#!/usr/bin/env bash
# Smoke test for the Kubernetes and Loki sources against REAL services (not recorded responses).
# Needs docker, k3d and kubectl on PATH. Creates a throwaway cluster and a Loki container, checks discovery, the generated
# checks and the source queries, and removes everything afterwards.
#   PYTHON=.venv/bin/python scripts/k8s-smoke/run.sh
set -euo pipefail
cd "$(dirname "$0")/../.."
PY="${PYTHON:-python3}"; CL=ow-smoke; API_PORT=6551; LOKI_PORT=3101
export KUBECONFIG="$(mktemp)"
trap 'k3d cluster delete "$CL" >/dev/null 2>&1 || true; docker rm -f ow-smoke-loki >/dev/null 2>&1 || true; rm -f "$KUBECONFIG"' EXIT

echo "== cluster"
k3d cluster create "$CL" --servers 1 --agents 0 --api-port "127.0.0.1:$API_PORT" --no-lb \
  --kubeconfig-update-default=false --kubeconfig-switch-context=false --wait >/dev/null
k3d kubeconfig get "$CL" > "$KUBECONFIG"
kubectl apply -f scripts/k8s-smoke/demo.yaml >/dev/null
kubectl -n shop wait --for=condition=available deploy/web deploy/api --timeout=120s >/dev/null
TOKEN="$(kubectl -n shop create token overwatch --duration=1h)"

echo "== loki"
docker run -d --name ow-smoke-loki -p "127.0.0.1:$LOKI_PORT:3100" grafana/loki:3.2.0 -config.file=/etc/loki/local-config.yaml >/dev/null
for _ in $(seq 1 40); do curl -sf "localhost:$LOKI_PORT/ready" | grep -q ready && break; sleep 3; done
sleep 8  # the demo worker must have restarted at least once, and Loki must accept writes

"$PY" scripts/k8s-smoke/check.py "$TOKEN" "https://127.0.0.1:$API_PORT" "http://127.0.0.1:$LOKI_PORT"
