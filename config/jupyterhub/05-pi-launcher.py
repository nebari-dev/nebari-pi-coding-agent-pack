# Pi Launcher: a tiny JupyterHub-managed service that spawns/opens the user's
# "pi" named server with a selected resource profile.
import os
import textwrap
from pathlib import Path

pi_launcher_py = Path("/srv/jupyterhub/pi_launcher.py")
pi_launcher_py.write_text(
    textwrap.dedent(
        r"""
        import base64
        import datetime
        import html
        import json
        import os
        import re
        import time
        import uuid
        from urllib.parse import quote, urlencode

        import requests
        from kubernetes import client as k8s_client, config as k8s_config
        from kubernetes.stream import stream as k8s_stream
        from kubernetes.stream.ws_client import ERROR_CHANNEL
        from tornado.ioloop import IOLoop
        from tornado.web import Application, RequestHandler, authenticated
        from jupyterhub.services.auth import HubOAuthenticated, HubOAuthCallbackHandler

        API_URL = os.environ["JUPYTERHUB_API_URL"].rstrip("/")
        API_TOKEN = os.environ["JUPYTERHUB_API_TOKEN"]

        SERVICE_PREFIX = os.environ.get("JUPYTERHUB_SERVICE_PREFIX", "/").rstrip("/")
        if not SERVICE_PREFIX.startswith("/"):
            SERVICE_PREFIX = "/" + SERVICE_PREFIX

        SERVER_NAME = "pi"
        NAMESPACE = os.environ.get("PI_SHARING_NAMESPACE", os.environ.get("POD_NAMESPACE", "default")).strip() or "default"
        SESSION_SHARE_SECRET_PREFIX = "pi-share-session-"
        LIVE_SHARE_SECRET_PREFIX = "pi-share-live-"
        PI_SHARE_LABEL_KEY = "pi-sharing.nebari.dev/type"
        PI_SHARE_OWNER_LABEL_KEY = "pi-sharing.nebari.dev/owner"
        K8S_API_URL = os.environ.get("PI_K8S_API_URL", "https://kubernetes.default.svc").strip()
        LIVE_SHARE_ENABLED = os.environ.get("PI_LIVE_SHARE_ENABLED", "0").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        SESSION_SHARE_MAX_BYTES = max(
            1024,
            int(os.environ.get("PI_SHARE_SESSION_MAX_BYTES", "1048576")),
        )
        PI_AGENT_DIR = (os.environ.get("PI_CODING_AGENT_DIR", "/tmp/pi-agent") or "/tmp/pi-agent").strip()

        PROFILE_BY_SIZE = {
            "small": "pi-small",
            "medium": "pi-medium",
            "large": "pi-large",
        }
        PROFILE_UI_SPECS = {
            "small": os.environ.get("PI_PROFILE_SMALL_SPEC", "2 cpu / 8G RAM"),
            "medium": os.environ.get("PI_PROFILE_MEDIUM_SPEC", "4 cpu / 16G RAM"),
            "large": os.environ.get("PI_PROFILE_LARGE_SPEC", "8 cpu / 32G RAM"),
        }

        def hub_request(method: str, path: str, **kwargs):
            headers = kwargs.pop("headers", {})
            headers["Authorization"] = f"token {API_TOKEN}"
            url = f"{API_URL}{path}"
            return requests.request(method, url, headers=headers, timeout=60, **kwargs)

        def get_user(username: str):
            resp = hub_request(
                "GET",
                f"/users/{quote(username)}",
                params={"include_stopped_servers": "true"},
            )
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"Hub API user lookup failed: status={resp.status_code} body={resp.text}"
                )
            data = resp.json()
            if not isinstance(data, dict):
                raise RuntimeError(f"Unexpected Hub user payload: {data!r}")
            return data

        def get_server(username: str):
            user = get_user(username)
            servers = user.get("servers") or {}
            if isinstance(servers, dict):
                return servers.get(SERVER_NAME)
            return None

        def get_server_profile(server) -> str:
            if not isinstance(server, dict):
                return ""
            opts = server.get("user_options") or {}
            if not isinstance(opts, dict):
                return ""
            profile = opts.get("profile")
            return profile.strip() if isinstance(profile, str) else ""

        def is_legacy_server(server) -> bool:
            if not isinstance(server, dict):
                return False
            opts = server.get("user_options") or {}
            if not isinstance(opts, dict):
                return False
            if opts.get("jhub_app"):
                return True
            # Legacy jhub-app payload fields; Pi should now be profile-only.
            legacy_fields = ("framework", "custom_command", "filepath", "conda_env")
            return any(field in opts for field in legacy_fields)

        def get_pending_state(server) -> str:
            if not isinstance(server, dict):
                return ""
            pending = server.get("pending")
            if pending is None:
                return ""
            if isinstance(pending, str):
                return pending.strip()
            return str(pending)

        def build_status_payload(username: str, *, server=None, error: str = ""):
            payload = {
                "username": username,
                "exists": False,
                "ready": False,
                "stopped": True,
                "pending": "",
                "profile": "",
                "legacy": False,
            }
            if error:
                payload["error"] = str(error)
                return payload
            if not isinstance(server, dict):
                return payload
            payload["exists"] = True
            payload["ready"] = bool(server.get("ready"))
            payload["stopped"] = bool(server.get("stopped"))
            payload["pending"] = get_pending_state(server)
            payload["profile"] = get_server_profile(server)
            payload["legacy"] = is_legacy_server(server)
            return payload

        def redirect_with_message(handler, message: str):
            handler.redirect(f"{SERVICE_PREFIX}/?{urlencode({'msg': message})}")

        def json_response(handler, payload, status=200):
            handler.set_status(status)
            handler.set_header("Content-Type", "application/json; charset=utf-8")
            handler.set_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            handler.set_header("Pragma", "no-cache")
            handler.write(json.dumps(payload))

        def _slug_for_label(value: str) -> str:
            return re.sub(r"[^a-z0-9_.-]+", "-", (value or "").strip().lower()).strip("-") or "unknown"

        def _encode_session_dir(cwd: str) -> str:
            normalized = (cwd or "").strip()
            if normalized.startswith("/"):
                normalized = normalized[1:]
            return "--" + normalized.replace("/", "-") + "--"

        def _utcnow() -> datetime.datetime:
            return datetime.datetime.now(datetime.timezone.utc)

        def _isoformat(dt: datetime.datetime) -> str:
            return dt.astimezone(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

        def _parse_iso(value: str):
            if not value or not isinstance(value, str):
                return None
            v = value.strip()
            if not v:
                return None
            if v.endswith("Z"):
                v = v[:-1] + "+00:00"
            try:
                return datetime.datetime.fromisoformat(v).astimezone(datetime.timezone.utc)
            except Exception:
                return None

        def _parse_targets(raw):
            if raw is None:
                return []
            if isinstance(raw, list):
                items = raw
            elif isinstance(raw, str):
                text = raw.strip()
                if not text:
                    return []
                try:
                    parsed = json.loads(text)
                except Exception:
                    parsed = None
                if isinstance(parsed, list):
                    items = parsed
                else:
                    items = [chunk.strip() for chunk in text.split(",")]
            else:
                return []
            targets = []
            for item in items:
                if not isinstance(item, str):
                    continue
                cleaned = item.strip()
                if cleaned:
                    targets.append(cleaned)
            deduped = []
            seen = set()
            for item in targets:
                key = item.lower()
                if key in seen:
                    continue
                seen.add(key)
                deduped.append(item)
            return deduped

        def _normalize_share_id(raw: str):
            value = (raw or "").strip().lower()
            if not re.fullmatch(r"[a-z0-9-]{6,64}", value):
                return ""
            return value

        _core_v1_api = None

        def core_v1_api():
            global _core_v1_api
            if _core_v1_api is None:
                try:
                    k8s_config.load_incluster_config()
                except Exception:
                    token_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
                    ca_path = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
                    with open(token_path, "r", encoding="utf-8") as f:
                        token = f.read().strip()
                    cfg = k8s_client.Configuration()
                    cfg.host = K8S_API_URL
                    cfg.verify_ssl = True
                    cfg.ssl_ca_cert = ca_path
                    cfg.api_key = {"authorization": f"Bearer {token}"}
                    k8s_client.Configuration.set_default(cfg)
                _core_v1_api = k8s_client.CoreV1Api()
            return _core_v1_api

        def get_pi_pod_name(username: str) -> str:
            selector = ",".join(
                [
                    "component=singleuser-server",
                    f"hub.jupyter.org/username={username}",
                    f"hub.jupyter.org/servername={SERVER_NAME}",
                ]
            )
            pods = core_v1_api().list_namespaced_pod(
                namespace=NAMESPACE,
                label_selector=selector,
            ).items
            if not pods:
                raise RuntimeError(
                    f"No pod found for user={username!r} server={SERVER_NAME!r}. Start Pi first."
                )
            running = [pod for pod in pods if (pod.status and pod.status.phase == "Running")]
            target = running[0] if running else pods[0]
            name = target.metadata.name if target.metadata else ""
            if not name:
                raise RuntimeError(f"Unable to resolve pod name for user={username!r}.")
            return name

        def exec_in_pi_pod(
            username: str,
            command,
            *,
            stdin_data: str = "",
            timeout_seconds: int = 120,
        ) -> str:
            pod_name = get_pi_pod_name(username)
            ws = k8s_stream(
                core_v1_api().connect_get_namespaced_pod_exec,
                pod_name,
                NAMESPACE,
                command=command,
                stderr=True,
                stdin=bool(stdin_data),
                stdout=True,
                tty=False,
                _preload_content=False,
            )
            stdout_chunks = []
            stderr_chunks = []
            start = time.time()
            if stdin_data:
                ws.write_stdin(stdin_data)
                close_stdin = getattr(ws, "close_stdin", None)
                if callable(close_stdin):
                    close_stdin()
            while ws.is_open():
                ws.update(timeout=1)
                if ws.peek_stdout():
                    stdout_chunks.append(ws.read_stdout())
                if ws.peek_stderr():
                    stderr_chunks.append(ws.read_stderr())
                if time.time() - start > timeout_seconds:
                    ws.close()
                    raise RuntimeError(
                        f"Timed out waiting for pod exec in {pod_name} (>{timeout_seconds}s)."
                    )
            status_raw = ws.read_channel(ERROR_CHANNEL)
            exit_code = "0"
            if status_raw:
                try:
                    status_json = json.loads(status_raw)
                    causes = (
                        status_json.get("details", {}).get("causes", [])
                        if isinstance(status_json, dict)
                        else []
                    )
                    for cause in causes:
                        if isinstance(cause, dict) and cause.get("reason") == "ExitCode":
                            exit_code = str(cause.get("message", "0"))
                            break
                except Exception:
                    pass
            if exit_code not in ("0", ""):
                stderr = "".join(stderr_chunks).strip()
                raise RuntimeError(
                    f"Pod exec failed for {username}/{SERVER_NAME} exit={exit_code}: {stderr or 'unknown error'}"
                )
            return "".join(stdout_chunks)

        def read_session_file_from_pi(username: str, session_path: str) -> bytes:
            output = exec_in_pi_pod(
                username,
                [
                    "python",
                    "-c",
                    (
                        "import base64, pathlib, sys;"
                        "p=pathlib.Path(sys.argv[1]).expanduser();"
                        "data=p.read_bytes();"
                        "sys.stdout.write(base64.b64encode(data).decode('ascii'))"
                    ),
                    session_path,
                ],
                timeout_seconds=90,
            ).strip()
            if not output:
                raise RuntimeError(f"Session file appears empty or unreadable: {session_path}")
            return base64.b64decode(output)

        def write_session_file_to_pi(username: str, target_path: str, content: bytes) -> str:
            encoded = base64.b64encode(content).decode("ascii")
            output = exec_in_pi_pod(
                username,
                [
                    "python",
                    "-c",
                    (
                        "import base64, pathlib, sys;"
                        "p=pathlib.Path(sys.argv[1]).expanduser();"
                        "p.parent.mkdir(parents=True, exist_ok=True);"
                        "raw=base64.b64decode(sys.argv[2]);"
                        "p.write_bytes(raw);"
                        "sys.stdout.write(str(p))"
                    ),
                    target_path,
                    encoded,
                ],
                timeout_seconds=90,
            )
            written = output.strip()
            if not written:
                raise RuntimeError("Unable to determine destination file path in Pi pod.")
            return written

        def _secret_name_for_share(prefix: str) -> str:
            return f"{prefix}{uuid.uuid4().hex[:12]}"

        def _parse_secret_data(secret_obj, key: str):
            data = (secret_obj.data or {}).get(key)
            if not data:
                return ""
            return base64.b64decode(data).decode("utf-8")

        def _list_share_secrets(share_type: str, owner: str = ""):
            selectors = [f"{PI_SHARE_LABEL_KEY}={share_type}"]
            if owner:
                selectors.append(f"{PI_SHARE_OWNER_LABEL_KEY}={_slug_for_label(owner)}")
            selector = ",".join(selectors)
            return core_v1_api().list_namespaced_secret(
                namespace=NAMESPACE,
                label_selector=selector,
            ).items

        def _load_share_metadata(secret_obj):
            raw = _parse_secret_data(secret_obj, "metadata.json")
            if not raw:
                return {}
            try:
                return json.loads(raw)
            except Exception:
                return {}

        def _secret_for_session_share_id(share_id: str):
            sid = _normalize_share_id(share_id)
            if not sid:
                raise RuntimeError("Invalid share_id.")
            secret_name = f"{SESSION_SHARE_SECRET_PREFIX}{sid}"
            return core_v1_api().read_namespaced_secret(name=secret_name, namespace=NAMESPACE)

        def get_user_group_names(username: str):
            user = get_user(username)
            groups = user.get("groups") or []
            names = []
            for entry in groups:
                if isinstance(entry, dict):
                    name = entry.get("name")
                elif isinstance(entry, str):
                    name = entry
                else:
                    name = ""
                if isinstance(name, str) and name.strip():
                    names.append(name.strip())
            return sorted(set(names))

        def is_user_admin(username: str) -> bool:
            user = get_user(username)
            return bool(user.get("admin"))

        def _share_visible_to_user(metadata, viewer: str, viewer_groups):
            owner = str(metadata.get("owner") or "").strip()
            if viewer == owner:
                return True
            allowed_users = _parse_targets(metadata.get("share_with_users"))
            if viewer in allowed_users:
                return True
            allowed_groups = set(_parse_targets(metadata.get("share_with_groups")))
            return bool(allowed_groups.intersection(set(viewer_groups)))

        def _share_expired(metadata):
            expires_at = _parse_iso(metadata.get("expires_at", ""))
            return bool(expires_at and _utcnow() >= expires_at)

        def _create_session_share(
            *,
            owner: str,
            session_path: str,
            title: str,
            share_with_users,
            share_with_groups,
            expires_hours: int,
        ):
            users = _parse_targets(share_with_users)
            groups = _parse_targets(share_with_groups)
            if not users and not groups:
                raise RuntimeError("Provide at least one user or group to share with.")
            if expires_hours <= 0 or expires_hours > 168:
                raise RuntimeError("expires_hours must be between 1 and 168.")
            content = read_session_file_from_pi(owner, session_path)
            if len(content) > SESSION_SHARE_MAX_BYTES:
                raise RuntimeError(
                    f"Session is too large ({len(content)} bytes). Max allowed is {SESSION_SHARE_MAX_BYTES} bytes."
                )
            share_id = uuid.uuid4().hex[:12]
            created_at = _utcnow()
            expires_at = created_at + datetime.timedelta(hours=expires_hours)
            metadata = {
                "id": share_id,
                "type": "session",
                "mode": "fork-only",
                "owner": owner,
                "title": (title or "").strip() or f"Pi session share {share_id}",
                "session_path": session_path,
                "created_at": _isoformat(created_at),
                "expires_at": _isoformat(expires_at),
                "share_with_users": users,
                "share_with_groups": groups,
            }
            secret = k8s_client.V1Secret(
                metadata=k8s_client.V1ObjectMeta(
                    name=f"{SESSION_SHARE_SECRET_PREFIX}{share_id}",
                    labels={
                        PI_SHARE_LABEL_KEY: "session",
                        PI_SHARE_OWNER_LABEL_KEY: _slug_for_label(owner),
                    },
                ),
                type="Opaque",
                string_data={
                    "metadata.json": json.dumps(metadata),
                    "session.jsonl": content.decode("utf-8", errors="replace"),
                },
            )
            core_v1_api().create_namespaced_secret(namespace=NAMESPACE, body=secret)
            return metadata

        def _reconcile_live_shares(owner: str):
            # We own all /shares state for owner/pi; rebuild from non-expired live-share records.
            now = _utcnow()
            live_records = []
            for secret_obj in _list_share_secrets("live", owner=owner):
                metadata = _load_share_metadata(secret_obj)
                expires_at = _parse_iso(metadata.get("expires_at", ""))
                if expires_at and now >= expires_at:
                    core_v1_api().delete_namespaced_secret(secret_obj.metadata.name, NAMESPACE)
                    continue
                if metadata:
                    live_records.append(metadata)

            revoke = hub_request("DELETE", f"/shares/{quote(owner)}/{SERVER_NAME}")
            if revoke.status_code not in (200, 202, 204, 404):
                raise RuntimeError(
                    f"Failed to reset live shares for {owner}/{SERVER_NAME}: "
                    f"status={revoke.status_code} body={revoke.text}"
                )

            share_errors = []
            for record in live_records:
                for target_user in _parse_targets(record.get("share_with_users")):
                    r = hub_request(
                        "POST",
                        f"/shares/{quote(owner)}/{SERVER_NAME}",
                        json={"user": target_user},
                    )
                    if r.status_code not in (200, 201, 202, 204):
                        share_errors.append(
                            f"user={target_user} status={r.status_code} body={r.text}"
                        )
                for target_group in _parse_targets(record.get("share_with_groups")):
                    r = hub_request(
                        "POST",
                        f"/shares/{quote(owner)}/{SERVER_NAME}",
                        json={"group": target_group},
                    )
                    if r.status_code not in (200, 201, 202, 204):
                        share_errors.append(
                            f"group={target_group} status={r.status_code} body={r.text}"
                        )
            return {
                "owner": owner,
                "records": len(live_records),
                "errors": share_errors,
            }

        def _create_live_share(
            *,
            owner: str,
            share_with_users,
            share_with_groups,
            expires_hours: int,
            created_by: str,
        ):
            users = _parse_targets(share_with_users)
            groups = _parse_targets(share_with_groups)
            if not users and not groups:
                raise RuntimeError("Provide at least one user or group to share with.")
            if expires_hours <= 0 or expires_hours > 48:
                raise RuntimeError("expires_hours must be between 1 and 48.")
            share_id = uuid.uuid4().hex[:12]
            created_at = _utcnow()
            metadata = {
                "id": share_id,
                "type": "live",
                "owner": owner,
                "created_by": created_by,
                "created_at": _isoformat(created_at),
                "expires_at": _isoformat(created_at + datetime.timedelta(hours=expires_hours)),
                "share_with_users": users,
                "share_with_groups": groups,
            }
            secret = k8s_client.V1Secret(
                metadata=k8s_client.V1ObjectMeta(
                    name=f"{LIVE_SHARE_SECRET_PREFIX}{share_id}",
                    labels={
                        PI_SHARE_LABEL_KEY: "live",
                        PI_SHARE_OWNER_LABEL_KEY: _slug_for_label(owner),
                    },
                ),
                type="Opaque",
                string_data={"metadata.json": json.dumps(metadata)},
            )
            core_v1_api().create_namespaced_secret(namespace=NAMESPACE, body=secret)
            reconcile = _reconcile_live_shares(owner)
            return metadata, reconcile

        def _delete_live_share(owner: str, share_id: str = ""):
            if share_id:
                sid = _normalize_share_id(share_id)
                if not sid:
                    raise RuntimeError("Invalid share_id.")
                core_v1_api().delete_namespaced_secret(
                    name=f"{LIVE_SHARE_SECRET_PREFIX}{sid}",
                    namespace=NAMESPACE,
                )
            else:
                for secret_obj in _list_share_secrets("live", owner=owner):
                    core_v1_api().delete_namespaced_secret(secret_obj.metadata.name, NAMESPACE)
            return _reconcile_live_shares(owner)

        class StatusHandler(HubOAuthenticated, RequestHandler):
            @authenticated
            def get(self):
                user = self.current_user
                username = user["name"]

                try:
                    server = get_server(username)
                    payload = build_status_payload(username, server=server)
                except Exception as exc:
                    payload = build_status_payload(username, error=str(exc))

                self.set_header("Content-Type", "application/json; charset=utf-8")
                self.set_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
                self.set_header("Pragma", "no-cache")
                self.write(json.dumps(payload))

        class RootHandler(HubOAuthenticated, RequestHandler):
            @authenticated
            def get(self):
                user = self.current_user
                username = user["name"]
                spawn_pending_url = f"/hub/spawn-pending/{quote(username)}/{SERVER_NAME}"
                smart_open_url = f"{SERVICE_PREFIX}/open"
                xsrf_token = self.xsrf_token.decode("utf-8")
                status_class = "status-idle"
                status_text = "Pi server is not running."
                detail_items = []
                pending_state = ""
                is_pending = False

                try:
                    server = get_server(username)
                except Exception as exc:
                    server = None
                    status_class = "status-error"
                    status_text = "Unable to fetch Pi server status."
                    detail_items.append(
                        f"Could not read current server state: {html.escape(str(exc))}"
                    )

                if server is not None:
                    pending = get_pending_state(server)
                    pending_state = pending
                    is_pending = bool(pending)
                    ready = bool(server.get("ready"))
                    stopped = bool(server.get("stopped"))
                    profile = get_server_profile(server)
                    legacy = is_legacy_server(server)

                    if pending:
                        status_class = "status-pending"
                        status_text = f"Pi server pending: {html.escape(pending)}"
                    elif ready:
                        status_class = "status-running"
                        status_text = "Pi server is running."
                    elif stopped:
                        status_class = "status-stopped"
                        status_text = "Pi server exists but is stopped."
                    else:
                        status_class = "status-unknown"
                        status_text = "Pi server exists."

                    if profile:
                        detail_items.append(
                            f"Current profile: <code>{html.escape(profile)}</code>"
                        )
                    if legacy:
                        detail_items.append(
                            "Legacy Pi server options detected. Launching a size will recreate it in terminal mode."
                        )

                notice = (self.get_argument("msg", "") or "").strip()
                if notice:
                    detail_items.insert(0, html.escape(notice))
                details_html = "".join(f"<li>{item}</li>" for item in detail_items)

                self.set_header("Content-Type", "text/html; charset=utf-8")
                self.set_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
                self.set_header("Pragma", "no-cache")
                self.write(
                    f'''<!doctype html>
        <html lang="en">
          <head>
            <meta charset="utf-8" />
            <meta name="viewport" content="width=device-width, initial-scale=1" />
            <title>Pi Launcher</title>
            <style>
              :root {{
                --bg: #0b1020;
                --panel: rgba(255, 255, 255, 0.06);
                --panel2: rgba(255, 255, 255, 0.10);
                --text: rgba(255, 255, 255, 0.92);
                --muted: rgba(255, 255, 255, 0.68);
                --border: rgba(255, 255, 255, 0.14);
              }}
              body {{
                margin: 0;
                font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
                color: var(--text);
                background:
                  radial-gradient(900px 600px at 20% 10%, rgba(71, 98, 255, 0.25), transparent 60%),
                  radial-gradient(900px 600px at 80% 30%, rgba(255, 112, 55, 0.22), transparent 60%),
                  var(--bg);
              }}
              .wrap {{
                max-width: 880px;
                margin: 0 auto;
                padding: 36px 18px 60px;
              }}
              .card {{
                background: var(--panel);
                border: 1px solid var(--border);
                border-radius: 16px;
                padding: 22px 22px 18px;
                box-shadow: 0 12px 30px rgba(0,0,0,0.35);
              }}
              h1 {{
                margin: 0 0 6px;
                font-size: 22px;
                letter-spacing: 0.2px;
              }}
              p {{
                margin: 0 0 16px;
                color: var(--muted);
                line-height: 1.35;
              }}
              .status {{
                border: 1px solid var(--border);
                border-radius: 14px;
                padding: 12px 14px;
                background: rgba(255,255,255,0.04);
                margin-bottom: 12px;
              }}
              .status-title {{
                font-size: 12px;
                letter-spacing: 0.6px;
                text-transform: uppercase;
                opacity: 0.8;
                margin-bottom: 6px;
              }}
              .status-text {{
                font-weight: 650;
                margin-bottom: 4px;
              }}
              .status ul {{
                margin: 6px 0 0 18px;
                padding: 0;
                color: var(--muted);
                font-size: 14px;
              }}
              .status-running {{
                border-color: rgba(40,200,120,0.55);
                background: rgba(40,200,120,0.10);
              }}
              .status-pending {{
                border-color: rgba(255,180,70,0.55);
                background: rgba(255,180,70,0.10);
              }}
              .status-error {{
                border-color: rgba(255,90,90,0.55);
                background: rgba(255,90,90,0.10);
              }}
              .sizes {{
                display: grid;
                grid-template-columns: repeat(3, minmax(0, 1fr));
                gap: 12px;
                margin-top: 14px;
              }}
              .btn {{
                display: block;
                width: 100%;
                padding: 14px 12px;
                border-radius: 14px;
                border: 1px solid var(--border);
                background: linear-gradient(180deg, var(--panel2), rgba(255,255,255,0.06));
                color: var(--text);
                font-weight: 650;
                font-size: 15px;
                cursor: pointer;
                text-align: left;
              }}
              .btn small {{
                display: block;
                font-weight: 500;
                opacity: 0.8;
                margin-top: 2px;
              }}
              .row {{
                display: flex;
                gap: 12px;
                margin-top: 16px;
                flex-wrap: wrap;
              }}
              .link {{
                display: inline-block;
                padding: 10px 12px;
                border-radius: 12px;
                border: 1px solid var(--border);
                color: var(--text);
                text-decoration: none;
                background: rgba(255,255,255,0.04);
                cursor: pointer;
                font: inherit;
              }}
              .link:hover {{
                background: rgba(255,255,255,0.08);
              }}
              button[disabled] {{
                opacity: 0.65;
                cursor: progress;
              }}
              @media (max-width: 720px) {{
                .sizes {{
                  grid-template-columns: 1fr;
                }}
                .btn {{
                  text-align: center;
                }}
              }}
            </style>
          </head>
          <body>
            <div class="wrap">
              <div class="card">
                <h1>Pi Launcher</h1>
                <p>Choose a size to start or reconfigure your <code>{SERVER_NAME}</code> named server.</p>
                <div id="action-status" class="status status-pending" style="display:none;">
                  <div class="status-text">Request submitted. Updating Pi status...</div>
                </div>
                <div class="status {status_class}">
                  <div class="status-title">Current Status</div>
                  <div class="status-text">{status_text}</div>
                  {'<ul>' + details_html + '</ul>' if detail_items else ''}
                </div>
                <div class="sizes">
                  <form method="post" action="{SERVICE_PREFIX}/launch">
                    <input type="hidden" name="_xsrf" value="{xsrf_token}" />
                    <input type="hidden" name="size" value="small" />
                    <button class="btn" type="submit">Small<small>{html.escape(str(PROFILE_UI_SPECS.get('small', '')))}</small></button>
                  </form>
                  <form method="post" action="{SERVICE_PREFIX}/launch">
                    <input type="hidden" name="_xsrf" value="{xsrf_token}" />
                    <input type="hidden" name="size" value="medium" />
                    <button class="btn" type="submit">Medium<small>{html.escape(str(PROFILE_UI_SPECS.get('medium', '')))}</small></button>
                  </form>
                  <form method="post" action="{SERVICE_PREFIX}/launch">
                    <input type="hidden" name="_xsrf" value="{xsrf_token}" />
                    <input type="hidden" name="size" value="large" />
                    <button class="btn" type="submit">Large<small>{html.escape(str(PROFILE_UI_SPECS.get('large', '')))}</small></button>
                  </form>
                </div>
                <div class="row">
                  <a class="link" href="{spawn_pending_url}">Spawn status</a>
                  <a class="link" href="{smart_open_url}">Open Pi (if running)</a>
                  <a class="link" href="{SERVICE_PREFIX}/share">Share Pi Sessions</a>
                  <form method="post" action="{SERVICE_PREFIX}/stop">
                    <input type="hidden" name="_xsrf" value="{xsrf_token}" />
                    <button class="link" type="submit">Stop Pi</button>
                  </form>
                  <a class="link" href="/hub/home">Back to Home</a>
                </div>
              </div>
            </div>
            <script>
              (function setupActionUx() {{
                var forms = document.querySelectorAll(
                  'form[action$="/launch"], form[action$="/stop"]'
                );
                if (!forms.length) {{
                  return;
                }}
                function currentXsrf() {{
                  var match = document.cookie.match(/(?:^|; )_xsrf=([^;]+)/);
                  return match ? decodeURIComponent(match[1]) : "";
                }}
                forms.forEach(function (form) {{
                  form.addEventListener("submit", function () {{
                    var token = currentXsrf();
                    if (token) {{
                      form.querySelectorAll('input[name="_xsrf"]').forEach(function (input) {{
                        input.value = token;
                      }});
                    }}
                    var actionStatus = document.getElementById("action-status");
                    if (actionStatus) {{
                      actionStatus.style.display = "block";
                    }}
                    var buttons = document.querySelectorAll("button");
                    buttons.forEach(function (btn) {{
                      btn.disabled = true;
                    }});
                  }});
                }});
              }})();

              (function maybeAutoRefresh() {{
                var isPending = {"true" if is_pending else "false"};
                var pendingState = "{html.escape(pending_state)}";
                if (isPending) {{
                  setTimeout(function () {{
                    window.location.reload();
                  }}, 3000);
                }}
              }})();
            </script>
          </body>
        </html>'''
                )

        class OpenHandler(HubOAuthenticated, RequestHandler):
            @authenticated
            def get(self):
                user = self.current_user
                username = user["name"]

                try:
                    server = get_server(username)
                except Exception as exc:
                    self.redirect(
                        f"{SERVICE_PREFIX}/?{urlencode({'msg': f'Could not query Pi server state: {exc}'})}"
                    )
                    return

                if server is None:
                    self.redirect(
                        f"{SERVICE_PREFIX}/?{urlencode({'msg': 'Pi is not running yet. Choose a size to launch.'})}"
                    )
                    return

                if is_legacy_server(server):
                    self.redirect(
                        f"{SERVICE_PREFIX}/?{urlencode({'msg': 'Legacy Pi server detected. Launch a size to migrate it to terminal mode.'})}"
                    )
                    return

                pending = get_pending_state(server)
                if pending:
                    self.redirect(f"/hub/spawn-pending/{quote(username)}/{SERVER_NAME}")
                    return

                if bool(server.get("ready")):
                    self.redirect(f"/user/{quote(username)}/{SERVER_NAME}/")
                    return

                self.redirect(
                    f"{SERVICE_PREFIX}/?{urlencode({'msg': 'Pi server is not ready yet. Check Spawn status.'})}"
                )

        class LaunchHandler(HubOAuthenticated, RequestHandler):
            def _wait_for_server_not_running(self, username: str, timeout_seconds: int = 60) -> bool:
                deadline = time.time() + timeout_seconds
                while time.time() < deadline:
                    try:
                        server = get_server(username)
                    except Exception:
                        return False
                    if server is None:
                        return True
                    pending = get_pending_state(server)
                    if pending:
                        time.sleep(1)
                        continue
                    if bool(server.get("ready")):
                        time.sleep(1)
                        continue
                    if bool(server.get("stopped")):
                        return True
                    time.sleep(1)
                return False

            def _wait_for_server_ready_or_pending(self, username: str, timeout_seconds: int = 30) -> bool:
                deadline = time.time() + timeout_seconds
                while time.time() < deadline:
                    try:
                        server = get_server(username)
                    except Exception:
                        return False
                    if isinstance(server, dict):
                        if bool(server.get("ready")) or bool(get_pending_state(server)):
                            return True
                    time.sleep(1)
                return False

            @authenticated
            def post(self):
                user = self.current_user
                username = user["name"]

                size = (self.get_body_argument("size", "") or "").strip().lower()
                if size not in PROFILE_BY_SIZE:
                    self.set_status(400)
                    self.write(f"Invalid size: {size!r}")
                    return

                profile = PROFILE_BY_SIZE[size]

                try:
                    existing = get_server(username)
                except Exception as exc:
                    self.set_status(500)
                    self.write(f"Failed to query current Pi server state: {exc}")
                    return

                if existing is not None:
                    existing_pending = get_pending_state(existing)
                    if existing_pending:
                        self.redirect(f"/hub/spawn-pending/{quote(username)}/{SERVER_NAME}")
                        return

                    legacy = is_legacy_server(existing)
                    existing_profile = get_server_profile(existing)
                    profile_mismatch = bool(existing_profile) and existing_profile != profile
                    unknown_profile = not existing_profile
                    needs_reconfigure = legacy or profile_mismatch or unknown_profile

                    if bool(existing.get("ready")) and not needs_reconfigure:
                        redirect_with_message(
                            self,
                            "Pi is already running with the selected size.",
                        )
                        return

                    if bool(existing.get("ready")) and needs_reconfigure:
                        stop_resp = hub_request(
                            "DELETE",
                            f"/users/{quote(username)}/servers/{SERVER_NAME}",
                            params={"remove": "false"},
                        )
                        if stop_resp.status_code not in (200, 202, 204, 400, 404):
                            self.set_status(500)
                            self.write(
                                "Failed to stop current Pi server before relaunch.\\n\\n"
                                f"status={stop_resp.status_code}\\n"
                                f"body={stop_resp.text}\\n"
                            )
                            return

                        if not self._wait_for_server_not_running(username):
                            redirect_with_message(
                                self,
                                "Pi is still stopping. Retry launch in a few seconds.",
                            )
                            return

                # Spawn as a regular named-server profile. Do not send jhub-app payload keys.
                resp = hub_request(
                    "POST",
                    f"/users/{quote(username)}/servers/{SERVER_NAME}",
                    json={"profile": profile},
                )

                if resp.status_code in (201, 202):
                    self._wait_for_server_ready_or_pending(username)
                    self.redirect(f"/hub/spawn-pending/{quote(username)}/{SERVER_NAME}")
                    return

                # If already running/already exists, use the smart opener.
                if resp.status_code in (400, 409):
                    try:
                        latest = get_server(username)
                    except Exception:
                        latest = None

                    if isinstance(latest, dict):
                        latest_pending = get_pending_state(latest)
                        if latest_pending:
                            self.redirect(f"/hub/spawn-pending/{quote(username)}/{SERVER_NAME}")
                            return

                        latest_profile = get_server_profile(latest)
                        if bool(latest.get("ready")) and latest_profile == profile and not is_legacy_server(latest):
                            redirect_with_message(
                                self,
                                "Pi is already running with the selected size.",
                            )
                            return

                    redirect_with_message(
                        self,
                        "Pi launch was not accepted yet. If a previous instance is still stopping, retry in a few seconds.",
                    )
                    return

                self.set_status(500)
                self.write(
                    "Failed to spawn server.\\n\\n"
                    f"status={resp.status_code}\\n"
                    f"body={resp.text}\\n"
                )

        class StopHandler(HubOAuthenticated, RequestHandler):
            def _wait_for_not_running(self, username: str, timeout_seconds: int = 45) -> bool:
                deadline = time.time() + timeout_seconds
                while time.time() < deadline:
                    try:
                        server = get_server(username)
                    except Exception:
                        return False
                    if server is None:
                        return True
                    if not bool(server.get("ready")) and (
                        bool(server.get("stopped")) or bool(get_pending_state(server))
                    ):
                        return True
                    time.sleep(1)
                return False

            @authenticated
            def post(self):
                user = self.current_user
                username = user["name"]

                resp = hub_request(
                    "DELETE",
                    f"/users/{quote(username)}/servers/{SERVER_NAME}",
                    params={"remove": "false"},
                )
                if resp.status_code not in (200, 202, 204, 400, 404):
                    self.set_status(500)
                    self.write(
                        "Failed to stop server.\\n\\n"
                        f"status={resp.status_code}\\n"
                        f"body={resp.text}\\n"
                    )
                    return

                if resp.status_code in (400, 404):
                    redirect_with_message(self, "Pi is already stopped.")
                    return

                if self._wait_for_not_running(username):
                    redirect_with_message(self, "Pi stopped successfully.")
                else:
                    redirect_with_message(
                        self,
                        "Stop requested. Pi is still shutting down; refresh in a few seconds.",
                    )

        class ShareBaseHandler(HubOAuthenticated, RequestHandler):
            def _payload(self):
                content_type = (self.request.headers.get("Content-Type") or "").lower()
                if "application/json" in content_type:
                    try:
                        data = json.loads((self.request.body or b"{}").decode("utf-8"))
                    except Exception:
                        data = {}
                    return data if isinstance(data, dict) else {}
                return {}

            def _arg(self, name: str, default=""):
                payload = self._payload()
                if name in payload:
                    return payload.get(name)
                return self.get_body_argument(name, default=default)

            def _current_username(self):
                user = self.current_user
                return user["name"] if isinstance(user, dict) else ""

            def _prefer_json(self):
                if "application/json" in (self.request.headers.get("Accept") or "").lower():
                    return True
                content_type = (self.request.headers.get("Content-Type") or "").lower()
                return "application/json" in content_type

            def _fail(self, message: str, status: int = 400, redirect_path: str = ""):
                if self._prefer_json():
                    json_response(self, {"ok": False, "error": message}, status=status)
                    return
                target = redirect_path or f"{SERVICE_PREFIX}/share"
                self.redirect(f"{target}?{urlencode({'msg': message})}")

            def _ok(self, payload, redirect_message: str = "", redirect_path: str = ""):
                if self._prefer_json():
                    json_response(self, payload, status=200)
                    return
                target = redirect_path or f"{SERVICE_PREFIX}/share"
                self.redirect(
                    f"{target}?{urlencode({'msg': redirect_message or 'Completed successfully.'})}"
                )

        class SharePageHandler(ShareBaseHandler):
            @authenticated
            def get(self):
                username = self._current_username()
                msg = (self.get_argument("msg", "") or "").strip()
                xsrf_token = self.xsrf_token.decode("utf-8")
                try:
                    groups = ", ".join(get_user_group_names(username)) or "(none)"
                except Exception:
                    groups = "(unknown)"
                live_status = "enabled" if LIVE_SHARE_ENABLED else "disabled"
                self.set_header("Content-Type", "text/html; charset=utf-8")
                self.write(
                    f'''<!doctype html>
        <html lang="en">
          <head>
            <meta charset="utf-8" />
            <meta name="viewport" content="width=device-width, initial-scale=1" />
            <title>Pi Session Sharing</title>
            <style>
              body {{ font-family: ui-sans-serif, system-ui, sans-serif; margin: 0; padding: 24px; background: #0b1020; color: #f4f4f7; }}
              .wrap {{ max-width: 960px; margin: 0 auto; }}
              .card {{ border: 1px solid #2e3a57; border-radius: 12px; padding: 16px; margin-bottom: 12px; background: #121a2f; }}
              h1, h2 {{ margin-top: 0; }}
              input {{ width: 100%; box-sizing: border-box; padding: 8px; border-radius: 8px; border: 1px solid #3e4c72; background: #0b1020; color: #f4f4f7; }}
              .row {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }}
              .row3 {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }}
              .btn {{ margin-top: 10px; padding: 10px 12px; border-radius: 8px; border: 1px solid #5a6ea5; background: #1f2a48; color: #fff; cursor: pointer; }}
              .msg {{ padding: 10px; border-radius: 8px; border: 1px solid #425989; background: #182240; margin-bottom: 12px; }}
              a {{ color: #9fc2ff; }}
              code {{ color: #d2e0ff; }}
            </style>
          </head>
          <body>
            <div class="wrap">
              <h1>Pi Session Sharing</h1>
              <p>User: <code>{html.escape(username)}</code> | Groups: <code>{html.escape(groups)}</code> | Live sharing: <code>{live_status}</code></p>
              {f'<div class="msg">{html.escape(msg)}</div>' if msg else ''}
              <div class="card">
                <h2>Phase 1: Create Fork Share</h2>
                <form method="post" action="{SERVICE_PREFIX}/share/session">
                  <input type="hidden" name="_xsrf" value="{xsrf_token}" />
                  <div class="row">
                    <label>Session path in your Pi pod
                      <input name="session_path" placeholder="{html.escape(PI_AGENT_DIR)}/sessions/...jsonl" required />
                    </label>
                    <label>Title
                      <input name="title" placeholder="My shared session" />
                    </label>
                  </div>
                  <div class="row3">
                    <label>Users (comma separated)
                      <input name="share_with_users" placeholder="alice,bob" />
                    </label>
                    <label>Groups (comma separated)
                      <input name="share_with_groups" placeholder="data-science" />
                    </label>
                    <label>Expires in hours
                      <input name="expires_hours" value="24" />
                    </label>
                  </div>
                  <button class="btn" type="submit">Create Session Share</button>
                </form>
              </div>
              <div class="card">
                <h2>Phase 1: Import Share</h2>
                <form method="post" action="{SERVICE_PREFIX}/share/session/import">
                  <input type="hidden" name="_xsrf" value="{xsrf_token}" />
                  <div class="row3">
                    <label>Share ID
                      <input name="share_id" placeholder="12-char id" required />
                    </label>
                    <label>Destination (optional)
                      <input name="target_path" placeholder="{html.escape(PI_AGENT_DIR)}/sessions/imported/...jsonl" />
                    </label>
                    <label>Start profile if Pi is stopped
                      <input name="profile" value="pi-small" />
                    </label>
                  </div>
                  <button class="btn" type="submit">Import Into My Pi Pod</button>
                </form>
              </div>
              <div class="card">
                <h2>Phase 2: Live Share (admin only)</h2>
                <form method="post" action="{SERVICE_PREFIX}/share/live">
                  <input type="hidden" name="_xsrf" value="{xsrf_token}" />
                  <div class="row3">
                    <label>Owner (default: me)
                      <input name="owner" placeholder="{html.escape(username)}" />
                    </label>
                    <label>Users (comma separated)
                      <input name="share_with_users" placeholder="alice,bob" />
                    </label>
                    <label>Groups (comma separated)
                      <input name="share_with_groups" placeholder="ops-team" />
                    </label>
                  </div>
                  <div class="row3">
                    <label>Expires in hours
                      <input name="expires_hours" value="8" />
                    </label>
                  </div>
                  <button class="btn" type="submit">Create Live Share</button>
                </form>
                <form method="post" action="{SERVICE_PREFIX}/share/live/revoke">
                  <input type="hidden" name="_xsrf" value="{xsrf_token}" />
                  <div class="row3">
                    <label>Owner
                      <input name="owner" placeholder="{html.escape(username)}" />
                    </label>
                    <label>Share ID (optional)
                      <input name="share_id" placeholder="leave blank to revoke all owner live shares" />
                    </label>
                  </div>
                  <button class="btn" type="submit">Revoke Live Share</button>
                </form>
              </div>
              <div class="card">
                <h2>Inspect Shares</h2>
                <p><a href="{SERVICE_PREFIX}/share/list">Open JSON view of visible shares</a></p>
                <p><a href="{SERVICE_PREFIX}/">Back to Pi Launcher</a></p>
              </div>
            </div>
          </body>
        </html>'''
                )

        class ShareListHandler(ShareBaseHandler):
            @authenticated
            def get(self):
                username = self._current_username()
                try:
                    viewer_groups = get_user_group_names(username)
                except Exception:
                    viewer_groups = []
                try:
                    viewer_is_admin = is_user_admin(username)
                except Exception:
                    viewer_is_admin = False
                session_shares = []
                for secret_obj in _list_share_secrets("session"):
                    metadata = _load_share_metadata(secret_obj)
                    if not metadata:
                        continue
                    if not _share_visible_to_user(metadata, username, viewer_groups):
                        continue
                    metadata = dict(metadata)
                    metadata["expired"] = _share_expired(metadata)
                    session_shares.append(metadata)
                live_shares = []
                for secret_obj in _list_share_secrets("live"):
                    metadata = _load_share_metadata(secret_obj)
                    if not metadata:
                        continue
                    visible = (
                        username == str(metadata.get("owner", "")).strip()
                        or username in _parse_targets(metadata.get("share_with_users"))
                        or bool(
                            set(viewer_groups).intersection(
                            set(_parse_targets(metadata.get("share_with_groups")))
                            )
                        )
                    )
                    if visible or viewer_is_admin:
                        metadata = dict(metadata)
                        metadata["expired"] = _share_expired(metadata)
                        live_shares.append(metadata)
                json_response(
                    self,
                    {
                        "ok": True,
                        "user": username,
                        "live_share_enabled": LIVE_SHARE_ENABLED,
                        "session_shares": sorted(
                            session_shares, key=lambda x: str(x.get("created_at", "")), reverse=True
                        ),
                        "live_shares": sorted(
                            live_shares, key=lambda x: str(x.get("created_at", "")), reverse=True
                        ),
                    },
                )

        class ShareSessionCreateHandler(ShareBaseHandler):
            @authenticated
            def post(self):
                username = self._current_username()
                session_path = str(self._arg("session_path", "") or "").strip()
                title = str(self._arg("title", "") or "").strip()
                users = self._arg("share_with_users", "")
                groups = self._arg("share_with_groups", "")
                expires_hours_raw = str(self._arg("expires_hours", "24") or "24").strip()
                try:
                    expires_hours = int(expires_hours_raw)
                except Exception:
                    return self._fail("expires_hours must be an integer.")
                if not session_path:
                    return self._fail("session_path is required.")
                try:
                    metadata = _create_session_share(
                        owner=username,
                        session_path=session_path,
                        title=title,
                        share_with_users=users,
                        share_with_groups=groups,
                        expires_hours=expires_hours,
                    )
                    self._ok(
                        {"ok": True, "share": metadata},
                        redirect_message=f"Created session share: {metadata.get('id')}",
                    )
                except Exception as exc:
                    self._fail(str(exc))

        class ShareSessionImportHandler(ShareBaseHandler):
            @authenticated
            def post(self):
                username = self._current_username()
                share_id = _normalize_share_id(str(self._arg("share_id", "") or ""))
                if not share_id:
                    return self._fail("share_id is required.")
                target_path = str(self._arg("target_path", "") or "").strip()
                if not target_path:
                    session_bucket = _encode_session_dir(f"/home/{username}")
                    stamp = _utcnow().strftime("%Y-%m-%dT%H-%M-%S-%f")[:-3] + "Z"
                    target_path = (
                        f"{PI_AGENT_DIR}/sessions/{session_bucket}/"
                        f"{stamp}_imported-share-{share_id}.jsonl"
                    )
                profile = str(self._arg("profile", "pi-small") or "pi-small").strip()
                try:
                    secret_obj = _secret_for_session_share_id(share_id)
                except Exception as exc:
                    return self._fail(f"Share not found: {exc}")
                metadata = _load_share_metadata(secret_obj)
                if not metadata:
                    return self._fail("Share metadata is missing or invalid.")
                if _share_expired(metadata):
                    return self._fail("Share has expired.")
                try:
                    viewer_groups = get_user_group_names(username)
                except Exception:
                    viewer_groups = []
                if not _share_visible_to_user(metadata, username, viewer_groups):
                    return self._fail("You do not have permission to import this share.", status=403)

                owner = str(metadata.get("owner", "")).strip()
                if not owner:
                    return self._fail("Share metadata is invalid: missing owner.")

                raw_session = _parse_secret_data(secret_obj, "session.jsonl")
                if not raw_session:
                    return self._fail("Shared session payload is missing.")

                try:
                    # If requester's Pi server isn't running, best effort start it with requested profile.
                    pod_name = get_pi_pod_name(username)
                except Exception:
                    start_resp = hub_request(
                        "POST",
                        f"/users/{quote(username)}/servers/{SERVER_NAME}",
                        json={"profile": profile},
                    )
                    if start_resp.status_code not in (201, 202, 400, 409):
                        return self._fail(
                            f"Unable to start your Pi server for import: "
                            f"status={start_resp.status_code} body={start_resp.text}"
                        )
                    deadline = time.time() + 90
                    pod_name = ""
                    while time.time() < deadline:
                        try:
                            pod_name = get_pi_pod_name(username)
                        except Exception:
                            time.sleep(2)
                            continue
                        if pod_name:
                            break
                    if not pod_name:
                        return self._fail("Timed out waiting for your Pi server to start.")
                try:
                    destination = write_session_file_to_pi(
                        username,
                        target_path,
                        raw_session.encode("utf-8"),
                    )
                except Exception as exc:
                    return self._fail(f"Failed to import session: {exc}")

                self._ok(
                    {
                        "ok": True,
                        "share_id": share_id,
                        "owner": owner,
                        "imported_to": destination,
                    },
                    redirect_message=f"Imported share {share_id} to {destination}",
                )

        class ShareLiveCreateHandler(ShareBaseHandler):
            @authenticated
            def post(self):
                if not LIVE_SHARE_ENABLED:
                    return self._fail(
                        "Live sharing is disabled. Set PI_LIVE_SHARE_ENABLED=1 for this service.",
                        status=403,
                    )
                username = self._current_username()
                try:
                    if not is_user_admin(username):
                        return self._fail("Live sharing is admin-only.", status=403)
                except Exception as exc:
                    return self._fail(f"Unable to verify admin permissions: {exc}", status=403)

                owner = str(self._arg("owner", "") or "").strip() or username
                users = self._arg("share_with_users", "")
                groups = self._arg("share_with_groups", "")
                expires_hours_raw = str(self._arg("expires_hours", "8") or "8").strip()
                try:
                    expires_hours = int(expires_hours_raw)
                except Exception:
                    return self._fail("expires_hours must be an integer.")

                try:
                    get_user(owner)
                except Exception as exc:
                    return self._fail(f"Owner user not found: {exc}")

                try:
                    metadata, reconcile = _create_live_share(
                        owner=owner,
                        share_with_users=users,
                        share_with_groups=groups,
                        expires_hours=expires_hours,
                        created_by=username,
                    )
                    self._ok(
                        {
                            "ok": True,
                            "share": metadata,
                            "reconcile": reconcile,
                        },
                        redirect_message=f"Created live share: {metadata.get('id')}",
                    )
                except Exception as exc:
                    self._fail(str(exc))

        class ShareLiveRevokeHandler(ShareBaseHandler):
            @authenticated
            def post(self):
                if not LIVE_SHARE_ENABLED:
                    return self._fail(
                        "Live sharing is disabled. Set PI_LIVE_SHARE_ENABLED=1 for this service.",
                        status=403,
                    )
                username = self._current_username()
                try:
                    if not is_user_admin(username):
                        return self._fail("Live sharing is admin-only.", status=403)
                except Exception as exc:
                    return self._fail(f"Unable to verify admin permissions: {exc}", status=403)

                owner = str(self._arg("owner", "") or "").strip() or username
                share_id = str(self._arg("share_id", "") or "").strip()
                try:
                    reconcile = _delete_live_share(owner=owner, share_id=share_id)
                    message = (
                        f"Revoked live share {share_id} for {owner}."
                        if share_id
                        else f"Revoked all live shares for {owner}."
                    )
                    self._ok(
                        {"ok": True, "owner": owner, "share_id": share_id, "reconcile": reconcile},
                        redirect_message=message,
                    )
                except Exception as exc:
                    self._fail(str(exc))

        def make_app():
            prefix = re.escape(SERVICE_PREFIX)
            return Application(
                [
                    (r"^" + prefix + r"/status/?$", StatusHandler),
                    (r"^" + prefix + r"/?$", RootHandler),
                    (r"^" + prefix + r"/open/?$", OpenHandler),
                    (r"^" + prefix + r"/launch/?$", LaunchHandler),
                    (r"^" + prefix + r"/stop/?$", StopHandler),
                    (r"^" + prefix + r"/share/?$", SharePageHandler),
                    (r"^" + prefix + r"/share/list/?$", ShareListHandler),
                    (r"^" + prefix + r"/share/session/?$", ShareSessionCreateHandler),
                    (r"^" + prefix + r"/share/session/import/?$", ShareSessionImportHandler),
                    (r"^" + prefix + r"/share/live/?$", ShareLiveCreateHandler),
                    (r"^" + prefix + r"/share/live/revoke/?$", ShareLiveRevokeHandler),
                    (r"^" + prefix + r"/oauth_callback$", HubOAuthCallbackHandler),
                ],
                cookie_secret=os.urandom(32),
            )

        if __name__ == "__main__":
            port = int(os.environ.get("PORT", "10300"))
            app = make_app()
            app.listen(port, address="0.0.0.0")
            IOLoop.current().start()
        """
    )
)

