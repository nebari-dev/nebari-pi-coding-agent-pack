---
name: jhub-deploy
description: Use when a user asks Pi to build and deploy arbitrary web apps through JupyterHub named servers using direct Hub API calls (no nebari_app_* tools).
---

# JHub Arbitrary Deploy Skill

Use this skill when the user wants agents to:
- build arbitrary frontend/backend apps,
- deploy them through JupyterHub,
- manage lifecycle (status/stop/delete) via Hub API directly,
- avoid framework-specific preference and avoid mandatory image push.

This skill piggybacks on **JupyterHub named servers**.

---

## 1) Preconditions

Resolve Hub API context from runtime:

```bash
API_URL="${NEBARI_HUB_API_URL:-${JUPYTERHUB_API_URL:-}}"
API_TOKEN="${NEBARI_HUB_API_TOKEN:-${JUPYTERHUB_API_TOKEN:-}}"
HUB_USER="${JUPYTERHUB_USER:-${NB_USER:-}}"

[ -n "$API_URL" ] || echo "Missing API_URL"
[ -n "$API_TOKEN" ] || echo "Missing API_TOKEN"
[ -n "$HUB_USER" ] || echo "Missing HUB_USER"
```

Validate token can read own user:

```bash
curl -fsS -H "Authorization: token $API_TOKEN" \
  "$API_URL/users/$HUB_USER?include_stopped_servers=true" >/dev/null
```

If this fails, stop and report missing scopes/credentials.

### 1.1) Runtime dependency preflight (MUST run)

Before selecting command/image strategy, verify required runtime dependencies for the target app.

Examples:
- shell-out binaries (`pdftotext`, `ffmpeg`, etc.)
- language runtimes/toolchains
- expected package managers/build tools

If required binaries are missing in target image/runtime:
- either switch to image mode,
- or include bootstrap installation/fallback logic explicitly,
- or fail fast with clear remediation.

---

## 2) Deployment modes (default = no image push)

### A) **Source-on-home mode** (conditional, not assumed)
- Build/install app on user home/workspace (`/home/jovyan/...`)
- Spawn named server to run command from that path
- **No image push required**

**Critical rule:** use source-on-home mode only if shared storage between build context and target named server is confirmed.

### B) **Bootstrap-on-target mode** (safe default when storage is uncertain)
- Treat target named server as isolated storage
- Build/write artifacts inside target server startup flow
- Prefer Python bootstrap wrapper that prepares files then `exec`s runtime

### C) Image mode (optional)
- Use custom image when runtime deps cannot be satisfied in source/bootstrap modes
- Can use local cluster image import for local k3d/k3s
- Registry push is optional, only needed when cluster cannot access local image

Do not prefer framework-native templates. Treat all apps as arbitrary command-based runtimes.

### 2.1) Storage topology preflight (MUST run)

Before using source-on-home mode, verify whether target named server shares storage with where artifacts were built.

Minimum checks:
1. If target server already exists, inspect/compare PVC claim identity with current runtime server.
2. If target server does not exist yet, treat storage as **unknown** and do **not** assume shared home.
3. If unknown or different, require bootstrap-on-target or image mode.

Failure message should explicitly say source-on-home was rejected due to non-shared/unknown storage topology.

### 2.2) Image resolution preflight (MUST run when `profile_image` is empty)

`profile_image: ""` can resolve to a broken default image.

Required behavior:
1. Resolve a known-good image before spawn when `profile_image` is empty.
2. Resolution order:
   - `JUPYTER_IMAGE_SPEC` from current runtime, else
   - image from a currently ready server for same user, else
   - explicit configured fallback image.
3. If spawn fails with pull errors (`ImagePullBackOff`/`ErrImagePull`), retry once with resolved known-good image.

---

## 3) App spawn contract

Assume deployment supports a command-based `user_options` contract for named servers.
Preferred payload shape:

