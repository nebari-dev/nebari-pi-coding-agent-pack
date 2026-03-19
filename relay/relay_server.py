#!/usr/bin/env python3
"""
M5 relay service for second-attempt deployment.

Phase A hardening:
- Mandatory adapter auth token.
- Strict sender -> Nebari user mapping (no implicit fallback).
- Idempotency keys to prevent duplicate execution.
- Structured JSON logs with correlation IDs.
- Versioned API endpoint: POST /v1/messages.

Backward compatibility:
- POST /webhook is retained as an alias to /v1/messages.
"""

from __future__ import annotations

import copy
import json
import os
import shlex
import sqlite3
import ssl
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib import error, parse, request

DEFAULT_BASE_URL = (
    "https://aa3cf11daa553482aac6aa272a7b9d4e-1077346153.us-east-1.elb.amazonaws.com"
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _coerce_bool(raw: str, default: bool = False) -> bool:
    text = (raw or "").strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return default


def log_event(level: str, event: str, correlation_id: str, **fields: Any) -> None:
    payload = {
        "ts": _now_iso(),
        "level": level,
        "event": event,
        "correlation_id": correlation_id,
    }
    payload.update(fields)
    print(json.dumps(payload, sort_keys=True), flush=True)


def _extract_bearer(headers: Any) -> str:
    auth = str(headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return str(headers.get("X-Relay-Token") or "").strip()


def _load_user_map(raw: str) -> dict[str, str]:
    if not raw.strip():
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("MESSAGE_USER_MAP must be a JSON object")
    result: dict[str, str] = {}
    for key, value in data.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError("MESSAGE_USER_MAP keys and values must be strings")
        clean_key = key.strip()
        clean_value = value.strip()
        if clean_key and clean_value:
            result[clean_key] = clean_value
    return result


def _sanitize_component(raw: str, fallback: str) -> str:
    cleaned: list[str] = []
    for ch in raw:
        if ch.isalnum() or ch in ("-", "_", "."):
            cleaned.append(ch)
        else:
            cleaned.append("-")
    value = "".join(cleaned).strip("-")
    if not value:
        return fallback
    return value[:80]


def _default_session_key(channel: str, workspace_id: str, external_user: str, thread_id: str) -> str:
    parts = [
        _sanitize_component(channel, "channel"),
        _sanitize_component(workspace_id or "global", "global"),
        _sanitize_component(external_user, "sender"),
        _sanitize_component(thread_id or "main", "main"),
    ]
    return "-".join(parts)


@dataclass
class Config:
    namespace: str
    server_name: str
    hub_api_url: str
    hub_api_token: str
    default_profile: str
    skill_dir: str
    pi_agent_dir: str
    insecure_tls: bool
    kubectl_bin: str
    kubeconfig: str
    user_map: dict[str, str]
    pi_timeout_seconds: int
    hub_timeout_seconds: int
    relay_adapter_token: str
    allow_explicit_nebari_user: bool
    idempotency_ttl_seconds: int
    idempotency_max_entries: int
    idempotency_backend: str
    idempotency_sqlite_path: str
    async_queue_enabled: bool
    queue_workers: int
    service_name: str


def load_config() -> Config:
    namespace = os.environ.get("NEBARI_NAMESPACE", "default").strip() or "default"
    hub_api_url = (os.environ.get("NEBARI_HUB_API_URL") or "").strip()
    if not hub_api_url:
        if os.environ.get("KUBERNETES_SERVICE_HOST"):
            hub_api_url = f"http://hub.{namespace}.svc.cluster.local:8081/hub/api"
        else:
            hub_api_url = f"{DEFAULT_BASE_URL}/hub/api"
    hub_api_url = hub_api_url.rstrip("/")
    hub_api_token = os.environ.get("NEBARI_HUB_API_TOKEN", "").strip()
    if not hub_api_token:
        raise RuntimeError("NEBARI_HUB_API_TOKEN is required")

    relay_adapter_token = os.environ.get("RELAY_ADAPTER_TOKEN", "").strip()
    if not relay_adapter_token:
        raise RuntimeError("RELAY_ADAPTER_TOKEN is required")

    kubeconfig = (os.environ.get("KUBECONFIG") or "").strip()

    user_map = _load_user_map(os.environ.get("MESSAGE_USER_MAP", "{}"))

    pi_agent_dir = os.environ.get("NEBARI_PI_AGENT_DIR", "/tmp/pi-agent").strip() or "/tmp/pi-agent"
    if not pi_agent_dir.startswith("/"):
        raise RuntimeError("NEBARI_PI_AGENT_DIR must be an absolute path")

    return Config(
        namespace=namespace,
        server_name=os.environ.get("NEBARI_PI_SERVER_NAME", "pi").strip(),
        hub_api_url=hub_api_url,
        hub_api_token=hub_api_token,
        default_profile=os.environ.get("NEBARI_PI_PROFILE", "pi-small").strip(),
        skill_dir=os.environ.get("NEBARI_SHARED_SKILLS_DIR", "/opt/nebari/baked-skills/shared-skills").strip(),
        pi_agent_dir=pi_agent_dir,
        insecure_tls=_coerce_bool(os.environ.get("NEBARI_INSECURE_TLS", "1"), default=True),
        kubectl_bin=os.environ.get("KUBECTL_BIN", "kubectl").strip(),
        kubeconfig=kubeconfig,
        user_map=user_map,
        pi_timeout_seconds=int(os.environ.get("PI_MESSAGE_TIMEOUT_SECONDS", "240")),
        hub_timeout_seconds=int(os.environ.get("HUB_START_TIMEOUT_SECONDS", "300")),
        relay_adapter_token=relay_adapter_token,
        allow_explicit_nebari_user=_coerce_bool(
            os.environ.get("RELAY_ALLOW_EXPLICIT_NEBARI_USER", "0"),
            default=False,
        ),
        idempotency_ttl_seconds=int(os.environ.get("RELAY_IDEMPOTENCY_TTL_SECONDS", "7200")),
        idempotency_max_entries=int(os.environ.get("RELAY_IDEMPOTENCY_MAX_ENTRIES", "10000")),
        idempotency_backend=(os.environ.get("RELAY_IDEMPOTENCY_BACKEND", "memory").strip() or "memory").lower(),
        idempotency_sqlite_path=os.environ.get("RELAY_IDEMPOTENCY_SQLITE_PATH", "/tmp/relay-idempotency.db").strip() or "/tmp/relay-idempotency.db",
        async_queue_enabled=_coerce_bool(os.environ.get("RELAY_ASYNC_QUEUE_ENABLED", "0"), default=False),
        queue_workers=max(1, int(os.environ.get("RELAY_QUEUE_WORKERS", "2"))),
        service_name=os.environ.get("RELAY_SERVICE_NAME", "m5-message-relay").strip() or "m5-message-relay",
    )


CFG = load_config()


class IdempotencyStore:
    """In-memory idempotency tracking with TTL and in-flight coordination."""

    def __init__(self, ttl_seconds: int, max_entries: int):
        self._ttl = max(ttl_seconds, 1)
        self._max_entries = max(max_entries, 100)
        self._lock = threading.Lock()
        self._done: dict[str, tuple[float, dict[str, Any]]] = {}
        self._inflight: set[str] = set()

    def _cleanup(self, now: float) -> None:
        expired = [key for key, (ts, _) in self._done.items() if now - ts > self._ttl]
        for key in expired:
            self._done.pop(key, None)

        if len(self._done) <= self._max_entries:
            return

        ordered = sorted(self._done.items(), key=lambda item: item[1][0])
        overflow = len(ordered) - self._max_entries
        for idx in range(overflow):
            self._done.pop(ordered[idx][0], None)

    def claim(self, key: str) -> tuple[str, dict[str, Any] | None]:
        now = time.time()
        with self._lock:
            self._cleanup(now)
            if key in self._done:
                return "done", copy.deepcopy(self._done[key][1])
            if key in self._inflight:
                return "inflight", None
            self._inflight.add(key)
            return "claimed", None

    def complete(self, key: str, response: dict[str, Any], *, cache: bool = True) -> None:
        now = time.time()
        with self._lock:
            self._inflight.discard(key)
            if cache:
                self._done[key] = (now, copy.deepcopy(response))
                self._cleanup(now)

    def fail(self, key: str) -> None:
        with self._lock:
            self._inflight.discard(key)

    def status(self, key: str) -> tuple[str, dict[str, Any] | None]:
        now = time.time()
        with self._lock:
            self._cleanup(now)
            if key in self._done:
                return "done", copy.deepcopy(self._done[key][1])
            if key in self._inflight:
                return "inflight", None
            return "missing", None


class SQLiteIdempotencyStore:
    """SQLite-backed idempotency tracking with TTL and in-flight coordination."""

    def __init__(self, ttl_seconds: int, max_entries: int, db_path: str):
        self._ttl = max(ttl_seconds, 1)
        self._max_entries = max(max_entries, 100)
        self._db_path = db_path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("CREATE TABLE IF NOT EXISTS done (k TEXT PRIMARY KEY, ts REAL NOT NULL, payload TEXT NOT NULL)")
        self._conn.execute("CREATE TABLE IF NOT EXISTS inflight (k TEXT PRIMARY KEY, ts REAL NOT NULL)")
        self._conn.commit()

    def _cleanup(self, now: float) -> None:
        cutoff = now - self._ttl
        self._conn.execute("DELETE FROM done WHERE ts < ?", (cutoff,))
        self._conn.execute("DELETE FROM inflight WHERE ts < ?", (cutoff,))

        row = self._conn.execute("SELECT COUNT(*) FROM done").fetchone()
        count = int(row[0] if row else 0)
        if count > self._max_entries:
            overflow = count - self._max_entries
            self._conn.execute(
                "DELETE FROM done WHERE k IN (SELECT k FROM done ORDER BY ts ASC LIMIT ?)",
                (overflow,),
            )
        self._conn.commit()

    def claim(self, key: str) -> tuple[str, dict[str, Any] | None]:
        now = time.time()
        with self._lock:
            self._cleanup(now)
            row = self._conn.execute("SELECT payload FROM done WHERE k = ?", (key,)).fetchone()
            if row:
                try:
                    return "done", json.loads(row[0])
                except json.JSONDecodeError:
                    return "done", {"ok": False, "error": "invalid cached payload"}
            row = self._conn.execute("SELECT 1 FROM inflight WHERE k = ?", (key,)).fetchone()
            if row:
                return "inflight", None
            self._conn.execute("INSERT OR REPLACE INTO inflight(k, ts) VALUES (?, ?)", (key, now))
            self._conn.commit()
            return "claimed", None

    def complete(self, key: str, response: dict[str, Any], *, cache: bool = True) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute("DELETE FROM inflight WHERE k = ?", (key,))
            if cache:
                self._conn.execute(
                    "INSERT OR REPLACE INTO done(k, ts, payload) VALUES (?, ?, ?)",
                    (key, now, json.dumps(response, sort_keys=True)),
                )
            self._cleanup(now)

    def fail(self, key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM inflight WHERE k = ?", (key,))
            self._conn.commit()

    def status(self, key: str) -> tuple[str, dict[str, Any] | None]:
        now = time.time()
        with self._lock:
            self._cleanup(now)
            row = self._conn.execute("SELECT payload FROM done WHERE k = ?", (key,)).fetchone()
            if row:
                try:
                    return "done", json.loads(row[0])
                except json.JSONDecodeError:
                    return "done", {"ok": False, "error": "invalid cached payload"}
            row = self._conn.execute("SELECT 1 FROM inflight WHERE k = ?", (key,)).fetchone()
            if row:
                return "inflight", None
            return "missing", None


class RelayMetrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.requests_total = 0
        self.requests_failed = 0
        self.requests_completed = 0
        self.replays_total = 0
        self.inflight_total = 0
        self.queue_submitted = 0
        self.queue_errors = 0
        self.last_latency_ms = 0

    def incr(self, field: str, amount: int = 1) -> None:
        with self._lock:
            setattr(self, field, int(getattr(self, field, 0)) + amount)

    def set_latency(self, latency_ms: int) -> None:
        with self._lock:
            self.last_latency_ms = max(0, int(latency_ms))

    def render_prometheus(self) -> str:
        with self._lock:
            lines = [
                f"relay_requests_total {self.requests_total}",
                f"relay_requests_completed_total {self.requests_completed}",
                f"relay_requests_failed_total {self.requests_failed}",
                f"relay_idempotency_replays_total {self.replays_total}",
                f"relay_idempotency_inflight_total {self.inflight_total}",
                f"relay_queue_submitted_total {self.queue_submitted}",
                f"relay_queue_errors_total {self.queue_errors}",
                f"relay_last_latency_ms {self.last_latency_ms}",
            ]
        return "\n".join(lines) + "\n"


if CFG.idempotency_backend == "sqlite":
    IDEMPOTENCY = SQLiteIdempotencyStore(
        ttl_seconds=CFG.idempotency_ttl_seconds,
        max_entries=CFG.idempotency_max_entries,
        db_path=CFG.idempotency_sqlite_path,
    )
else:
    IDEMPOTENCY = IdempotencyStore(
        ttl_seconds=CFG.idempotency_ttl_seconds,
        max_entries=CFG.idempotency_max_entries,
    )

METRICS = RelayMetrics()
WORKERS = ThreadPoolExecutor(max_workers=CFG.queue_workers)


def _ssl_context():
    if CFG.insecure_tls:
        return ssl._create_unverified_context()  # noqa: S323 - demo deployment may be self-signed
    return None


def hub_request(method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
    url = f"{CFG.hub_api_url}{path}"
    payload = None if body is None else json.dumps(body).encode("utf-8")
    req = request.Request(url=url, method=method, data=payload)
    req.add_header("Authorization", f"token {CFG.hub_api_token}")
    req.add_header("Accept", "application/json")
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with request.urlopen(req, timeout=60, context=_ssl_context()) as resp:
            text = resp.read().decode("utf-8")
            return resp.status, json.loads(text) if text else {}
    except error.HTTPError as exc:
        body_text = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(body_text) if body_text else {}
        except json.JSONDecodeError:
            return exc.code, {"text": body_text}


def get_user(username: str) -> dict[str, Any]:
    query = parse.urlencode({"include_stopped_servers": "true"})
    status, data = hub_request("GET", f"/users/{parse.quote(username)}?{query}")
    if status >= 400:
        raise RuntimeError(f"Hub user lookup failed: status={status} body={data}")
    if not isinstance(data, dict):
        raise RuntimeError(f"Hub user lookup returned non-object: {data!r}")
    return data


def get_server(username: str) -> dict[str, Any] | None:
    user = get_user(username)
    servers = user.get("servers") or {}
    if not isinstance(servers, dict):
        return None
    server = servers.get(CFG.server_name)
    return server if isinstance(server, dict) else None


def ensure_server_running(username: str, profile: str) -> dict[str, Any]:
    existing = get_server(username)
    if existing and existing.get("ready"):
        return {"action": "already-running", "server": existing}

    if not existing:
        status, body = hub_request(
            "POST",
            f"/users/{parse.quote(username)}/servers/{parse.quote(CFG.server_name)}",
            {"profile": profile},
        )
        if status not in (201, 202, 400, 409):
            raise RuntimeError(f"Failed to start Pi server: status={status} body={body}")

    deadline = time.time() + CFG.hub_timeout_seconds
    while time.time() < deadline:
        latest = get_server(username)
        if latest and latest.get("ready"):
            return {"action": "started", "server": latest}
        time.sleep(2)

    raise RuntimeError(f"Timed out waiting for {username}/{CFG.server_name} to become ready")


def _kubectl(*args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if CFG.kubeconfig:
        env["KUBECONFIG"] = CFG.kubeconfig
    else:
        env.pop("KUBECONFIG", None)
    return subprocess.run(
        [CFG.kubectl_bin, *args],
        check=True,
        text=True,
        capture_output=True,
        env=env,
        timeout=timeout,
    )


def get_pi_pod(username: str) -> str:
    selector = ",".join(
        [
            "component=singleuser-server",
            f"hub.jupyter.org/username={username}",
            f"hub.jupyter.org/servername={CFG.server_name}",
        ]
    )
    proc = _kubectl(
        "-n",
        CFG.namespace,
        "get",
        "pod",
        "-l",
        selector,
        "-o",
        "json",
        timeout=60,
    )
    data = json.loads(proc.stdout or "{}")
    items = data.get("items") or []
    if not items:
        raise RuntimeError(f"No running pod found for {username}/{CFG.server_name}")
    running = [item for item in items if item.get("status", {}).get("phase") == "Running"]
    target = running[0] if running else items[0]
    name = target.get("metadata", {}).get("name")
    if not isinstance(name, str) or not name:
        raise RuntimeError("Unable to determine pod name from kubectl output")
    return name


def run_pi_prompt(
    pod: str,
    message: str,
    *,
    session_key: str,
    model: str = "",
) -> dict[str, Any]:
    model = (model or "").strip()
    quoted_message = shlex.quote(message)
    quoted_skill = shlex.quote(CFG.skill_dir)
    quoted_agent_dir = shlex.quote(CFG.pi_agent_dir)
    quoted_session = shlex.quote(f"{CFG.pi_agent_dir}/sessions/relay/{session_key}.jsonl")
    model_part = f"--model {shlex.quote(model)} " if model else ""
    shell_cmd = (
        f"AGENT_DIR={quoted_agent_dir}; "
        'mkdir -p "$AGENT_DIR/sessions/relay" && '
        f"pi -p --session {quoted_session} --skill {quoted_skill} {model_part}-- {quoted_message}"
    )
    proc = _kubectl(
        "-n",
        CFG.namespace,
        "exec",
        pod,
        "--",
        "sh",
        "-lc",
        shell_cmd,
        timeout=CFG.pi_timeout_seconds,
    )
    return {
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
    }


def _validate_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")

    channel = str(payload.get("channel") or "").strip().lower()
    if not channel:
        raise ValueError("channel is required")

    external_user = str(payload.get("external_user") or "").strip()
    if not external_user:
        raise ValueError("external_user is required")

    external_message_id = str(payload.get("external_message_id") or "").strip()
    if not external_message_id:
        raise ValueError("external_message_id is required")

    message = str(payload.get("message") or "").strip()
    if not message:
        raise ValueError("message is required")

    workspace_id = str(payload.get("workspace_id") or payload.get("team_id") or "").strip()
    thread_id = str(payload.get("thread_id") or "").strip()

    session_key = str(payload.get("session_key") or "").strip()
    if not session_key:
        session_key = _default_session_key(channel, workspace_id, external_user, thread_id)
    else:
        session_key = _sanitize_component(session_key, "session")

    return {
        "channel": channel,
        "external_user": external_user,
        "external_message_id": external_message_id,
        "message": message,
        "workspace_id": workspace_id,
        "thread_id": thread_id,
        "profile": str(payload.get("profile") or CFG.default_profile).strip(),
        "model": str(payload.get("model") or "").strip(),
        "session_key": session_key,
        "nebari_user": str(payload.get("nebari_user") or "").strip(),
        "metadata": payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
    }


def _idempotency_key(payload: dict[str, Any]) -> str:
    return "|".join(
        [
            payload["channel"],
            payload.get("workspace_id") or "global",
            payload["external_user"],
            payload["external_message_id"],
        ]
    )


def _idempotency_key_from_parts(
    channel: str,
    workspace_id: str,
    external_user: str,
    external_message_id: str,
) -> str:
    return "|".join(
        [
            channel.strip().lower(),
            workspace_id.strip() or "global",
            external_user.strip(),
            external_message_id.strip(),
        ]
    )


def resolve_user(payload: dict[str, Any]) -> tuple[str, str]:
    explicit = str(payload.get("nebari_user") or "").strip()
    if explicit:
        if not CFG.allow_explicit_nebari_user:
            raise PermissionError("nebari_user override is disabled")
        return explicit, "explicit"

    channel = payload["channel"]
    external_user = payload["external_user"]
    workspace_id = str(payload.get("workspace_id") or "").strip()

    candidate_keys = []
    if workspace_id:
        candidate_keys.append(f"{channel}:{workspace_id}:{external_user}")
    candidate_keys.append(f"{channel}:{external_user}")
    candidate_keys.append(external_user)

    for key in candidate_keys:
        mapped = CFG.user_map.get(key)
        if mapped:
            return mapped, key

    raise PermissionError(
        "sender mapping not found; add MESSAGE_USER_MAP entry for "
        f"{channel}:{external_user} (or scoped key with workspace_id)"
    )


def _process_and_cache(
    *,
    payload: dict[str, Any],
    run_id: str,
    correlation_id: str,
    idem_key: str,
    nebari_user: str,
    mapping_key: str,
    start_time: float,
) -> dict[str, Any]:
    start_state = ensure_server_running(nebari_user, payload["profile"])
    pod = get_pi_pod(nebari_user)
    pi_result = run_pi_prompt(
        pod,
        payload["message"],
        session_key=payload["session_key"],
        model=payload["model"],
    )

    response_payload = {
        "ok": True,
        "run_id": run_id,
        "correlation_id": correlation_id,
        "nebari_user": nebari_user,
        "mapping_key": mapping_key,
        "channel": payload["channel"],
        "workspace_id": payload.get("workspace_id") or None,
        "external_user": payload["external_user"],
        "external_message_id": payload["external_message_id"],
        "thread_id": payload.get("thread_id") or None,
        "profile": payload["profile"],
        "pod": pod,
        "session_key": payload["session_key"],
        "server_state": start_state,
        "pi_stdout": pi_result["stdout"],
        "pi_stderr": pi_result["stderr"],
        "idempotent_replay": False,
    }

    IDEMPOTENCY.complete(idem_key, response_payload, cache=True)

    elapsed_ms = int((time.time() - start_time) * 1000)
    METRICS.incr("requests_completed", 1)
    METRICS.set_latency(elapsed_ms)
    log_event(
        "info",
        "request_completed",
        correlation_id,
        run_id=run_id,
        idempotency_key=idem_key,
        elapsed_ms=elapsed_ms,
        mapped_user=nebari_user,
        pod=pod,
    )

    return response_payload


class RelayHandler(BaseHTTPRequestHandler):
    server_version = "NebariRelay/0.2"

    def _json(self, status: int, payload: dict[str, Any]) -> bool:
        body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return True
        except (BrokenPipeError, ConnectionResetError):
            return False

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        return

    def do_GET(self) -> None:  # noqa: N802
        parsed = parse.urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path == "/healthz":
            self._json(
                200,
                {
                    "ok": True,
                    "service": CFG.service_name,
                    "version": self.server_version,
                },
            )
            return

        if path == "/metrics":
            body = METRICS.render_prometheus().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path not in ("/v1/messages/status", "/webhook/status"):
            self._json(404, {"ok": False, "error": "not-found"})
            return

        correlation_id = (
            str(self.headers.get("X-Correlation-Id") or "").strip() or uuid.uuid4().hex[:16]
        )
        provided_token = _extract_bearer(self.headers)
        if provided_token != CFG.relay_adapter_token:
            log_event(
                "warn",
                "auth_failed",
                correlation_id,
                path=path,
                remote=self.client_address[0] if self.client_address else None,
            )
            self._json(401, {"ok": False, "error": "unauthorized", "correlation_id": correlation_id})
            return

        params = parse.parse_qs(parsed.query)
        channel = str((params.get("channel") or [""])[0]).strip().lower()
        external_user = str((params.get("external_user") or [""])[0]).strip()
        external_message_id = str((params.get("external_message_id") or [""])[0]).strip()
        workspace_id = str((params.get("workspace_id") or params.get("team_id") or [""])[0]).strip()

        if not channel or not external_user or not external_message_id:
            self._json(
                400,
                {
                    "ok": False,
                    "error": "channel, external_user, external_message_id are required",
                    "correlation_id": correlation_id,
                },
            )
            return

        idem_key = _idempotency_key_from_parts(
            channel=channel,
            workspace_id=workspace_id,
            external_user=external_user,
            external_message_id=external_message_id,
        )
        state, cached = IDEMPOTENCY.status(idem_key)
        payload: dict[str, Any] = {
            "ok": True,
            "correlation_id": correlation_id,
            "idempotency_key": idem_key,
            "idempotency_status": state,
            "channel": channel,
            "workspace_id": workspace_id or None,
            "external_user": external_user,
            "external_message_id": external_message_id,
        }
        if state == "done" and cached is not None:
            payload["result"] = cached

        self._json(200, payload)
        return

    def do_POST(self) -> None:  # noqa: N802
        path = parse.urlparse(self.path).path.rstrip("/")
        if path not in ("/v1/messages", "/webhook"):
            self._json(404, {"ok": False, "error": "not-found"})
            return

        correlation_id = (
            str(self.headers.get("X-Correlation-Id") or "").strip() or uuid.uuid4().hex[:16]
        )
        start_time = time.time()
        idem_key: str | None = None
        METRICS.incr("requests_total", 1)

        try:
            provided_token = _extract_bearer(self.headers)
            if provided_token != CFG.relay_adapter_token:
                METRICS.incr("requests_failed", 1)
                log_event(
                    "warn",
                    "auth_failed",
                    correlation_id,
                    path=path,
                    remote=self.client_address[0] if self.client_address else None,
                )
                self._json(401, {"ok": False, "error": "unauthorized", "correlation_id": correlation_id})
                return

            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length > 0 else b"{}"
            incoming = json.loads(raw.decode("utf-8"))

            payload = _validate_payload(incoming)
            idem_key = _idempotency_key(payload)

            state, cached = IDEMPOTENCY.claim(idem_key)
            if state == "done" and cached is not None:
                METRICS.incr("replays_total", 1)
                cached["idempotent_replay"] = True
                cached["correlation_id"] = correlation_id
                log_event(
                    "info",
                    "idempotency_replay",
                    correlation_id,
                    idempotency_key=idem_key,
                    channel=payload["channel"],
                    external_user=payload["external_user"],
                )
                self._json(200, cached)
                return

            if state == "inflight":
                METRICS.incr("inflight_total", 1)
                log_event(
                    "info",
                    "idempotency_inflight",
                    correlation_id,
                    idempotency_key=idem_key,
                    channel=payload["channel"],
                    external_user=payload["external_user"],
                )
                self._json(
                    202,
                    {
                        "ok": True,
                        "accepted": True,
                        "idempotency_status": "inflight",
                        "correlation_id": correlation_id,
                    },
                )
                return

            run_id = uuid.uuid4().hex
            try:
                nebari_user, mapping_key = resolve_user(payload)
            except PermissionError as exc:
                IDEMPOTENCY.fail(idem_key)
                METRICS.incr("requests_failed", 1)
                log_event(
                    "warn",
                    "mapping_denied",
                    correlation_id,
                    idempotency_key=idem_key,
                    channel=payload["channel"],
                    external_user=payload["external_user"],
                    error=str(exc),
                )
                self._json(
                    403,
                    {
                        "ok": False,
                        "error": str(exc),
                        "run_id": run_id,
                        "correlation_id": correlation_id,
                    },
                )
                return

            log_event(
                "info",
                "request_started",
                correlation_id,
                run_id=run_id,
                path=path,
                idempotency_key=idem_key,
                channel=payload["channel"],
                workspace_id=payload.get("workspace_id") or None,
                external_user=payload["external_user"],
                mapped_user=nebari_user,
                mapping_key=mapping_key,
            )

            if CFG.async_queue_enabled:
                METRICS.incr("queue_submitted", 1)

                def _worker() -> None:
                    try:
                        _process_and_cache(
                            payload=payload,
                            run_id=run_id,
                            correlation_id=correlation_id,
                            idem_key=idem_key,
                            nebari_user=nebari_user,
                            mapping_key=mapping_key,
                            start_time=start_time,
                        )
                    except Exception as exc:  # noqa: BLE001
                        METRICS.incr("requests_failed", 1)
                        error_payload = {
                            "ok": False,
                            "run_id": run_id,
                            "correlation_id": correlation_id,
                            "idempotency_key": idem_key,
                            "error": str(exc),
                        }
                        IDEMPOTENCY.complete(idem_key, error_payload, cache=True)
                        log_event(
                            "error",
                            "request_failed_async",
                            correlation_id,
                            run_id=run_id,
                            idempotency_key=idem_key,
                            error=str(exc),
                            path=path,
                        )

                try:
                    WORKERS.submit(_worker)
                except Exception as exc:  # noqa: BLE001
                    METRICS.incr("queue_errors", 1)
                    IDEMPOTENCY.fail(idem_key)
                    self._json(
                        503,
                        {
                            "ok": False,
                            "error": f"queue submit failed: {exc}",
                            "run_id": run_id,
                            "correlation_id": correlation_id,
                        },
                    )
                    return

                self._json(
                    202,
                    {
                        "ok": True,
                        "accepted": True,
                        "queued": True,
                        "run_id": run_id,
                        "idempotency_key": idem_key,
                        "correlation_id": correlation_id,
                    },
                )
                return

            response_payload = _process_and_cache(
                payload=payload,
                run_id=run_id,
                correlation_id=correlation_id,
                idem_key=idem_key,
                nebari_user=nebari_user,
                mapping_key=mapping_key,
                start_time=start_time,
            )

            sent = self._json(200, response_payload)
            if not sent:
                log_event(
                    "info",
                    "response_disconnected",
                    correlation_id,
                    run_id=run_id,
                    idempotency_key=idem_key,
                )
        except json.JSONDecodeError as exc:
            if idem_key:
                IDEMPOTENCY.fail(idem_key)
            METRICS.incr("requests_failed", 1)
            log_event("warn", "bad_json", correlation_id, error=str(exc), path=path)
            self._json(
                400,
                {
                    "ok": False,
                    "error": "invalid JSON payload",
                    "correlation_id": correlation_id,
                },
            )
        except ValueError as exc:
            if idem_key:
                IDEMPOTENCY.fail(idem_key)
            METRICS.incr("requests_failed", 1)
            log_event("warn", "bad_request", correlation_id, error=str(exc), path=path)
            self._json(
                400,
                {
                    "ok": False,
                    "error": str(exc),
                    "correlation_id": correlation_id,
                },
            )
        except Exception as exc:  # noqa: BLE001
            if idem_key:
                IDEMPOTENCY.fail(idem_key)
            METRICS.incr("requests_failed", 1)
            log_event("error", "request_failed", correlation_id, error=str(exc), path=path)
            self._json(
                500,
                {
                    "ok": False,
                    "error": str(exc),
                    "correlation_id": correlation_id,
                },
            )


def main() -> None:
    host = os.environ.get("RELAY_HOST", "0.0.0.0")
    port = int(os.environ.get("RELAY_PORT", "8788"))
    server = ThreadingHTTPServer((host, port), RelayHandler)
    log_event(
        "info",
        "service_started",
        correlation_id="startup",
        service=CFG.service_name,
        listen=f"http://{host}:{port}",
        namespace=CFG.namespace,
        server_name=CFG.server_name,
        endpoints=["/healthz", "/metrics", "/v1/messages", "/webhook", "/v1/messages/status", "/webhook/status"],
        strict_mapping=True,
        explicit_user_override=CFG.allow_explicit_nebari_user,
        async_queue_enabled=CFG.async_queue_enabled,
        queue_workers=CFG.queue_workers,
        idempotency_backend=CFG.idempotency_backend,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
