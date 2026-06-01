from __future__ import annotations

import threading
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.parse import urlparse

from .config import Config
from .indexer import FileIndexer
from .ollama import OllamaClient, OllamaError
from .service_support import (
    ReusableThreadingHTTPServer,
    ServiceError,
    error_payload,
    read_json_request,
    success_payload,
    write_json,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class IngestionService:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.ollama = OllamaClient(config.ollama_base_url)
        self.indexer = FileIndexer(config, self.ollama)
        self._stop_event = threading.Event()
        self._scan_thread: threading.Thread | None = None
        self._manual_scan_thread: threading.Thread | None = None
        self._http_server: ReusableThreadingHTTPServer | None = None
        self._status_lock = threading.Lock()
        self._poll_lock = threading.Lock()
        self._latest_probe: dict[str, Any] = {
            "checked_at": None,
            "should_scan": True,
            "reason": "startup",
            "snapshot": None,
        }
        self._latest_scan_result: dict[str, Any] = {
            "started_at": None,
            "completed_at": None,
            "ok": False,
            "reason": "startup",
            "result": None,
        }
        self._started_at: str | None = None

    def start(self) -> None:
        self.config.ensure_paths()
        self.config.save_runtime_settings()
        self.indexer.initialize()
        self._started_at = _utc_now()
        self._scan_thread = threading.Thread(target=self._scan_loop, name="hive-rage-ingestion", daemon=True)
        self._scan_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._http_server is not None:
            self._http_server.shutdown()
        if self._scan_thread is not None:
            self._scan_thread.join(timeout=5)

    def _scan_loop(self) -> None:
        self._poll_once(force=False)
        while not self._stop_event.wait(self.config.poll_interval_seconds):
            self._poll_once(force=False)

    def _poll_once(self, *, force: bool) -> dict[str, Any]:
        with self._poll_lock:
            started_at = _utc_now()
            plan = self.indexer.plan_scan(force=force)
            with self._status_lock:
                self._latest_probe = {
                    "checked_at": started_at,
                    **plan,
                }

            if not plan["should_scan"]:
                with self._status_lock:
                    self._latest_scan_result = {
                        "started_at": started_at,
                        "completed_at": _utc_now(),
                        "ok": True,
                        "reason": str(plan["reason"]),
                        "result": {
                            "indexed": 0,
                            "removed": 0,
                            "failed": 0,
                            "skipped": 0,
                            "scan_skipped": True,
                        },
                    }
                return self._latest_scan_result

            try:
                result = self.indexer.scan_once()
            except Exception as exc:
                payload = {
                    "started_at": started_at,
                    "completed_at": _utc_now(),
                    "ok": False,
                    "reason": str(plan["reason"]),
                    "result": None,
                    "error": str(exc),
                }
                with self._status_lock:
                    self._latest_scan_result = payload
                return payload

            snapshot = dict(plan["snapshot"])
            snapshot["updated_at"] = _utc_now()
            self.indexer.set_state_json("inventory_snapshot", snapshot)
            payload = {
                "started_at": started_at,
                "completed_at": _utc_now(),
                "ok": True,
                "reason": str(plan["reason"]),
                "result": result,
                "snapshot": snapshot,
            }
            with self._status_lock:
                self._latest_scan_result = payload
            return payload

    def request_scan(self, force: bool = True) -> dict[str, Any]:
        with self._status_lock:
            running = self._manual_scan_thread is not None and self._manual_scan_thread.is_alive()
            latest_scan = dict(self._latest_scan_result)
        if running:
            return {
                "accepted": False,
                "message": "A manual ingestion scan is already running.",
                "latest_scan": latest_scan,
            }

        self._manual_scan_thread = threading.Thread(
            target=self._poll_once,
            kwargs={"force": force},
            name="hive-rage-ingestion-manual",
            daemon=True,
        )
        self._manual_scan_thread.start()
        return {
            "accepted": True,
            "message": "Manual ingestion scan queued.",
            "force": force,
            "latest_scan": latest_scan,
        }

    def run_http(self) -> None:
        server = ReusableThreadingHTTPServer(
            (self.config.ingestion_host, self.config.ingestion_port),
            self._handler(),
            bind_and_activate=False,
        )
        try:
            server.server_bind()
            server.server_activate()
        except Exception:
            server.server_close()
            raise

        self._http_server = server
        try:
            server.serve_forever(poll_interval=0.5)
        finally:
            server.server_close()

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        service = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                try:
                    parsed = urlparse(self.path)
                    if parsed.path == "/health":
                        write_json(self, HTTPStatus.OK, success_payload(service.health()))
                        return
                    if parsed.path == "/status":
                        write_json(self, HTTPStatus.OK, success_payload(service.status()))
                        return
                    write_json(self, HTTPStatus.NOT_FOUND, error_payload("not_found", "Endpoint not found."))
                except ServiceError as exc:
                    write_json(self, exc.status, error_payload(exc.code, exc.message, details=exc.details))

            def do_POST(self) -> None:  # noqa: N802
                try:
                    if self.path == "/scan":
                        payload = read_json_request(self)
                        force = bool(payload.get("force", False))
                        wait = bool(payload.get("wait", False))
                        if wait:
                            result = service._poll_once(force=force)
                            status = HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_GATEWAY
                            body = success_payload(result) if result.get("ok") else error_payload(
                                "scan_failed",
                                "Ingestion scan failed.",
                                details={"result": result},
                            )
                            write_json(self, status, body)
                            return
                        result = service.request_scan(force=force)
                        write_json(self, HTTPStatus.ACCEPTED, success_payload(result))
                        return
                    if self.path == "/reload-config":
                        service.reload_config()
                        write_json(
                            self,
                            HTTPStatus.OK,
                            success_payload({"config": service.config.runtime_settings()}, message="Configuration reloaded."),
                        )
                        return
                    if self.path == "/shutdown":
                        write_json(self, HTTPStatus.OK, success_payload(message="Shutdown requested."))
                        threading.Thread(target=service.stop, name="hive-rage-ingestion-shutdown", daemon=True).start()
                        return
                    write_json(self, HTTPStatus.NOT_FOUND, error_payload("not_found", "Endpoint not found."))
                except ServiceError as exc:
                    write_json(self, exc.status, error_payload(exc.code, exc.message, details=exc.details))

            def log_message(self, format: str, *args: object) -> None:
                return

        return Handler

    def reload_config(self) -> None:
        refreshed = Config.from_env()
        refreshed.ensure_paths()
        self.config = refreshed
        self.ollama = OllamaClient(refreshed.ollama_base_url)
        self.indexer.config = refreshed
        self.indexer.ollama = self.ollama

    def health(self) -> dict[str, Any]:
        try:
            available_models = self.ollama.list_models()
            ollama_ok = True
            ollama_error = None
        except OllamaError as exc:
            available_models = []
            ollama_ok = False
            ollama_error = str(exc)

        embedding_name = self.config.embedding_model.split(":", 1)[0]
        embedding_available = any(model.split(":", 1)[0] == embedding_name for model in available_models)
        return {
            "service": "ingestion",
            "started_at": self._started_at,
            "ok": ollama_ok and embedding_available,
            "ready": self._scan_thread is not None,
            "ollama_ok": ollama_ok,
            "ollama_error": ollama_error,
            "embedding_model": self.config.embedding_model,
            "embedding_model_available": embedding_available,
            "available_models": available_models,
            "config_path": str(self.config.config_path),
            "db_path": str(self.config.db_path),
            "hive_dir": str(self.config.hive_dir),
        }

    def status(self) -> dict[str, Any]:
        with self._status_lock:
            latest_probe = dict(self._latest_probe)
            latest_scan_result = dict(self._latest_scan_result)
        return {
            "service": {
                "name": "ingestion",
                "host": self.config.ingestion_host,
                "port": self.config.ingestion_port,
                "started_at": self._started_at,
                "poll_interval_seconds": self.config.poll_interval_seconds,
            },
            "config": self.config.runtime_settings(),
            "health": self.health(),
            "index": self.indexer.status(),
            "latest_probe": latest_probe,
            "latest_scan": latest_scan_result,
        }