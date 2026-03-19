# nebari-pi-pack

Helm chart that extends Nebari's data-science JupyterHub stack with a Pi coding-agent workflow.

## What this chart adds

- Pi launcher service in Hub (`/services/pi-launcher/`, port `10300`)
- Pi named-server profiles (`pi-small`, `pi-medium`, `pi-large`)
- Pi home/quick-access UI customizations
- Optional Pi session sharing RBAC + handlers
- Optional relay subsystem (core + dummy/slack/whatsapp adapters)
- Shared skills delivered from the Pi runtime image (no PVC sync job)

## Architecture

- JupyterHub remains the control plane.
- User requests Pi via launcher.
- JupyterHub spawns/controls named server `pi` per user.
- Pi pod serves browser terminal via `jhsingle_native_proxy` + `ttyd` + `pi` CLI.
- Pi startup includes `--skill /opt/nebari/baked-skills/shared-skills`.

## Install

```bash
helm dependency update .
helm upgrade --install data-science-pack . \
  --namespace data-science \
  --wait --timeout 40m
```

### Example values

```bash
# Local baseline
helm upgrade --install data-science-pack . \
  --namespace data-science \
  -f examples/values-local.yaml \
  --wait --timeout 40m

# Local reliable
helm upgrade --install data-science-pack . \
  --namespace data-science \
  -f examples/values-local-reliable.yaml \
  --wait --timeout 45m

# Relay profile
helm upgrade --install data-science-pack . \
  --namespace data-science \
  -f examples/values-relay.yaml \
  --wait --timeout 50m

# Production-style baked skills profile
helm upgrade --install data-science-pack . \
  --namespace data-science \
  -f examples/values-baked-skills.yaml \
  --wait --timeout 45m
```

## Important values

- `pi.image.*`
- `pi.sharedSkills.enabled`
- `pi.sharedSkills.mode` (`image`)
- `pi.sharedSkills.imagePath`
- `jupyterhub.custom.pi-image`
- `jupyterhub.custom.pi-skills-path`
- `jupyterhub.custom.pi-coding-agent-dir`
- `relay.*`

When relay is enabled, keep these aligned:

- `relay.core.piAgentDir`
- `jupyterhub.custom.pi-coding-agent-dir`

## Build Pi image with baked skills

`images/pi-agent/Dockerfile` expects:

- `PI_SKILLS_URL` (required)
- `PI_SKILLS_SHA256` (optional)

Example:

```bash
docker build \
  -t quay.io/openteams/pi-coding-agent-demo:baked-<tag> \
  --build-arg PI_SKILLS_URL="https://github.com/nenb/nebari-agent-skills/releases/download/<release>/shared-skills-<sha>.tar.gz" \
  --build-arg PI_SKILLS_SHA256="<sha256>" \
  images/pi-agent
```

Then set your Helm values to the pushed image tag/digest:

- `pi.image.repository`
- `pi.image.tag`
- `jupyterhub.custom.pi-image` (prefer digest pin)

## Notes

- Legacy PVC-based shared-skills sync is removed from runtime flow.
- Existing running Pi sessions keep old image/skills until pod restart.
