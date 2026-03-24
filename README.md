# nebari-pi-pack

Helm chart that extends Nebari's data-science JupyterHub stack with a Pi coding-agent workflow.

## What this chart adds

- Pi launcher service in Hub (`/services/pi-launcher/`, port `10300`)
- Pi named-server profiles (`pi-small`, `pi-medium`, `pi-large`)
- Pi home/quick-access UI customizations
- Optional Pi session sharing RBAC + handlers
- Optional Pi Session Viewer service (`/services/pi-session-viewer/`, port `10400`)
- Optional relay subsystem (core + dummy/slack/whatsapp adapters)
- Shared skills delivered from the Pi runtime image (no PVC sync job)

## Architecture

- JupyterHub remains the control plane.
- User requests Pi via launcher.
- JupyterHub spawns/controls named server `pi` per user.
- Pi pod serves browser terminal via `jhsingle_native_proxy` + `ttyd` + `pi` CLI.
- Pi startup includes `--skill /opt/nebari/baked-skills/shared-skills`.
- Pi startup loads the OpenAI Codex device-code OAuth override extension (`-e /opt/nebari/extensions/openai-codex-device-auth.ts`).
- Pi startup loads session sharing commands extension (`-e /opt/nebari/extensions/session-share.ts`).
- Pi startup also loads `pi-self-learning` extension (`-e /usr/local/lib/node_modules/pi-self-learning/extensions/self-learning.ts`).

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
- `jupyterhub.custom.pi-session-viewer-*` (enable/port/S3 settings)
- `relay.*`

When relay is enabled, keep these aligned:

- `relay.core.piAgentDir`
- `jupyterhub.custom.pi-coding-agent-dir`

## Build Pi image with baked skills

`images/pi-agent/Dockerfile` now bakes skills directly from local repository files:

- `skills/shared-skills/jhub-deploy/SKILL.md`
- `skills/shared-skills/observability/SKILL.md`
- `skills/shared-skills/web-browser/SKILL.md`
- `skills/shared-skills/gog/SKILL.md`
- `skills/shared-skills/notion/SKILL.md`
- `skills/shared-skills/video-frames/SKILL.md`
- `skills/shared-skills/markdown-converter/SKILL.md`

Example:

```bash
docker build \
  -f images/pi-agent/Dockerfile \
  -t quay.io/openteams/pi-coding-agent-demo:baked-<tag> \
  .
```

Then set your Helm values to the pushed image tag/digest:

- `pi.image.repository`
- `pi.image.tag`
- `jupyterhub.custom.pi-image` (prefer digest pin)

## Notes

- Legacy PVC-based shared-skills sync is removed from runtime flow.
- Existing running Pi sessions keep old image/skills until pod restart.
