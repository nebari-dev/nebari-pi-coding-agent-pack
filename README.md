# nebari-pi-pack

Helm chart that extends Nebari's data-science JupyterHub stack with a Pi coding-agent workflow.

## What this chart adds

- Pi launcher service in Hub (`/services/pi-launcher/`, port `10300`)
- Pi named-server profiles (`pi-small`, `pi-medium`, `pi-large`)
- Pi home/quick-access UI customizations
- Optional Pi session sharing RBAC + handlers
- Optional shared-skills distribution via PVC-subpath synced from immutable URL artifacts
- Optional relay subsystem (core + dummy/slack/whatsapp adapters)

## Architecture (high level)

- JupyterHub remains control plane.
- User requests Pi via launcher.
- JupyterHub spawns/controls named server `pi` per user.
- Pi pod serves browser terminal via `jhsingle_native_proxy` + `ttyd` + `pi` CLI.
- Pi image also includes `nebari_app_*` wrappers (`deploy/status/logs/stop/delete/doctor`) for jhub-app lifecycle operations.

## Install

```bash
helm dependency update PACKS/nebari-pi-pack
helm upgrade --install data-science-pack PACKS/nebari-pi-pack \
  --namespace data-science \
  --set nebariapp.hostname=jupyter.nebari.local \
  --wait --timeout 40m
```

For RECREATE.md local flow, use ready-made examples:

```bash
# Pi core (no relay)
helm upgrade --install data-science-pack PACKS/nebari-pi-pack \
  --namespace data-science \
  -f PACKS/nebari-pi-pack/examples/values-local.yaml \
  --wait --timeout 40m

# Pi + relay
helm upgrade --install data-science-pack PACKS/nebari-pi-pack \
  --namespace data-science \
  -f PACKS/nebari-pi-pack/examples/values-relay.yaml \
  --wait --timeout 50m

# Pi + pvc-subpath shared-skills with release rotation sync job
helm upgrade --install data-science-pack PACKS/nebari-pi-pack \
  --namespace data-science \
  -f PACKS/nebari-pi-pack/examples/values-pvc-shared-skills-sync.yaml \
  --wait --timeout 50m
```

### Reliable local Pi testing profile (recommended)

Use `examples/values-local-reliable.yaml` for more deterministic local behavior:
- registry-backed Pi image (no `k3d image import` race)
- immutable Pi image ref (`@sha256:...`)
- prePullers disabled (lower disk churn)
- Pi user pods pinned to one node in local multi-node clusters

```bash
helm upgrade --install data-science-pack PACKS/nebari-pi-pack \
  --namespace data-science \
  -f PACKS/nebari-pi-pack/examples/values-local-reliable.yaml \
  --wait --timeout 45m
```

If you rebuild the Pi image, update both:
- `pi.image.repository` + `pi.image.tag`
- `jupyterhub.custom.pi-image` (prefer digest-pinned value)

For best local dev stability, prefer a single-node k3d cluster for Pi work.

#### Local k3d registry flow (recommended)

```bash
# Create local registry attached to the cluster network (one-time)
k3d registry create pi-registry.localhost --port 5001 --default-network k3d-nebari-local

# Configure node-side registry mirrors (one-time per cluster lifecycle)
cat >/tmp/k3s-registries-pi.yaml <<'YAML'
mirrors:
  "k3d-pi-registry.localhost:5000":
    endpoint:
      - "http://k3d-pi-registry.localhost:5000"
  "localhost:5001":
    endpoint:
      - "http://k3d-pi-registry.localhost:5000"
YAML
for n in k3d-nebari-local-server-0 k3d-nebari-local-agent-0 k3d-nebari-local-agent-1; do
  docker cp /tmp/k3s-registries-pi.yaml "$n":/etc/rancher/k3s/registries.yaml
done
docker restart k3d-nebari-local-agent-0 k3d-nebari-local-agent-1 k3d-nebari-local-server-0

# Build Pi image (includes pi CLI + nebari_app_* wrappers)
docker build -t nebari-pi-agent:local-slim PACKS/nebari-pi-pack/images/pi-agent

# Tag + push immutable Pi image to local registry
TAG=dev-$(date +%Y%m%d-%H%M%S)
docker tag nebari-pi-agent:local-slim localhost:5001/nebari-pi-agent:${TAG}
docker push localhost:5001/nebari-pi-agent:${TAG}

# Update values-local-reliable.yaml with the new tag/digest, then deploy
helm upgrade --install data-science-pack PACKS/nebari-pi-pack \
  --namespace data-science \
  -f PACKS/nebari-pi-pack/examples/values-local-reliable.yaml \
  --wait --timeout 45m
```

