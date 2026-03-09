#!/usr/bin/env python3
"""
Slack Events API adapter.

- Endpoint: POST /slack/events (configurable via SLACK_WEBHOOK_PATH)
- Verifies Slack signing secret.
- Normalizes inbound events and forwards to relay API.
- Returns fast acknowledgment (202 accepted).
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import hmac
import json
import os
import time
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from adapter_common import log_event, read_json_body, relay_post


@dataclass
class Config:
    host: str
    port: int
    service_name: str
    webhook_path: str
    signing_secret: str
    relay_api_url: str
    relay_adapter_token: str
    relay_timeout_seconds: int
    max_skew_seconds: int
    workers: int


def load_config() -> Config:
    signing_secret = os.environ.get("SLACK_SIGNING_SECRET", "").strip()
    if not signing_secret:
        raise RuntimeError("SLACK_SIGNING_SECRET is required")

    relay_token = os.environ.get("RELAY_ADAPTER_TOKEN", "").strip()
    if not relay_token:
        raise RuntimeError("RELAY_ADAPTER_TOKEN is required")

    path = os.environ.get("SLACK_WEBHOOK_PATH", "/slack/events").strip() or "/slack/events"
    if not path.startswith("/"):
        path = f"/{path}"

    return Config(
        host=os.environ.get("SLACK_ADAPTER_HOST", "0.0.0.0").strip() or "0.0.0.0",
        port=int(os.environ.get("SLACK_ADAPTER_PORT", "8791")),
        service_name=os.environ.get("SLACK_ADAPTER_SERVICE_NAME", "m5-slack-adapter").strip()
        or "m5-slack-adapter",
        webhook_path=path,
        signing_secret=signing_secret,
        relay_api_url=os.environ.get(
            "RELAY_API_URL",
            f"http://pi-relay-core.{(os.environ.get('NEBARI_NAMESPACE', 'default').strip() or 'default')}.svc.cluster.local:8788/v1/messages",
        ).strip(),
        relay_adapter_token=relay_token,
        relay_timeout_seconds=int(os.environ.get("RELAY_TIMEOUT_SECONDS", "60")),
        max_skew_seconds=int(os.environ.get("SLACK_SIGNATURE_MAX_SKEW_SECONDS", "300")),
        workers=max(1, int(os.environ.get("SLACK_ADAPTER_WORKERS", "4"))),
    )


CFG = load_config()
EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=CFG.workers)


def _verify_slack_signature(raw_body: bytes, timestamp: str, signature: str) -> tuple[bool, str]:
    if not timestamp:
        return False, "missing X-Slack-Request-Timestamp"
    if not signature:
        return False, "missing X-Slack-Signature"

    try:
        ts_value = int(timestamp)
    except ValueError:
        return False, "invalid timestamp"

    now = int(time.time())
    if abs(now - ts_value) > CFG.max_skew_seconds:
        return False, "timestamp outside allowed skew"

    base_string = f"v0:{timestamp}:{raw_body.decode('utf-8')}".encode("utf-8")
    expected = "v0=" + hmac.new(
        CFG.signing_secret.encode("utf-8"),
        base_string,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return False, "signature mismatch"

    return True, "ok"


def _extract_message_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    event = payload.get("event")
    if not isinstance(event, dict):
        return None

    event_type = str(event.get("type") or "").strip()
    subtype = str(event.get("subtype") or "").strip()
    if event_type not in ("message", "app_mention"):
        return None

    # Ignore common non-user message events.
    if subtype in ("message_deleted", "message_changed", "channel_join", "channel_leave"):
        return None

    external_user = str(event.get("user") or "").strip()
    text = str(event.get("text") or "").strip()
    if not external_user or not text:
        return None

    event_id = str(payload.get("event_id") or "").strip()
    event_ts = str(event.get("client_msg_id") or event.get("ts") or "").strip()
    external_message_id = event_id or event_ts
    if not external_message_id:
        return None

    team_id = str(payload.get("team_id") or "").strip()
    channel_id = str(event.get("channel") or "").strip()
    thread_ts = str(event.get("thread_ts") or "").strip()

    return {
        "channel": "slack",
        "workspace_id": team_id,
        "external_user": external_user,
        "external_message_id": external_message_id,
        "thread_id": thread_ts,
        "message": text,
        "metadata": {
            "slack_event_type": event_type,
            "slack_subtype": subtype,
            "slack_channel": channel_id,
            "slack_team": team_id,
        },
    }


def _forward_to_relay(relay_payload: dict[str, Any], correlation_id: str) -> None:
    status, response = relay_post(
        relay_api_url=CFG.relay_api_url,
        relay_adapter_token=CFG.relay_adapter_token,
        payload=relay_payload,
        correlation_id=correlation_id,
        timeout_seconds=CFG.relay_timeout_seconds,
    )
    log_event(
        CFG.service_name,
        "info",
        "relay_forward_result",
        correlation_id,
        relay_status=status,
        relay_ok=response.get("ok"),
        external_message_id=relay_payload.get("external_message_id"),
        external_user=relay_payload.get("external_user"),
    )


class SlackAdapterHandler(BaseHTTPRequestHandler):
    server_version = "SlackAdapter/0.1"

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _text(self, status: int, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") == "/healthz":
            self._json(200, {"ok": True, "service": CFG.service_name, "version": self.server_version})
            return
        self._json(404, {"ok": False, "error": "not-found"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.rstrip("/")
        correlation_id = str(self.headers.get("X-Correlation-Id") or "").strip() or uuid.uuid4().hex[:16]

        if path != CFG.webhook_path.rstrip("/"):
            self._json(404, {"ok": False, "error": "not-found", "correlation_id": correlation_id})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length > 0 else b"{}"

            timestamp = str(self.headers.get("X-Slack-Request-Timestamp") or "").strip()
            signature = str(self.headers.get("X-Slack-Signature") or "").strip()
            ok, reason = _verify_slack_signature(raw, timestamp, signature)
            if not ok:
                log_event(CFG.service_name, "warn", "signature_invalid", correlation_id, reason=reason)
                self._json(
                    401,
                    {"ok": False, "error": "invalid signature", "reason": reason, "correlation_id": correlation_id},
                )
                return

            payload = read_json_body(raw)

            if str(payload.get("type") or "") == "url_verification":
                challenge = str(payload.get("challenge") or "")
                self._text(200, challenge)
                return

            relay_payload = _extract_message_payload(payload)
            if relay_payload is None:
                self._json(200, {"ok": True, "ignored": True, "correlation_id": correlation_id})
                return

            EXECUTOR.submit(_forward_to_relay, relay_payload, correlation_id)
            self._json(
                202,
                {
                    "ok": True,
                    "accepted": True,
                    "correlation_id": correlation_id,
                    "external_message_id": relay_payload["external_message_id"],
                },
            )
        except ValueError as exc:
            self._json(400, {"ok": False, "error": str(exc), "correlation_id": correlation_id})
        except json.JSONDecodeError:
            self._json(400, {"ok": False, "error": "invalid JSON payload", "correlation_id": correlation_id})
        except Exception as exc:  # noqa: BLE001
            log_event(CFG.service_name, "error", "request_failed", correlation_id, error=str(exc))
            self._json(500, {"ok": False, "error": str(exc), "correlation_id": correlation_id})


def main() -> None:
    server = ThreadingHTTPServer((CFG.host, CFG.port), SlackAdapterHandler)
    log_event(
        CFG.service_name,
        "info",
        "service_started",
        "startup",
        listen=f"http://{CFG.host}:{CFG.port}",
        webhook_path=CFG.webhook_path,
        relay_api_url=CFG.relay_api_url,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