```json
{
  "profile": "<optional-profile>",
  "app": {
    "cwd": "/home/jovyan/apps/myapp",
    "command": "<runtime command with {port} placeholder>",
    "env": {"KEY": "VALUE"}
  },
  "profile_image": "<optional-image>"
}
```

If your Hub uses a different key shape, adapt payload to the deployment’s configured spawner hooks.

### 3.1) Default resilient command pattern (REQUIRED)

To avoid brittle spawn failures, default to this pattern:

1. Use a **single executable command string** in `app.command`.
2. Keep `{port}` in the final runtime invocation.
3. For multi-step setup (write files/build/start), prefer a quote-minimized Python bootstrap wrapper:

```bash
python3 -c exec(__import__('base64').b64decode('<BASE64_PY>').decode()) {port}
```

Where `<BASE64_PY>` decodes to Python that:
- creates/writes required files under app cwd,
- performs any build step,
- and finishes with `os.execv(...)` to start the long-running server process.

Avoid by default:
- complex `bash -c` strings,
- chained shell operators (`&&`, pipes, heredocs),
- fragile quoting that may break under proxy/spawner wrapping.

---

### 3.2) Required preflight guard (MUST enforce)

Before making the spawn API call, validate that `app.command` is actually runnable in the target server.

Hard-fail guard:
- If `app.command` references a local binary path (for example `/home/jovyan/apps/.../bin/...`) and
- the payload does **not** include a bootstrap/build step that creates that binary in the same spawn command,
- then **do not spawn**. Return a clear remediation message instead.

Required behavior:
1. Prefer bootstrap-first commands (Python base64 wrapper) that write/build/exec in one deterministic flow.
2. If using a direct binary path, verify it is present + executable before spawn.
3. If verification is not possible, treat as unsafe and require bootstrap mode.

Minimum remediation message should explain:
- which executable path is missing,
- that source/build artifacts were not created in this server context,
- and that the payload must be regenerated with bootstrap/build included.

### 3.3) Command-size guard (MUST enforce)

Large embedded payloads in `app.command` can fail with argument-length errors.

Required rule:
- Keep `app.command` small (target: <= 4KB; hard-stop around 8KB).
- Put medium payloads in env vars or files created by a small bootstrap.
- For large artifacts, use durable artifact URLs/object storage or image mode.
- Do not embed very large base64 blobs directly in `app.command`.

Practical payload thresholds:
- total spawn payload <= 64KB: usually safe for inline/env patterns
- env payload total <= 256KB: acceptable for medium bundles
- env payload > 512KB or compressed artifact > 5MB: prefer artifact store or image mode
- if command + env strategy repeatedly hits size/time limits, switch to image mode

## 4) Spawn arbitrary app via direct Hub API

```bash
APP_NAME="my-app"
PAYLOAD_FILE="/tmp/${APP_NAME}-spawn.json"

cat > "$PAYLOAD_FILE" <<'JSON'
{
  "profile": "",
  "app": {
    "cwd": "/home/jovyan/apps/my-app",
    "command": "python3 -c exec(__import__('base64').b64decode('<BASE64_PY>').decode()) {port}",
    "env": {
      "NODE_ENV": "production"
    }
  },
  "profile_image": ""
}
JSON

curl -sS -X POST \
  -H "Authorization: token $API_TOKEN" \
  -H "Content-Type: application/json" \
  "$API_URL/users/$HUB_USER/servers/$APP_NAME" \
  --data-binary @"$PAYLOAD_FILE"
```

Expected: HTTP `201` or `202`.

---

## 5) Status, readiness, URL

Check server status:

```bash
curl -sS -H "Authorization: token $API_TOKEN" \
  "$API_URL/users/$HUB_USER?include_stopped_servers=true"
```

Canonical URL:
- `/user/<username>/<app-name>/`

Pending URL:
- `/hub/spawn-pending/<username>/<app-name>`

### 5.1) Mandatory post-deploy verification loop (MUST pass)

Never report "deployed" until **all** checks below pass.
If any check fails, report deployment as failed (or verification-incomplete), include diagnostics, and stop claiming success.

Required checks:

