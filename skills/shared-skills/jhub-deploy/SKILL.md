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

---

## 2) Deployment modes (default = no image push)

### A) **Source-on-home mode** (default)
- Build/install app on user home/workspace (`/home/jovyan/...`)
- Spawn named server to run command from that path
- **No image push required**

### B) Image mode (optional)
- Use custom image when runtime deps cannot be satisfied in source-on-home mode
- Can use local cluster image import for local k3d/k3s
- Registry push is optional, only needed when cluster cannot access local image

Do not prefer framework-native templates. Treat all apps as arbitrary command-based runtimes.

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
3. For multi-step setup (write files/build/start), prefer a Python bootstrap wrapper:

```bash
python3 -c "exec(__import__('base64').b64decode('<BASE64_PY>').decode())" {port}
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
   - Provide a routable base URL for the current environment.
     - local in-cluster default: `http://proxy-public.data-science.svc.cluster.local`
     - production: your real external domain base URL
   - Run smoke verification against canonical app path.

   Example:

   ```bash
   BASE_URL="${NEBARI_BROWSER_BASE_URL:-http://proxy-public.data-science.svc.cluster.local}"
   APP_PATH="/user/${HUB_USER}/${APP_NAME}/"

   pi-browser-smoke \
     --base-url "$BASE_URL" \
     --app-path "$APP_PATH" \
     --username "$HUB_USER" \
     --hub-api-url "$API_URL" \
     --hub-api-token "$API_TOKEN" \
     --timeout-seconds 120 \
     --screenshot "/tmp/${APP_NAME}-smoke.png"
   ```

   Notes:
   - Token-bootstrap mode is the default and preferred path.
   - Username/password is only fallback for environments where token-bootstrap is unavailable.

   Browser smoke pass criteria (minimum):
   - final URL contains `/user/<username>/<app-name>/` path,
   - final URL does **not** include `/hub/spawn-pending/` or `/_temp/jhub-app-proxy/`,
   - page HTTP status is success/redirect (`2xx`/`3xx`),
   - at least one visible DOM node exists in `document.body`.

4. **No-browser fallback policy**
   - Browser smoke is mandatory for completion.
   - If `pi-browser-smoke` is unavailable or cannot run, return `verification_incomplete` and do **not** claim deployment success.

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

- Build app in user workspace first.
- Ensure runtime binds to provided port.
- Keep startup command deterministic and restart-safe.
- Prefer the Python-base64 bootstrap command pattern for multi-step startup.
- If command depends on local build artifacts, create them before spawn.

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
- resource request/limit mismatch
- image pull issues (image mode)
- PVC/permissions problems
- auth/cookie flow issues causing browser redirect loops or blank home/app page

---

## 9) Guardrails

- Default to self-user operations only.
- Do not attempt cross-user actions unless explicitly requested and authorized.
- Do not claim generic TCP/UDP exposure; this pattern is for Hub-routed HTTP/WebSocket apps.

---

## 10) Output requirements

Always report:
1. build location/artifacts used
2. exact spawn payload (sanitized)
3. exact Hub API calls made
4. resulting app URL
5. current server state + next action
6. verification evidence:
   - Hub ready/pending state snapshot,
   - browser smoke final URL,
   - browser smoke page title,
   - screenshot file/path (if captured by browser tooling)
