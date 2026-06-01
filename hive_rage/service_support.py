from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib import error, request


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


@dataclass(slots=True)
class ServiceError(RuntimeError):
    code: str
    message: str
    status: int = HTTPStatus.BAD_REQUEST
    details: dict[str, Any] | None = None

    def __str__(self) -> str:
        return self.message


def success_payload(data: dict[str, Any] | None = None, *, message: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": True}
    if message:
        payload["message"] = message
    if data:
        payload.update(data)
    return payload


def error_payload(code: str, message: str, *, details: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": False,
        "error": {
            "code": code,
            "message": message,
        },
    }
    if details:
        payload["error"]["details"] = details
    return payload


def read_json_request(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    content_length = int(handler.headers.get("Content-Length", "0"))
    raw = handler.rfile.read(content_length) if content_length else b"{}"
    if not raw:
        return {}

    try:
        payload = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ServiceError(
            code="invalid_json",
            message="Request body must contain valid JSON.",
            status=HTTPStatus.BAD_REQUEST,
            details={"reason": str(exc)},
        ) from exc

    if not isinstance(payload, dict):
        raise ServiceError(
            code="invalid_json_type",
            message="Request body must decode to a JSON object.",
            status=HTTPStatus.BAD_REQUEST,
        )
    return payload


def write_json(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    try:
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError):
        return


def request_json(
    base_url: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    method: str | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = request.Request(
        f"{base_url.rstrip('/')}" + path,
        data=body,
        headers={"Content-Type": "application/json"},
        method=method or ("GET" if payload is None else "POST"),
    )
    try:
        with request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ServiceError(
            code="upstream_http_error",
            message=f"Request to {base_url} failed with HTTP {exc.code}.",
            status=HTTPStatus.BAD_GATEWAY,
            details={"response": detail},
        ) from exc
    except error.URLError as exc:
        raise ServiceError(
            code="upstream_unreachable",
            message=f"Unable to reach service at {base_url}.",
            status=HTTPStatus.BAD_GATEWAY,
            details={"reason": str(exc.reason)},
        ) from exc
    except TimeoutError as exc:
        raise ServiceError(
            code="upstream_timeout",
            message=f"Service at {base_url} did not respond before the timeout expired.",
            status=HTTPStatus.GATEWAY_TIMEOUT,
        ) from exc
    except socket.timeout as exc:
        raise ServiceError(
            code="upstream_timeout",
            message=f"Service at {base_url} did not respond before the timeout expired.",
            status=HTTPStatus.GATEWAY_TIMEOUT,
        ) from exc

    if not raw:
        return {}

    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ServiceError(
            code="upstream_invalid_json",
            message=f"Service at {base_url} returned invalid JSON.",
            status=HTTPStatus.BAD_GATEWAY,
            details={"reason": str(exc), "response": raw[:500]},
        ) from exc

    if not isinstance(decoded, dict):
        raise ServiceError(
            code="upstream_invalid_payload",
            message=f"Service at {base_url} returned an unexpected payload.",
            status=HTTPStatus.BAD_GATEWAY,
        )
    return decoded