1. **Hub readiness check (CLI)**
   - Poll user server state until `ready=true` and not `pending`.
   - Timeout must be explicit.

2. **Interim/proxy failure check (CLI)**
   - Ensure app route is not stuck on spawn pending/interim endpoints.
   - Treat these as failure states:
     - `/hub/spawn-pending/...`
     - `/_temp/jhub-app-proxy/...`

3. **Mandatory browser smoke check (HEADLESS BROWSER)**
   - Use `pi-browser-smoke` from inside the Pi runtime.
   - Run **internal in-cluster smoke first** (app health truth source).
   - Then run **external smoke** (real user path) when external base URL is configured.

   Internal-first example:

   ```bash
   INTERNAL_BASE_URL="${NEBARI_BROWSER_INTERNAL_BASE_URL:-http://proxy-public}"
   EXTERNAL_BASE_URL="${NEBARI_BROWSER_BASE_URL:-}"
   APP_PATH="/user/${HUB_USER}/${APP_NAME}/"

   pi-browser-smoke \
     --base-url "$INTERNAL_BASE_URL" \
     --app-path "$APP_PATH" \
     --username "$HUB_USER" \
     --hub-api-url "$API_URL" \
     --hub-api-token "$API_TOKEN" \
     --timeout-seconds 120 \
     --screenshot "/tmp/${APP_NAME}-smoke-internal.png"
   ```

   Optional external follow-up:

   ```bash
   if [ -n "$EXTERNAL_BASE_URL" ] && [ "$EXTERNAL_BASE_URL" != "$INTERNAL_BASE_URL" ]; then
     pi-browser-smoke \
       --base-url "$EXTERNAL_BASE_URL" \
       --app-path "$APP_PATH" \
       --username "$HUB_USER" \
       --hub-api-url "$API_URL" \
       --hub-api-token "$API_TOKEN" \
       --timeout-seconds 120 \
       --screenshot "/tmp/${APP_NAME}-smoke-external.png"
   fi
   ```

   Notes:
   - Token-bootstrap mode is the default and preferred path.
   - Username/password is only fallback for environments where token-bootstrap is unavailable.

   Browser smoke pass criteria (minimum):
   - final URL contains `/user/<username>/<app-name>/` path,
   - final URL does **not** include `/hub/spawn-pending/` or `/_temp/jhub-app-proxy/`,
   - page HTTP status is success/redirect (`2xx`/`3xx`),
   - at least one visible DOM node exists in `document.body`.

   External auth-path classification rule:
   - If internal smoke passes but external smoke lands on IdP/login (Keycloak/OIDC/hub login),
     classify as `external_auth_path_issue` (not app-runtime failure).
   - Report deployment as runtime-healthy with external-auth remediation required.

   Diagnostic hint:
   - If backend/server readiness is true but browser shows blank/near-empty DOM,
     suspect SPA asset base path or root-relative API fetches (subpath issue), not backend health.

4. **No-browser fallback policy**
   - Internal browser smoke is mandatory for completion.
   - If `pi-browser-smoke` is unavailable or cannot run, return `verification_incomplete` and do **not** claim deployment success.

### 5.2) Operational restart sanity check (RECOMMENDED for rebuild/restart flows)

After restarts/redeploys, verify you are testing the new process (not stale binaries):
- confirm pod/container restart timestamp changed after action,
- confirm expected executable identity/version (`ps`, `readlink`, `--version`),
- confirm expected port is listening in the target process,
- confirm app route responds from the new runtime before browser smoke assertions.

If identity/port checks fail, treat verification as incomplete and re-run deployment/startup.

---

## 6) Stop and delete

Stop (keep definition):

```bash
curl -sS -X DELETE \
  -H "Authorization: token $API_TOKEN" \
  "$API_URL/users/$HUB_USER/servers/$APP_NAME?remove=false"
```

Delete (remove server entry):

```bash
curl -sS -X DELETE \
  -H "Authorization: token $API_TOKEN" \
  "$API_URL/users/$HUB_USER/servers/$APP_NAME?remove=true"
```

