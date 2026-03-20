---
name: observability
description: Use when Pi needs to troubleshoot logs/metrics/traces or verify LGTM (Grafana/Loki/Mimir/Tempo) access in local k3s, cloud Kubernetes, or any generic K8s setup.
---

# Observability Skill (Nebari + Generic Kubernetes)

Use this skill whenever the user asks to:
- debug deployments using logs/events/rollout state,
- verify monitoring is working,
- find Grafana/Loki/Prometheus/Mimir/Tempo endpoints,
- check whether Pi users can access observability data.

This skill supports:
- **local k3s/k3d**,
- **cloud Kubernetes** (EKS/GKE/AKS),
- **generic Kubernetes** environments.

---

## Core design awareness (important)

In Nebari Pi environments, the common architecture is:
- Pi/JupyterHub workloads in an app namespace (often `data-science`),
- LGTM stack in `monitoring`,
- optional `pi-observability-proxy` and `pi-debug` CLI for curated debugging.

### Access paths you must evaluate
1. **pi-debug path** (if available):
   - `pi-debug logs|events|rollout|doctor`
   - Usually fronted by `pi-observability-proxy`.
2. **Direct in-cluster LGTM path**:
   - Loki/Mimir/Tempo/Grafana service DNS from within Pi pod.
3. **Port-forward/browser path**:
   - useful when running from local workstation or when pod egress is restricted.

Do **not** assume only one path exists. Detect and use what works.

---

## Workflow

## 0) Detect environment + tools
Run:
```bash
kubectl config current-context
kubectl get ns
```

Then detect whether `pi-debug` exists:
```bash
command -v pi-debug >/dev/null && echo "pi-debug available" || echo "pi-debug not available"
```

If `pi-debug` is available, test quickly:
```bash
pi-debug doctor --app pi || true
```

---

## 1) Discover LGTM endpoints (do not hardcode)

Preferred discovery:
```bash
kubectl get svc -A | grep -Ei 'grafana|loki|mimir|tempo|prometheus'
```

Export discovered endpoints (examples):
```bash
export GRAFANA_SVC="http://<grafana-svc>.<ns>.svc.cluster.local:80"
export LOKI_URL="http://<loki-svc>.<ns>.svc.cluster.local:3100"
export MIMIR_URL="http://<mimir-gateway-svc>.<ns>.svc.cluster.local"
export TEMPO_URL="http://<tempo-svc>.<ns>.svc.cluster.local:3200"
export PROM_URL="http://<prometheus-svc>.<ns>.svc.cluster.local:9090"
```

For Nebari local defaults, likely names are:
- `lgtm-pack-grafana.monitoring.svc.cluster.local`
- `lgtm-pack-loki.monitoring.svc.cluster.local`
- `lgtm-pack-mimir-gateway.monitoring.svc.cluster.local`
- `lgtm-pack-tempo.monitoring.svc.cluster.local`

---

## 2) Verify access fast (health checks)

```bash
curl -fsS "${GRAFANA_SVC}/login" >/dev/null && echo grafana_ok
curl -fsS "${LOKI_URL}/ready" && echo
curl -fsS "${MIMIR_URL}/ready" && echo
curl -fsS "${PROM_URL}/-/ready" && echo
```

If any fail, capture exact HTTP/code/error and continue with Kubernetes diagnostics.

---

## 3) Get logs for debugging (full-access mode)

If the deployment allows broad direct access, use Loki directly.

### Namespace-scoped logs
```bash
curl -sG "${LOKI_URL}/loki/api/v1/query_range" \
  --data-urlencode 'query={namespace="data-science"}' \
  --data-urlencode 'limit=200' \
  --data-urlencode 'direction=BACKWARD'
```

### Broad logs across namespaces (when needed)
```bash
curl -sG "${LOKI_URL}/loki/api/v1/query_range" \
  --data-urlencode 'query={job=~".+"}' \
  --data-urlencode 'limit=200' \
  --data-urlencode 'direction=BACKWARD'
```

### Pod-focused logs
```bash
curl -sG "${LOKI_URL}/loki/api/v1/query_range" \
  --data-urlencode 'query={namespace="<ns>",pod=~"<pod-prefix>.*"}' \
  --data-urlencode 'limit=200' \
  --data-urlencode 'direction=BACKWARD'
```

If `pi-debug` exists, you can also run:
```bash
pi-debug logs --app pi --since 30m
pi-debug events --app pi
pi-debug rollout --app pi
pi-debug doctor --app pi
```

---

## 4) Deployment failure triage checklist

Always include these checks when user says "deployment not working":

```bash
kubectl get pods -A
kubectl get events -A --sort-by=.lastTimestamp | tail -n 120
kubectl get deploy -A
```

Then correlate with Loki query by namespace/pod label.

Look specifically for:
- `ImagePullBackOff`, `ErrImagePull`
- `CrashLoopBackOff`
- readiness/liveness probe failures
- scheduling failures (`FailedScheduling`)
- storage/PVC binding issues

---

## 5) Cloud/public route checks (optional)

If a public hostname/path is expected:
```bash
export GRAFANA_URL="https://<host>/monitoring"
curl -k -fsS "${GRAFANA_URL}/api/health"
```

If this fails but in-cluster service works, focus on ingress/gateway/route/auth layer.

---

## 6) Kubernetes diagnostics for broken observability path

```bash
kubectl get pods -A | grep -Ei 'grafana|loki|mimir|tempo|prometheus|otel'
kubectl get svc -A | grep -Ei 'grafana|loki|mimir|tempo|prometheus|otel'
kubectl logs -n monitoring ds/opentelemetry-collector-agent --tail=120 || true
kubectl get networkpolicy -A
```

---

## Reporting format (required)

Always report:
1. Exact commands run.
2. Endpoints resolved.
3. Access path used (`pi-debug`, direct in-cluster, or port-forward/public URL).
4. Whether full logs are currently accessible.
5. Concrete next actions.

---

## Reference
- `references/lgtm-queries.md` for reusable queries.
