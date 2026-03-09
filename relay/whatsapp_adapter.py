#!/usr/bin/env python3
"""
WhatsApp webhook adapter (Cloud API style).

- Endpoint: POST /whatsapp/webhook (configurable).
- Verifies request signature with app secret.
- Supports GET verification challenge for webhook setup.
- Normalizes inbound WhatsApp message payloads and forwards to relay.
- Returns fast acknowledgment (202 accepted).
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import hmac
import json
import os
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib import parse

from adapter_common import log_event, read_json_body, relay_post


@dataclass
class Config:
    host: str
    port: int
    service_name: str
    webhook_path: str
    verify_token: str
    app_secret: str
    relay_api_url: str
    relay_adapter_token: str
    relay_timeout_seconds: int
    workers: int


def load_config() -> Config:
    relay_token = os.environ.get("RELAY_ADAPTER_TOKEN", "").strip()
    if not relay_token:
        raise RuntimeError("RELAY_ADAPTER_TOKEN is required")

    app_secret = os.environ.get("WHATSAPP_APP_SECRET", "").strip()
    if not app_secret:
        raise RuntimeError("WHATSAPP_APP_SECRET is required")

    path = os.environ.get("WHATSAPP_WEBHOOK_PATH", "/whatsapp/webhook").strip() or "/whatsapp/webhook"
    if not path.startswith("/"):
        path = f"/{path}"

    return Config(
        host=os.environ.get("WHATSAPP_ADAPTER_HOST", "0.0.0.0").strip() or "0.0.0.0",
        port=int(os.environ.get("WHATSAPP_ADAPTER_PORT", "8792")),
        service_name=os.environ.get("WHATSAPP_ADAPTER_SERVICE_NAME", "m5-whatsapp-adapter").strip()
        or "m5-whatsapp-adapter",
        webhook_path=path,
        verify_token=os.environ.get("WHATSAPP_VERIFY_TOKEN", "").strip(),
        app_secret=app_secret,
        relay_api_url=os.environ.get(
            "RELAY_API_URL",
            f"http://pi-relay-core.{(os.environ.get('NEBARI_NAMESPACE', 'default').strip() or 'default')}.svc.cluster.local:8788/v1/messages",
        ).strip(),
        relay_adapter_token=relay_token,
        relay_timeout_seconds=int(os.environ.get("RELAY_TIMEOUT_SECONDS", "60")),
        workers=max(1, int(os.environ.get("WHATSAPP_ADAPTER_WORKERS", "4"))),
    )


CFG = load_config()
EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=CFG.workers)


def _is_signature_valid(raw_body: bytes, header_signature: str) -> bool:
    candidate = (header_signature or "").strip()
    if not candidate:
        return False
    if candidate.startswith("sha256="):
        candidate = candidate[7:]

    expected = hmac.new(CFG.app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(candidate, expected)


def _verify_request_signature(headers: Any, raw_body: bytes) -> tuple[bool, str]:
    sig_meta = str(headers.get("X-Hub-Signature-256") or "").strip()
    sig_alt = str(headers.get("X-WhatsApp-Signature") or "").strip()

    if sig_meta:
        if _is_signature_valid(raw_body, sig_meta):
            return True, "x-hub-signature-256"
        return False, "invalid X-Hub-Signature-256"

    if sig_alt:
        if _is_signature_valid(raw_body, sig_alt):
            return True, "x-whatsapp-signature"
        return False, "invalid X-WhatsApp-Signature"

    return False, "missing signature header"


def _extract_text_message_body(message: dict[str, Any]) -> str:
    message_type = str(message.get("type") or "").strip()
    if message_type == "text":
        text = str((message.get("text") or {}).get("body") or "").strip()
        return text

    if message_type == "button":
        return str((message.get("button") or {}).get("text") or "").strip()

    if message_type == "interactive":
        interactive = message.get("interactive") or {}
        button_reply = interactive.get("button_reply") or {}
        list_reply = interactive.get("list_reply") or {}
        title = str(button_reply.get("title") or list_reply.get("title") or "").strip()
        if title:
            return title

    if message_type:
        return f"<whatsapp:{message_type}>"

    return ""


def _extract_relay_payloads(payload: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    entries = payload.get("entry")
    if not isinstance(entries, list):
        return out

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        changes = entry.get("changes")
        if not isinstance(changes, list):
            continue
        for change in changes:
            if not isinstance(change, dict):
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue

            workspace_id = str((value.get("metadata") or {}).get("phone_number_id") or "").strip()
            messages = value.get("messages")
            if not isinstance(messages, list):
                continue

            for message in messages:
                if not isinstance(message, dict):
                    continue

                external_user = str(message.get("from") or "").strip()
                external_message_id = str(message.get("id") or "").strip()
                if not external_user or not external_message_id:
                    continue

                text = _extract_text_message_body(message)
                if not text:
                    continue

                context = message.get("context") if isinstance(message.get("context"), dict) else {}
                thread_id = str(context.get("id") or "").strip()
                message_type = str(message.get("type") or "unknown").strip() or "unknown"

                out.append(
                    {
                        "channel": "whatsapp",
                        "workspace_id": workspace_id,
                        "external_user": external_user,
                        "external_message_id": external_message_id,
                        "thread_id": thread_id,
                        "message": text,
                        "metadata": {
                            "whatsapp_message_type": message_type,
                            "phone_number_id": workspace_id,
                        },
                    }
                )

    return out


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


class WhatsAppAdapterHandler(BaseHTTPRequestHandler):
    server_version = "WhatsAppAdapter/0.1"

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _text(self, status: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        return

    def do_GET(self) -> None:  # noqa: N802
        path = self.path
        if path.rstrip("/") == "/healthz":
            self._json(200, {"ok": True, "service": CFG.service_name, "version": self.server_version})
            return

        parsed = parse.urlparse(path)
        if parsed.path.rstrip("/") != CFG.webhook_path.rstrip("/"):
            self._json(404, {"ok": False, "error": "not-found"})
            return

        params = parse.parse_qs(parsed.query)
        mode = str(params.get("hub.mode", [""])[0])
        token = str(params.get("hub.verify_token", [""])[0])
        challenge = str(params.get("hub.challenge", [""])[0])

        if mode == "subscribe" and CFG.verify_token and token == CFG.verify_token and challenge:
            self._text(200, challenge)
            return

        self._json(403, {"ok": False, "error": "verification-failed"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.rstrip("/")
        correlation_id = str(self.headers.get("X-Correlation-Id") or "").strip() or uuid.uuid4().hex[:16]

        if path != CFG.webhook_path.rstrip("/"):
            self._json(404, {"ok": False, "error": "not-found", "correlation_id": correlation_id})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length > 0 else b"{}"

            ok, signature_source = _verify_request_signature(self.headers, raw)
            if not ok:
                log_event(
                    CFG.service_name,
                    "warn",
                    "signature_invalid",
                    correlation_id,
                    reason=signature_source,
                )
                self._json(
                    401,
                    {
                        "ok": False,
                        "error": "invalid signature",
                        "reason": signature_source,
                        "correlation_id": correlation_id,
                    },
                )
                return

            payload = read_json_body(raw)
            relay_payloads = _extract_relay_payloads(payload)
            if not relay_payloads:
                self._json(
                    202,
                    {
                        "ok": True,
                        "accepted": True,
                        "ignored": True,
                        "correlation_id": correlation_id,
                    },
                )
                return

            for item in relay_payloads:
                EXECUTOR.submit(_forward_to_relay, item, correlation_id)

            self._json(
                202,
                {
                    "ok": True,
                    "accepted": True,
                    "correlation_id": correlation_id,
                    "count": len(relay_payloads),
                    "signature_source": signature_source,
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
    server = ThreadingHTTPServer((CFG.host, CFG.port), WhatsAppAdapterHandler)
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