## Migrate from existing data-science-pack

Use same release and namespace:

```bash
helm upgrade data-science-pack PACKS/nebari-pi-pack \
  --namespace data-science \
  -f <your-values.yaml> \
  --wait --timeout 40m
```

## Important values

- `pi.enabled`
- `pi.image.*`
- `pi.sharing.enabled`
- `pi.sharedSkills.enabled`
- `pi.sharedSkills.mode` (`pvc-subpath` only)
- `pi.sharedSkills.sync.*` (chart-managed PVC sync/rotation pipeline for URL artifacts)
- `relay.enabled`
- `relay.deployMode` (`configmap` or `image`)
- `relay.core.piAgentDir`

Also review `jupyterhub.custom.*` Pi fields in `values.yaml`, consumed by Hub config modules (including `pi-coding-agent-dir` for writable Pi runtime state location).
Keep `jupyterhub.custom.pi-coding-agent-dir` aligned with `relay.core.piAgentDir` when relay is enabled.

### Production-style shared-skills updates (PVC mode)

When `pi.sharedSkills.mode=pvc-subpath`, enable chart-managed sync/rotation and feed it immutable artifacts built from your skills repo CI.

Recommended values:

- `pi.sharedSkills.sync.enabled=true`
- `pi.sharedSkills.sync.source.type=url`
- `pi.sharedSkills.sync.source.url=https://.../shared-skills-<version>.tar.gz`
- `pi.sharedSkills.sync.source.sha256=<artifact-sha256>`
- `pi.sharedSkills.sync.releaseId=<same-version-or-git-sha>`
- `pi.sharedSkills.sync.keepReleases=<N>`

Behavior on each Helm install/upgrade:
- syncs archive into `<pvcSubPath>/releases/<release-id>/...`
- updates symlink: `<pvcSubPath>/current -> releases/<release-id>`
- prunes old releases (keep latest N)

This gives reproducible roll-forward/rollback via `helm upgrade` without rebuilding the whole cluster.

#### Private artifact endpoints

If your artifact URL requires auth, set:

- `pi.sharedSkills.sync.source.auth.secretName=<secret-name>`
- `pi.sharedSkills.sync.source.auth.tokenKey=<key>` (default `token`)

Create secret example:

```bash
kubectl -n data-science create secret generic pi-shared-skills-artifact-token \
  --from-literal=token='<your-bearer-token>'
```

#### GitHub Actions CI workflow (skills repo)

Use the dedicated skills repository (`nenb/nebari-agent-skills`) to publish immutable artifacts.

A workflow template is provided at:

- `.github/workflows/publish-pi-shared-skills-artifact.yml`

Run this workflow in the **skills repo**, not in the Pi pack repo. It should publish:
- `shared-skills-<sha>.tar.gz` (immutable asset)
- `shared-skills.tar.gz` (rolling latest asset)
- matching `.sha256` files
- release tag: `pi-shared-skills-<sha>`

Then point `pi.sharedSkills.sync.source.url` to either:
- immutable URL (recommended), or
- latest URL (convenient, less reproducible)

If you use immutable URL, set `releaseId` to the same tag/sha.

## Security notes

- No hardcoded long-lived Pi tooling token in source.
- M4 tooling token is generated/stored in chart-managed Secret (`pi-secrets`) and injected into Hub env.
- Relay secrets use keep/lookup behavior for stable upgrades.
