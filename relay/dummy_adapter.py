#!/usr/bin/env python3
"""
Dummy adapter for CLI/curl simulation.

- Endpoint: POST /simulate (configurable via DUMMY_ADAPTER_PATH)
- Endpoint: GET /simulate/status (configurable via DUMMY_ADAPTER_STATUS_PATH)
- Verifies adapter ingress token.
- Forwards normalized payload to relay API.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib import error, parse

from adapter_common import extract_bearer, log_event, read_json_body, relay_get, relay_post


@dataclass
class Config:
    host: str
    port: int
    simulate_path: str
    status_path: str
    service_name: str
    relay_api_url: str
    relay_status_url: str
    relay_adapter_token: str
    relay_timeout_seconds: int
    dummy_ingress_token: str


def load_config() -> Config:
    relay_token = os.environ.get("RELAY_ADAPTER_TOKEN", "").strip()
    if not relay_token:
        raise RuntimeError("RELAY_ADAPTER_TOKEN is required")

    ingress_token = os.environ.get("DUMMY_ADAPTER_INGRESS_TOKEN", "").strip()
    if not ingress_token:
        raise RuntimeError("DUMMY_ADAPTER_INGRESS_TOKEN is required")

    simulate_path = os.environ.get("DUMMY_ADAPTER_PATH", "/simulate").strip() or "/simulate"
    if not simulate_path.startswith("/"):
        simulate_path = f"/{simulate_path}"

    default_status_path = "/simulate/status"
    if simulate_path.endswith("/simulate"):
        default_status_path = f"{simulate_path[:-9]}/status"
    status_path = os.environ.get("DUMMY_ADAPTER_STATUS_PATH", default_status_path).strip() or default_status_path
    if not status_path.startswith("/"):
        status_path = f"/{status_path}"

    namespace = os.environ.get("NEBARI_NAMESPACE", "default").strip() or "default"
    relay_api_url = os.environ.get(
        "RELAY_API_URL",
        f"http://pi-relay-core.{namespace}.svc.cluster.local:8788/v1/messages",
    ).strip()
    relay_status_url = os.environ.get("RELAY_STATUS_URL", "").strip() or f"{relay_api_url}/status"

    return Config(
        host=os.environ.get("DUMMY_ADAPTER_HOST", "0.0.0.0").strip() or "0.0.0.0",
        port=int(os.environ.get("DUMMY_ADAPTER_PORT", "8790")),
        simulate_path=simulate_path,
        status_path=status_path,
        service_name=os.environ.get("DUMMY_ADAPTER_SERVICE_NAME", "m5-dummy-adapter").strip()
        or "m5-dummy-adapter",
        relay_api_url=relay_api_url,
        relay_status_url=relay_status_url,
        relay_adapter_token=relay_token,
        relay_timeout_seconds=int(os.environ.get("RELAY_TIMEOUT_SECONDS", "240")),
        dummy_ingress_token=ingress_token,
    )


CFG = load_config()


class DummyAdapterHandler(BaseHTTPRequestHandler):
    server_version = "DummyAdapter/0.1"

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

    def _auth(self, correlation_id: str) -> bool:
        token = extract_bearer(self.headers) or str(self.headers.get("X-Dummy-Token") or "").strip()
        if token == CFG.dummy_ingress_token:
            return True
        log_event(
            CFG.service_name,
            "warn",
            "ingress_auth_failed",
            correlation_id,
            remote=self.client_address[0] if self.client_address else None,
        )
        self._json(401, {"ok": False, "error": "unauthorized", "correlation_id": correlation_id})
        return False

    def do_GET(self) -> None:  # noqa: N802
        parsed = parse.urlparse(self.path)
        path = parsed.path.rstrip("/")
        correlation_id = str(self.headers.get("X-Correlation-Id") or "").strip() or uuid.uuid4().hex[:16]

        if path == "/healthz":
            self._json(200, {"ok": True, "service": CFG.service_name, "version": self.server_version})
            return

        if path != CFG.status_path.rstrip("/"):
            self._json(404, {"ok": False, "error": "not-found"})
            return

        if not self._auth(correlation_id):
            return

        params = parse.parse_qs(parsed.query)
        channel = str((params.get("channel") or ["dummy"])[0]).strip().lower() or "dummy"
        external_user = str((params.get("external_user") or [""])[0]).strip()
        external_message_id = str((params.get("external_message_id") or [""])[0]).strip()
        workspace_id = str((params.get("workspace_id") or params.get("team_id") or [""])[0]).strip()

        if not external_user or not external_message_id:
            self._json(
                400,
                {
                    "ok": False,
                    "error": "external_user and external_message_id are required",
                    "correlation_id": correlation_id,
                },
            )
            return

        relay_params = {
            "channel": channel,
            "external_user": external_user,
            "external_message_id": external_message_id,
        }
        if workspace_id:
            relay_params["workspace_id"] = workspace_id

        try:
            status, relay_response = relay_get(
                relay_api_url=CFG.relay_status_url,
                relay_adapter_token=CFG.relay_adapter_token,
                params=relay_params,
                correlation_id=correlation_id,
                timeout_seconds=CFG.relay_timeout_seconds,
            )
            wrapped = {
                "ok": status < 400,
                "adapter": CFG.service_name,
                "correlation_id": correlation_id,
                "relay_status": status,
                "relay_response": relay_response,
            }
            self._json(status, wrapped)
        except Exception as exc:  # noqa: BLE001
            log_event(CFG.service_name, "error", "status_failed", correlation_id, error=str(exc))
            self._json(500, {"ok": False, "error": str(exc), "correlation_id": correlation_id})

        return

    def do_POST(self) -> None:  # noqa: N802
        path = parse.urlparse(self.path).path.rstrip("/")
        correlation_id = str(self.headers.get("X-Correlation-Id") or "").strip() or uuid.uuid4().hex[:16]

        if path != CFG.simulate_path.rstrip("/"):
            self._json(404, {"ok": False, "error": "not-found", "correlation_id": correlation_id})
            return

        try:
            if not self._auth(correlation_id):
                return

            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length > 0 else b"{}"
            incoming = read_json_body(raw)

            external_user = str(incoming.get("external_user") or "").strip()
            external_message_id = str(incoming.get("external_message_id") or "").strip()
            message = str(incoming.get("message") or "").strip()
            if not external_user:
                raise ValueError("external_user is required")
            if not external_message_id:
                raise ValueError("external_message_id is required")
            if not message:
                raise ValueError("message is required")

            relay_payload = {
                "channel": str(incoming.get("channel") or "dummy").strip().lower() or "dummy",
                "workspace_id": str(incoming.get("workspace_id") or incoming.get("team_id") or "").strip(),
                "external_user": external_user,
                "external_message_id": external_message_id,
                "thread_id": str(incoming.get("thread_id") or "").strip(),
                "message": message,
                "model": str(incoming.get("model") or "").strip(),
                "profile": str(incoming.get("profile") or "").strip(),
                "session_key": str(incoming.get("session_key") or "").strip(),
                "metadata": incoming.get("metadata") if isinstance(incoming.get("metadata"), dict) else {},
            }

            status, relay_response = relay_post(
                relay_api_url=CFG.relay_api_url,
                relay_adapter_token=CFG.relay_adapter_token,
                payload=relay_payload,
                correlation_id=correlation_id,
                timeout_seconds=CFG.relay_timeout_seconds,
            )

            log_event(
                CFG.service_name,
                "info",
                "simulate_forwarded",
                correlation_id,
                relay_status=status,
                channel=relay_payload["channel"],
                external_user=external_user,
                external_message_id=external_message_id,
            )

            wrapped = {
                "ok": status < 400,
                "accepted": status in (200, 202),
                "adapter": CFG.service_name,
                "correlation_id": correlation_id,
                "relay_status": status,
                "relay_response": relay_response,
                "status_endpoint": CFG.status_path,
            }
            self._json(status, wrapped)
        except json.JSONDecodeError:
            self._json(
                400,
                {
                    "ok": False,
                    "error": "invalid JSON payload",
                    "correlation_id": correlation_id,
                },
            )
        except error.URLError as exc:
            # Relay may still be processing; return accepted and expose polling endpoint.
            log_event(
                CFG.service_name,
                "warn",
                "simulate_forward_timeout",
                correlation_id,
                error=str(exc),
            )
            self._json(
                202,
                {
                    "ok": True,
                    "accepted": True,
                    "adapter": CFG.service_name,
                    "correlation_id": correlation_id,
                    "relay_status": 202,
                    "relay_response": {
                        "ok": True,
                        "accepted": True,
                        "idempotency_status": "inflight",
                    },
                    "status_endpoint": CFG.status_path,
                },
            )
        except ValueError as exc:
            self._json(400, {"ok": False, "error": str(exc), "correlation_id": correlation_id})
        except Exception as exc:  # noqa: BLE001
            log_event(CFG.service_name, "error", "simulate_failed", correlation_id, error=str(exc))
            self._json(500, {"ok": False, "error": str(exc), "correlation_id": correlation_id})


def main() -> None:
    server = ThreadingHTTPServer((CFG.host, CFG.port), DummyAdapterHandler)
    log_event(
        CFG.service_name,
        "info",
        "service_started",
        "startup",
        listen=f"http://{CFG.host}:{CFG.port}",
        relay_api_url=CFG.relay_api_url,
        relay_status_url=CFG.relay_status_url,
        endpoint=CFG.simulate_path,
        status_endpoint=CFG.status_path,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