import z2jh

pi_launcher_port = int(z2jh.get_config("custom.pi-launcher-port", 10300))
pi_launcher_service_name = str(z2jh.get_config("custom.pi-launcher-service-name", "pi-launcher") or "pi-launcher")
pi_live_share_enabled_cfg = z2jh.get_config("custom.pi-live-share-enabled", False)
pi_live_share_enabled = "1" if bool(pi_live_share_enabled_cfg) else "0"
pi_sharing_namespace = str(z2jh.get_config("custom.pi-sharing-namespace", "") or "").strip()
pi_share_session_max_bytes = str(z2jh.get_config("custom.pi-share-session-max-bytes", 1048576))
pi_coding_agent_dir = str(z2jh.get_config("custom.pi-coding-agent-dir", "/tmp/pi-agent") or "/tmp/pi-agent").strip()

pi_profiles_cfg = z2jh.get_config("custom.pi-profiles", {}) or {}

def _format_cpu_value(value):
    if value is None:
        return ""
    try:
        f = float(value)
        if f.is_integer():
            return str(int(f))
    except Exception:
        pass
    return str(value)

def _profile_spec_text(size_key, default_text):
    if isinstance(pi_profiles_cfg, dict):
        spec = pi_profiles_cfg.get(size_key)
        if isinstance(spec, dict):
            cpu = _format_cpu_value(spec.get("cpu_limit"))
            mem = str(spec.get("mem_limit") or "").strip()
            if cpu and mem:
                return f"{cpu} cpu / {mem} RAM"
    return default_text