---

## 7) Build guidance for arbitrary stacks

- Ensure runtime binds to provided port.
- Keep startup command deterministic and restart-safe.
- Prefer the Python-base64 bootstrap command pattern for multi-step startup.
- If command depends on local build artifacts, ensure artifacts are created in the same target server context.

### 7.1) Frontend subpath preflight (MUST run for SPAs)

Before claiming success for SPA-style apps, check subpath compatibility for Hub-routed paths (`/user/<u>/<app>/...`).

Checklist:
- avoid root-relative assets (`/assets/...`), prefer relative (`./assets/...`)
- avoid root-relative API calls (`/api/...`), prefer relative (`./api/...`)
- configure router basename/base path if framework requires it
- for Vite static builds, prefer `base: './'` unless app requires a different explicit subpath

### 7.2) Artifact delivery guidance

Do not depend on temporary pod-local HTTP file servers for cross-pod artifact delivery.

Note:
- In many environments, egress/network policy blocks ad-hoc or public temporary upload endpoints.
- If artifact fetch fails due reachability/policy, switch to env-embedded payloads (small/medium) or image mode.

Preferred order:
1. small bundles: embed via env/file bootstrap
2. medium bundles: stage as durable files/object URLs
3. large bundles: image mode or durable artifact storage

Examples of arbitrary commands (adjust as needed):
- Node: `npm run start -- --host 0.0.0.0 --port {port}`
- Next.js standalone: `node server.js --port {port}`
- Go binary: `./bin/server --port {port}`
- Java: `java -jar app.jar --server.port={port}`

---

## 8) Failure triage

1. Check Hub API response body from spawn call.
2. Inspect user server object (`pending`, `ready`, `stopped`, `state`).
3. If available, inspect hub logs for spawn exceptions.
4. If available, inspect Kubernetes events/pod status:

```bash
kubectl get events -A --sort-by=.lastTimestamp | tail -n 150
```

Common causes:
- invalid spawn payload keys
- command not executable / wrong cwd
- brittle shell quoting in `app.command` (prefer Python-base64 wrapper)
- argument list too long from oversized command payloads
- resource request/limit mismatch
- image pull issues (image mode)
- PVC/permissions problems
- non-shared storage assumptions across named servers
- SPA subpath misconfiguration (root-relative assets/API paths)
- auth/cookie flow issues causing browser redirect loops or blank home/app page
- missing runtime dependencies in target image

### 8.1) Failure-signature quick hints

- `ImagePullBackOff` / `ErrImagePull`:
  likely bad/unresolvable image; retry with known-good resolved image.
- `ready=true` but browser smoke blank/near-empty UI:
  likely SPA subpath/base path issue.
- `argument list too long`:
  payload too large in `app.command`; move payload to env/file/artifact.
- `fork/exec ... no such file or directory`:
  target server missing artifacts/binary; storage is isolated or bootstrap missing.
- internal smoke passes but external smoke ends on IdP/hub login:
  external auth-path issue (gateway/SSO/session bootstrap), not app runtime failure.

---

## 9) Guardrails

- Default to self-user operations only.
- Do not attempt cross-user actions unless explicitly requested and authorized.
- Do not claim generic TCP/UDP exposure; this pattern is for Hub-routed HTTP/WebSocket apps.

---

## 10) Output requirements

Always report:
1. selected deployment mode (source-on-home / bootstrap-on-target / image) and why
2. storage topology decision (shared/unknown/non-shared) with evidence
3. image resolution decision (especially when `profile_image` was empty)
4. build location/artifacts used
5. exact spawn payload (sanitized)
6. exact Hub API calls made
7. resulting app URL
8. current server state + next action
9. verification evidence:
   - Hub ready/pending state snapshot,
   - internal browser smoke result (URL/status/title),
   - external browser smoke result when configured (URL/status/title),
   - classification (`passed` / `external_auth_path_issue` / `failed`),
   - screenshot file/path (if captured by browser tooling)
