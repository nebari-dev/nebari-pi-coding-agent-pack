#!/usr/bin/env python3
"""CLI wrappers for jhub-app app lifecycle operations.

This script is installed multiple times via symlinks:
- nebari_app_deploy
- nebari_app_status
- nebari_app_logs
- nebari_app_stop
- nebari_app_delete
- nebari_app_doctor

It uses Hub API credentials exposed to Pi profiles via:
- NEBARI_HUB_API_URL / NEBARI_HUB_API_TOKEN (preferred)
- JUPYTERHUB_API_URL / JUPYTERHUB_API_TOKEN (fallback)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import requests


VALID_FRAMEWORKS = {"streamlit", "panel", "voila", "gradio", "jupyterlab", "custom"}


class CliError(RuntimeError):
    pass


@dataclass
class ClientConfig:
    api_url: str
    api_token: str
    proxy_url: str
    username: str


def _default_username() -> str:
    username = (os.environ.get("JUPYTERHUB_USER") or os.environ.get("NB_USER") or "").strip()
    if username:
        return username

    prefix = (os.environ.get("JUPYTERHUB_SERVICE_PREFIX") or "").strip()
    # Expected: /user/<username>/<servername>/
    m = re.match(r"^/user/([^/]+)/", prefix)
    if m:
        return m.group(1)
    return ""


def load_config(username_override: str = "") -> ClientConfig:
    api_url = (
        (os.environ.get("NEBARI_HUB_API_URL") or "").strip()
        or (os.environ.get("JUPYTERHUB_API_URL") or "").strip()
    )
    api_token = (
        (os.environ.get("NEBARI_HUB_API_TOKEN") or "").strip()
        or (os.environ.get("JUPYTERHUB_API_TOKEN") or "").strip()
    )
    proxy_url = (
        (os.environ.get("NEBARI_PROXY_URL") or "").strip()
        or (os.environ.get("JUPYTERHUB_PUBLIC_URL") or "").strip()
        or "http://proxy-public"
    )
    current_user = _default_username()
    username = (username_override or "").strip() or current_user

    if not api_url:
        raise CliError("Missing NEBARI_HUB_API_URL/JUPYTERHUB_API_URL in environment.")
    if not api_token:
        raise CliError("Missing NEBARI_HUB_API_TOKEN/JUPYTERHUB_API_TOKEN in environment.")
    if not username:
        raise CliError("Unable to determine target user. Set --user or JUPYTERHUB_USER.")

    if (
        username_override
        and current_user
        and username != current_user
        and os.environ.get("NEBARI_APP_ALLOW_OTHER_USERS", "").strip().lower()
        not in {"1", "true", "yes", "on"}
    ):
        raise CliError(
            f"Refusing cross-user action ({current_user} -> {username}). "
            "Set NEBARI_APP_ALLOW_OTHER_USERS=1 to override explicitly."
        )

    return ClientConfig(
        api_url=api_url.rstrip("/"),
        api_token=api_token,
        proxy_url=proxy_url.rstrip("/"),
        username=username,
    )


def _session(cfg: ClientConfig) -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "Authorization": f"token {cfg.api_token}",
            "Accept": "application/json",
        }
    )
    return s


def _slugify_name(name: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", (name or "").strip().lower()).strip("-")
    if not slug:
        raise CliError("App name cannot be empty after normalization.")
    return slug


def _parse_csv(raw: str) -> list[str]:
    if not raw:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw.split(","):
        value = item.strip()
        if not value:
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def _parse_env_json(raw: str) -> dict[str, str]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception as exc:
        raise CliError(f"Invalid --env-json payload: {exc}") from exc
    if not isinstance(data, dict):
        raise CliError("--env-json must decode to a JSON object.")
    out: dict[str, str] = {}
    for k, v in data.items():
        key = str(k).strip()
        if not key:
            continue
        out[key] = "" if v is None else str(v)
    return out


def _hub_get_user_server(cfg: ClientConfig, app_name: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
    name = _slugify_name(app_name)
    s = _session(cfg)
    user_path = quote(cfg.username, safe="")
    r = s.get(
        f"{cfg.api_url}/users/{user_path}",
        params={"include_stopped_servers": "true"},
        timeout=30,
    )
    if r.status_code >= 400:
        raise CliError(f"Hub user lookup failed ({r.status_code}): {r.text}")
    user_payload = r.json()
    servers = user_payload.get("servers") if isinstance(user_payload, dict) else {}
    server = servers.get(name) if isinstance(servers, dict) else None
    return user_payload, server


def _validate_custom_command(custom_command: str) -> None:
    cmd = (custom_command or "").strip()
    if not cmd:
        raise CliError("--custom-command is required for framework=custom.")
    try:
        first = shlex.split(cmd)[0]
    except Exception:
        first = cmd.split()[0] if cmd.split() else ""
    low = first.lower()

    if re.match(r"^python(\d+(\.\d+)*)?$", low):
        raise CliError(
            "custom command must be module-style (no python/python -m prefix). "
            "Example: 'my_package.server {--}port={port}'"
        )
    if low.endswith(".py") or "/" in first:
        raise CliError(
            "custom command should point to a Python module, not a script path. "
            "Example: 'my_package.server {--}port={port}'"
        )
    if "{port}" not in cmd:
        raise CliError("custom command must include the {port} placeholder.")


def cmd_deploy(args: argparse.Namespace) -> int:
    cfg = load_config(args.user)
    app_name = _slugify_name(args.name)
    framework = (args.framework or "").strip().lower()

    if framework not in VALID_FRAMEWORKS:
        raise CliError(f"Unsupported framework: {framework}. Valid: {sorted(VALID_FRAMEWORKS)}")

    filepath = args.filepath if args.filepath is not None else "None"
    custom_command = (args.custom_command or "").strip()

    if framework == "custom":
        _validate_custom_command(custom_command)
    elif not (filepath or "").strip():
        raise CliError("--filepath is required for non-custom frameworks.")

    s = _session(cfg)
    user_path = quote(cfg.username, safe="")
    server_path = quote(app_name, safe="")

    if args.replace:
        s.delete(
            f"{cfg.api_url}/users/{user_path}/servers/{server_path}",
            params={"remove": "true"},
            timeout=30,
        )
        # Best-effort short wait for cleanup.
        time.sleep(1)

    payload: dict[str, Any] = {
        "name": app_name,
        "display_name": (args.display_name or app_name).strip(),
        "description": (args.description or "").strip(),
        "filepath": filepath,
        "framework": framework,
        "custom_command": custom_command,
        "public": bool(args.public),
        "keep_alive": bool(args.keep_alive),
        "env": _parse_env_json(args.env_json),
        "conda_env": (args.conda_env or "").strip(),
        "profile": (args.profile or "").strip(),
        "profile_image": (args.profile_image or "").strip(),
        "jhub_app": True,
        "share_with": {
            "users": _parse_csv(args.share_users),
            "groups": _parse_csv(args.share_groups),
        },
    }

    r = s.post(
        f"{cfg.api_url}/users/{user_path}/servers/{server_path}",
        json=payload,
        timeout=60,
    )
    if r.status_code not in (201, 202):
        raise CliError(f"Deploy failed ({r.status_code}): {r.text}")

    print(f"ok: deploy accepted for '{app_name}' (status={r.status_code})")
    print(f"url: /user/{cfg.username}/{app_name}/")
    if args.wait_seconds > 0:
        deadline = time.time() + args.wait_seconds
        while time.time() < deadline:
            _, server = _hub_get_user_server(cfg, app_name)
            if isinstance(server, dict) and (server.get("ready") or server.get("pending")):
                break
            time.sleep(1)
        _, server = _hub_get_user_server(cfg, app_name)
        print(
            json.dumps(
                {
                    "ready": bool(server.get("ready")) if isinstance(server, dict) else False,
                    "pending": server.get("pending") if isinstance(server, dict) else None,
                    "stopped": bool(server.get("stopped")) if isinstance(server, dict) else True,
                },
                indent=2,
            )
        )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = load_config(args.user)
    app_name = _slugify_name(args.name)
    _, server = _hub_get_user_server(cfg, app_name)

    if server is None:
        raise CliError(f"App '{app_name}' not found for user '{cfg.username}'.")

    if args.json:
        print(json.dumps(server, indent=2, sort_keys=True))
        return 0

    opts = server.get("user_options") if isinstance(server, dict) else {}
    opts = opts if isinstance(opts, dict) else {}
    print(f"name: {app_name}")
    print(f"url: {server.get('url', f'/user/{cfg.username}/{app_name}/')}")
    print(f"ready: {bool(server.get('ready'))}")
    print(f"pending: {server.get('pending')}")
    print(f"stopped: {bool(server.get('stopped'))}")
    print(f"started: {server.get('started')}")
    print(f"last_activity: {server.get('last_activity')}")
    if opts:
        print(f"framework: {opts.get('framework', '')}")
        print(f"profile: {opts.get('profile', '')}")
        if opts.get("custom_command"):
            print(f"custom_command: {opts.get('custom_command')}")
    return 0


def _fetch_proxy_logs(cfg: ClientConfig, app_name: str, lines: int) -> requests.Response:
    path = f"/user/{quote(cfg.username, safe='')}/{quote(app_name, safe='')}/_temp/jhub-app-proxy/api/logs"
    url = f"{cfg.proxy_url}{path}"
    s = _session(cfg)
    return s.get(url, params={"lines": max(1, lines)}, allow_redirects=True, timeout=30)


def cmd_logs(args: argparse.Namespace) -> int:
    cfg = load_config(args.user)
    app_name = _slugify_name(args.name)

    r = _fetch_proxy_logs(cfg, app_name, args.lines)
    if r.status_code == 200:
        content_type = (r.headers.get("content-type") or "").lower()
        if "application/json" in content_type:
            try:
                payload = r.json()
            except Exception:
                print(r.text)
                return 0
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(r.text)
        return 0

    # Fallback diagnostics if temp logs are unavailable.
    print(f"warning: could not fetch startup logs (status={r.status_code})", file=sys.stderr)
    body = (r.text or "").strip()
    if body:
        print(body, file=sys.stderr)
    try:
        _, server = _hub_get_user_server(cfg, app_name)
    except Exception as exc:
        print(f"status lookup failed: {exc}", file=sys.stderr)
        return 1

    if not isinstance(server, dict):
        print(f"app '{app_name}' not found", file=sys.stderr)
        return 1

    summary = {
        "name": app_name,
        "url": server.get("url", f"/user/{cfg.username}/{app_name}/"),
        "ready": bool(server.get("ready")),
        "pending": server.get("pending"),
        "stopped": bool(server.get("stopped")),
        "started": server.get("started"),
        "last_activity": server.get("last_activity"),
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 1


def _wait_until_absent_or_stopped(cfg: ClientConfig, app_name: str, timeout_seconds: int, absent_ok: bool) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        _, server = _hub_get_user_server(cfg, app_name)
        if server is None and absent_ok:
            return
        if isinstance(server, dict):
            if bool(server.get("stopped")) and not server.get("pending"):
                return
        time.sleep(1)


def cmd_stop(args: argparse.Namespace) -> int:
    cfg = load_config(args.user)
    app_name = _slugify_name(args.name)

    s = _session(cfg)
    user_path = quote(cfg.username, safe="")
    server_path = quote(app_name, safe="")
    r = s.delete(
        f"{cfg.api_url}/users/{user_path}/servers/{server_path}",
        params={"remove": "false"},
        timeout=60,
    )

    if r.status_code not in (200, 202, 204, 400, 404):
        raise CliError(f"Stop failed ({r.status_code}): {r.text}")

    print(f"ok: stop requested for '{app_name}' (status={r.status_code})")
    if args.wait_seconds > 0:
        _wait_until_absent_or_stopped(cfg, app_name, args.wait_seconds, absent_ok=False)
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    cfg = load_config(args.user)
    app_name = _slugify_name(args.name)

    s = _session(cfg)
    user_path = quote(cfg.username, safe="")
    server_path = quote(app_name, safe="")
    r = s.delete(
        f"{cfg.api_url}/users/{user_path}/servers/{server_path}",
        params={"remove": "true"},
        timeout=60,
    )

    if r.status_code not in (200, 202, 204, 404):
        raise CliError(f"Delete failed ({r.status_code}): {r.text}")

    print(f"ok: delete requested for '{app_name}' (status={r.status_code})")
    if args.wait_seconds > 0:
        _wait_until_absent_or_stopped(cfg, app_name, args.wait_seconds, absent_ok=True)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = load_config(args.user)
    s = _session(cfg)

    checks: list[dict[str, Any]] = []

    def add_check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    # 1) Hub API reachability with current token.
    r = s.get(cfg.api_url, timeout=20)
    if r.status_code == 200:
        version = ""
        try:
            payload = r.json()
            if isinstance(payload, dict):
                version = str(payload.get("version") or "")
        except Exception:
            version = ""
        add_check("hub_api", True, f"reachable ({cfg.api_url})" + (f", version={version}" if version else ""))
    else:
        add_check("hub_api", False, f"status={r.status_code} body={r.text[:180]}")

    # 2) User lookup scope and server visibility.
    user_path = quote(cfg.username, safe="")
    r_user = s.get(
        f"{cfg.api_url}/users/{user_path}",
        params={"include_stopped_servers": "true"},
        timeout=20,
    )
    if r_user.status_code == 200:
        server_count = 0
        try:
            payload = r_user.json()
            servers = payload.get("servers") if isinstance(payload, dict) else {}
            server_count = len(servers) if isinstance(servers, dict) else 0
        except Exception:
            server_count = 0
        add_check("hub_user_lookup", True, f"user={cfg.username}, servers={server_count}")
    else:
        add_check("hub_user_lookup", False, f"status={r_user.status_code} body={r_user.text[:180]}")

    # 3) Proxy reachability.
    try:
        r_proxy = requests.get(f"{cfg.proxy_url}/hub/health", timeout=20, allow_redirects=False)
        add_check(
            "proxy_health",
            r_proxy.status_code == 200,
            f"status={r_proxy.status_code} url={cfg.proxy_url}/hub/health",
        )
    except requests.RequestException as exc:
        add_check("proxy_health", False, f"request failed: {exc}")

    # 4) Optional app-specific checks.
    app_name = (args.name or "").strip()
    if app_name:
        normalized = _slugify_name(app_name)
        try:
            _, server = _hub_get_user_server(cfg, normalized)
        except Exception as exc:
            add_check("app_lookup", False, f"name={normalized}: {exc}")
            server = None
        if isinstance(server, dict):
            ready = bool(server.get("ready"))
            pending = server.get("pending")
            stopped = bool(server.get("stopped"))
            add_check(
                "app_lookup",
                True,
                f"name={normalized}, ready={ready}, pending={pending}, stopped={stopped}",
            )

            if not args.no_log_probe:
                try:
                    log_resp = _fetch_proxy_logs(cfg, normalized, lines=20)
                    log_ok = log_resp.status_code in (200, 424)
                    add_check(
                        "app_logs_probe",
                        log_ok,
                        f"status={log_resp.status_code} ({cfg.proxy_url}/user/{cfg.username}/{normalized}/_temp/jhub-app-proxy/api/logs)",
                    )
                except requests.RequestException as exc:
                    add_check("app_logs_probe", False, f"request failed: {exc}")
        else:
            add_check("app_lookup", False, f"name={normalized}: not found")

    ok = all(bool(item.get("ok")) for item in checks)
    payload = {
        "ok": ok,
        "user": cfg.username,
        "api_url": cfg.api_url,
        "proxy_url": cfg.proxy_url,
        "checks": checks,
    }

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        for item in checks:
            prefix = "[ok]" if item.get("ok") else "[fail]"
            print(f"{prefix} {item.get('name')}: {item.get('detail')}")
        print(f"overall: {'ok' if ok else 'fail'}")

    return 0 if ok else 1


def build_parser(mode: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=mode)

    if mode == "nebari_app_deploy":
        p.add_argument("--name", required=True, help="App name")
        p.add_argument("--framework", required=True, help=f"One of: {', '.join(sorted(VALID_FRAMEWORKS))}")
        p.add_argument("--filepath", default="None", help="App file path (or directory for custom mode)")
        p.add_argument("--custom-command", default="", help="Custom module command for framework=custom")
        p.add_argument("--display-name", default="", help="Display name in launcher")
        p.add_argument("--description", default="", help="Description")
        p.add_argument("--public", action="store_true", help="Mark app public")
        p.add_argument("--keep-alive", action="store_true", help="Keep app alive")
        p.add_argument("--env-json", default="", help="JSON object with environment vars")
        p.add_argument("--conda-env", default="", help="Optional conda environment name")
        p.add_argument("--profile", default="", help="Optional profile name")
        p.add_argument("--profile-image", default="", help="Optional image override")
        p.add_argument("--share-users", default="", help="Comma-separated users")
        p.add_argument("--share-groups", default="", help="Comma-separated groups")
        p.add_argument("--replace", action="store_true", help="Delete existing app with same name before deploy")
        p.add_argument("--wait-seconds", type=int, default=0, help="Wait for readiness/pending observation")
        p.add_argument("--user", default="", help="Target Hub username (defaults to current user)")
        return p

    if mode == "nebari_app_status":
        p.add_argument("--name", required=True, help="App name")
        p.add_argument("--json", action="store_true", help="Print raw server JSON")
        p.add_argument("--user", default="", help="Target Hub username (defaults to current user)")
        return p

    if mode == "nebari_app_logs":
        p.add_argument("--name", required=True, help="App name")
        p.add_argument("--lines", type=int, default=200, help="Number of lines to request")
        p.add_argument("--user", default="", help="Target Hub username (defaults to current user)")
        return p

    if mode == "nebari_app_stop":
        p.add_argument("--name", required=True, help="App name")
        p.add_argument("--wait-seconds", type=int, default=0, help="Wait for app to become stopped")
        p.add_argument("--user", default="", help="Target Hub username (defaults to current user)")
        return p

    if mode == "nebari_app_delete":
        p.add_argument("--name", required=True, help="App name")
        p.add_argument("--wait-seconds", type=int, default=0, help="Wait for app removal")
        p.add_argument("--user", default="", help="Target Hub username (defaults to current user)")
        return p

    if mode == "nebari_app_doctor":
        p.add_argument("--name", default="", help="Optional app name for app/log probes")
        p.add_argument("--json", action="store_true", help="Print JSON report")
        p.add_argument("--no-log-probe", action="store_true", help="Skip app temp-log endpoint probe")
        p.add_argument("--user", default="", help="Target Hub username (defaults to current user)")
        return p

    # Optional direct invocation: nebari_app_wrapper.py <command> ...
    p.add_argument(
        "command",
        choices=[
            "deploy",
            "status",
            "logs",
            "stop",
            "delete",
            "doctor",
        ],
        help="Command when running wrapper directly",
    )
    return p


def _dispatch(mode: str, argv: list[str]) -> int:
    if mode in {"nebari_app_wrapper.py", "nebari_app_wrapper"}:
        if not argv:
            raise CliError("usage: nebari_app_wrapper.py <deploy|status|logs|stop|delete|doctor> ...")
        sub = argv[0].strip().lower()
        mapped = {
            "deploy": "nebari_app_deploy",
            "status": "nebari_app_status",
            "logs": "nebari_app_logs",
            "stop": "nebari_app_stop",
            "delete": "nebari_app_delete",
            "doctor": "nebari_app_doctor",
        }.get(sub)
        if not mapped:
            raise CliError(f"Unknown command: {sub}")
        mode = mapped
        argv = argv[1:]

    parser = build_parser(mode)
    args = parser.parse_args(argv)

    if mode == "nebari_app_deploy":
        return cmd_deploy(args)
    if mode == "nebari_app_status":
        return cmd_status(args)
    if mode == "nebari_app_logs":
        return cmd_logs(args)
    if mode == "nebari_app_stop":
        return cmd_stop(args)
    if mode == "nebari_app_delete":
        return cmd_delete(args)
    if mode == "nebari_app_doctor":
        return cmd_doctor(args)

    raise CliError(f"Unsupported entrypoint mode: {mode}")


def main() -> int:
    mode = os.path.basename(sys.argv[0])
    try:
        return _dispatch(mode, sys.argv[1:])
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except requests.RequestException as exc:
        print(f"error: request failed: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