pi_profile_small_spec = _profile_spec_text("small", "2 cpu / 8G RAM")
pi_profile_medium_spec = _profile_spec_text("medium", "4 cpu / 16G RAM")
pi_profile_large_spec = _profile_spec_text("large", "8 cpu / 32G RAM")

public_host = z2jh.get_config("custom.external-url") or ""
public_host = str(public_host).strip().rstrip("/")
if public_host and not public_host.startswith(("http://", "https://")):
    public_scheme = str(z2jh.get_config("custom.external-url-scheme", "https") or "https").strip()
    public_host = f"{public_scheme}://{public_host}"
pi_launcher_oauth_redirect_uri = f"{public_host}/services/{pi_launcher_service_name}/oauth_callback"

launcher_env = {
    # Not required for HubOAuth itself, but useful if we extend the launcher later.
    "PUBLIC_HOST": public_host,
    "PORT": str(pi_launcher_port),
    "JUPYTERHUB_OAUTH_CALLBACK_URL": pi_launcher_oauth_redirect_uri,
    # Phase 2 feature flag: off by default unless custom.pi-live-share-enabled=true.
    "PI_LIVE_SHARE_ENABLED": pi_live_share_enabled,
    "PI_SHARE_SESSION_MAX_BYTES": pi_share_session_max_bytes,
    "PI_CODING_AGENT_DIR": pi_coding_agent_dir,
    "PI_PROFILE_SMALL_SPEC": pi_profile_small_spec,
    "PI_PROFILE_MEDIUM_SPEC": pi_profile_medium_spec,
    "PI_PROFILE_LARGE_SPEC": pi_profile_large_spec,
}
if pi_sharing_namespace:
    launcher_env["PI_SHARING_NAMESPACE"] = pi_sharing_namespace

