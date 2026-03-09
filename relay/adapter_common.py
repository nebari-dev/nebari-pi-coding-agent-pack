#!/usr/bin/env python3
"""Shared helpers for relay adapter services."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from urllib import parse
from urllib import error, request


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def log_event(service: str, level: str, event: str, correlation_id: str, **fields: Any) -> None:
    payload = {
        "ts": now_iso(),
        "service": service,
        "level": level,
        "event": event,
        "correlation_id": correlation_id,
    }
    payload.update(fields)
    print(json.dumps(payload, sort_keys=True), flush=True)


def extract_bearer(headers: Any) -> str:
    auth = str(headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def read_json_body(raw: bytes) -> dict[str, Any]:
    parsed = json.loads(raw.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("payload must be a JSON object")
    return parsed


def relay_post(
    *,
    relay_api_url: str,
    relay_adapter_token: str,
    payload: dict[str, Any],
    correlation_id: str,
    timeout_seconds: int,
) -> tuple[int, dict[str, Any]]:
    encoded = json.dumps(payload).encode("utf-8")
    req = request.Request(url=relay_api_url, method="POST", data=encoded)
    req.add_header("Authorization", f"Bearer {relay_adapter_token}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("X-Correlation-Id", correlation_id)
    try:
        with request.urlopen(req, timeout=timeout_seconds) as resp:
            body = resp.read().decode("utf-8")
            data = json.loads(body) if body else {}
            if isinstance(data, dict):
                return resp.status, data
            return resp.status, {"ok": False, "error": "relay returned non-object response"}
    except error.HTTPError as exc:
        text = exc.read().decode("utf-8")
        try:
            data = json.loads(text) if text else {}
            if isinstance(data, dict):
                return exc.code, data
            return exc.code, {"ok": False, "error": text}
        except json.JSONDecodeError:
            return exc.code, {"ok": False, "error": text}


def relay_get(
    *,
    relay_api_url: str,
    relay_adapter_token: str,
    params: dict[str, str],
    correlation_id: str,
    timeout_seconds: int,
) -> tuple[int, dict[str, Any]]:
    query = parse.urlencode(params)
    url = relay_api_url if not query else f"{relay_api_url}?{query}"
    req = request.Request(url=url, method="GET")
    req.add_header("Authorization", f"Bearer {relay_adapter_token}")
    req.add_header("Accept", "application/json")
    req.add_header("X-Correlation-Id", correlation_id)
    try:
        with request.urlopen(req, timeout=timeout_seconds) as resp:
            body = resp.read().decode("utf-8")
            data = json.loads(body) if body else {}
            if isinstance(data, dict):
                return resp.status, data
            return resp.status, {"ok": False, "error": "relay returned non-object response"}
    except error.HTTPError as exc:
        text = exc.read().decode("utf-8")
        try:
            data = json.loads(text) if text else {}
            if isinstance(data, dict):
                return exc.code, data
            return exc.code, {"ok": False, "error": text}
        except json.JSONDecodeError:
            return exc.code, {"ok": False, "error": text}