c.JupyterHub.services.append(
    {
        "name": pi_launcher_service_name,
        "url": f"http://hub:{pi_launcher_port}",
        "command": [
            "python",
            str(pi_launcher_py),
        ],
        "environment": launcher_env,
        "oauth_redirect_uri": pi_launcher_oauth_redirect_uri,
        "oauth_no_confirm": True,
        "display": False,
    }
)

pi_launcher_role = {
    "name": "pi-launcher-service-role",
    "services": [pi_launcher_service_name],
    "scopes": [
        # Allow creating/starting/stopping servers for the current user.
        # For demo speed/simplicity we grant admin:servers rather than acting as the user.
        "admin:servers",
        "admin:server_state",
        "read:users",
        "read:users:name",
        "read:groups",
        "list:groups",
        "shares",
        "access:services",
        "read:services",
        "list:services",
    ],
}
if not c.JupyterHub.load_roles:
    c.JupyterHub.load_roles = []
if isinstance(c.JupyterHub.load_roles, list):
    c.JupyterHub.load_roles.append(pi_launcher_role)
else:
    c.JupyterHub.load_roles = [pi_launcher_role]

# M4 tooling token for app deploy/log wrappers inside Pi servers.
# Token is injected via hub env (PI_M4_TOOLS_API_TOKEN) from chart-managed Secret.
pi_tools_api_token = (os.environ.get("PI_M4_TOOLS_API_TOKEN") or "").strip()
pi_tools_service_name = str(z2jh.get_config("custom.pi-m4-tools-service-name", "pi-m4-tools") or "pi-m4-tools")

if pi_tools_api_token:
    c.JupyterHub.services.append(
        {
            "name": pi_tools_service_name,
            "api_token": pi_tools_api_token,
            "display": False,
        }
    )

    pi_tools_role = {
        "name": "pi-m4-tools-role",
        "services": [pi_tools_service_name],
        "scopes": [
            "admin:servers",
            "admin:server_state",
            "read:users",
            "read:users:name",
            "access:servers",
        ],
    }
    if isinstance(c.JupyterHub.load_roles, list):
        c.JupyterHub.load_roles.append(pi_tools_role)
    else:
        c.JupyterHub.load_roles = [pi_tools_role]
else:
    print("WARN: PI_M4_TOOLS_API_TOKEN not set; skipping pi-m4-tools service registration")
