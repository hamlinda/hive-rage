from __future__ import annotations

import copy
import json
import threading
from dataclasses import asdict, dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from time import time
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import Config
from .indexer import FileIndexer
from .ollama import OllamaClient, OllamaError
from .service_support import (
    ReusableThreadingHTTPServer,
    ServiceError,
    error_payload,
    read_json_request,
    request_json,
    success_payload,
    write_json,
)


MODEL_RECOMMENDATIONS = {
    "small": {
        "chat": "qwen2.5:3b",
        "embedding": "nomic-embed-text",
        "use_for": "Low-memory systems and faster local iteration.",
    },
    "default": {
        "chat": "llama3:8b",
        "embedding": "nomic-embed-text",
        "use_for": "Balanced retrieval-augmented Q&A on a typical workstation with broad Ollama support.",
    },
    "higher_quality": {
        "chat": "qwen2.5:7b",
        "embedding": "nomic-embed-text",
        "use_for": "Stronger instruction following when you are willing to pull another chat model.",
    },
}


@dataclass(slots=True)
class QueryResult:
    question: str
    answer: str
    model: str
    sources: list[dict[str, Any]]
    elapsed_seconds: float


class HiveRageBackendService:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.ollama = OllamaClient(config.ollama_base_url)
        self.indexer = FileIndexer(config, self.ollama)
        self._stop_event = threading.Event()
        self._http_server: ReusableThreadingHTTPServer | None = None

    def start(self) -> None:
        self.config.ensure_paths()
        self.indexer.initialize()

    def stop(self) -> None:
        self._stop_event.set()
        if self._http_server is not None:
            self._http_server.shutdown()

    def reload_config(self) -> None:
        self.config = Config.from_env()
        self.config.ensure_paths()
        self.ollama = OllamaClient(self.config.ollama_base_url)
        self.indexer = FileIndexer(self.config, self.ollama)
        self.indexer.initialize()

    def run_http(self) -> None:
        server = ReusableThreadingHTTPServer(
            (self.config.host, self.config.port),
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
                parsed = urlparse(self.path)
                try:
                    if parsed.path == "/health":
                        write_json(self, HTTPStatus.OK, success_payload(service.health()))
                        return
                    if parsed.path == "/status":
                        write_json(self, HTTPStatus.OK, success_payload(service.status()))
                        return
                    if parsed.path == "/models":
                        write_json(self, HTTPStatus.OK, success_payload(service.model_status()))
                        return
                    if parsed.path == "/files":
                        query = parse_qs(parsed.query)
                        limit = int(query.get("limit", ["100"])[0])
                        prefix = query.get("prefix", [None])[0]
                        write_json(
                            self,
                            HTTPStatus.OK,
                            success_payload(
                                {
                                    "files": service.list_files(limit=limit, prefix=prefix),
                                    "limit": limit,
                                    "prefix": prefix,
                                }
                            ),
                        )
                        return
                    if parsed.path == "/config":
                        write_json(self, HTTPStatus.OK, success_payload(service.config_payload()))
                        return
                    raise ServiceError("not_found", "The requested backend endpoint was not found.", HTTPStatus.NOT_FOUND)
                except ServiceError as exc:
                    write_json(self, exc.status, error_payload(exc.code, exc.message, details=exc.details))

            def do_POST(self) -> None:  # noqa: N802
                try:
                    if self.path == "/query":
                        payload = read_json_request(self)
                        question = str(payload.get("question", "")).strip()
                        top_k = int(payload.get("top_k", service.config.top_k))
                        if not question:
                            raise ServiceError("missing_question", "A non-empty question is required.")
                        try:
                            result = asdict(service.query(question, top_k))
                        except OllamaError as exc:
                            raise ServiceError(
                                "ollama_query_failed",
                                "The backend could not complete the LLM request.",
                                status=HTTPStatus.BAD_GATEWAY,
                                details={"reason": str(exc)},
                            ) from exc
                        write_json(self, HTTPStatus.OK, success_payload({"query": result}))
                        return
                    if self.path == "/reindex":
                        payload = read_json_request(self)
                        result = service.request_reindex(force=bool(payload.get("force", True)))
                        write_json(self, HTTPStatus.OK, success_payload({"reindex": result}))
                        return
                    if self.path == "/config":
                        payload = read_json_request(self)
                        result = service.update_config(payload)
                        write_json(self, HTTPStatus.OK, success_payload(result))
                        return
                    if self.path == "/shutdown":
                        write_json(self, HTTPStatus.OK, success_payload(message="Backend shutdown requested."))
                        threading.Thread(target=service.stop, name="hive-rage-backend-shutdown", daemon=True).start()
                        return
                    raise ServiceError("not_found", "The requested backend endpoint was not found.", HTTPStatus.NOT_FOUND)
                except ServiceError as exc:
                    write_json(self, exc.status, error_payload(exc.code, exc.message, details=exc.details))

            def log_message(self, format: str, *args: object) -> None:
                return

        return Handler

    def health(self) -> dict[str, Any]:
        ingestion_health: dict[str, Any]
        try:
            models = self.ollama.list_models()
            ollama_ok = True
        except OllamaError as exc:
            models = []
            ollama_ok = False
            ollama_error = str(exc)
        else:
            ollama_error = None
        try:
            ingestion_health = request_json(self.config.ingestion_base_url(), "/health")
        except ServiceError as exc:
            ingestion_health = error_payload(exc.code, exc.message, details=exc.details)
        return {
            "service": "backend",
            "ok": ollama_ok and bool(ingestion_health.get("ok", False)),
            "ollama_ok": ollama_ok,
            "ollama_error": ollama_error,
            "available_models": models,
            "ingestion": ingestion_health,
        }

    def model_status(self) -> dict[str, Any]:
        available = self.health().get("available_models", [])
        normalized_available = {model.split(":", 1)[0] if ":" in model else model for model in available}
        configured_chat_name = self.config.chat_model.split(":", 1)[0]
        configured_embedding_name = self.config.embedding_model.split(":", 1)[0]
        return {
            "configured": {
                "chat_model": self.config.chat_model,
                "embedding_model": self.config.embedding_model,
                "chat_model_available": configured_chat_name in normalized_available,
                "embedding_model_available": configured_embedding_name in normalized_available,
            },
            "available": available,
            "recommended": MODEL_RECOMMENDATIONS,
            "pull_commands": [
                f"ollama pull {recommendation['chat']}" for recommendation in MODEL_RECOMMENDATIONS.values()
            ] + ["ollama pull nomic-embed-text"],
            "interaction_methods": {
                "local_cli": [
                    "hive-rage status",
                    "hive-rage start-stack",
                    "hive-rage list-files --limit 20",
                    "hive-rage query 'What changed in the hive?'",
                    "hive-rage reindex",
                ],
                "http_api": [
                    "GET /health",
                    "GET /status",
                    "GET /models",
                    "GET /files",
                    "GET /config",
                    "POST /query",
                    "POST /reindex",
                    "POST /config",
                ],
            },
            "frontend_url": self.config.frontend_base_url(),
        }

    def status(self) -> dict[str, Any]:
        ingestion_status = self._ingestion_status()
        return {
            "health": self.health(),
            "service": {
                "host": self.config.host,
                "port": self.config.port,
                "base_url": self.config.api_base_url(),
                "chat_model": self.config.chat_model,
                "embedding_model": self.config.embedding_model,
            },
            "frontend": {
                "host": self.config.frontend_host,
                "port": self.config.frontend_port,
                "base_url": self.config.frontend_base_url(),
            },
            "index": self.indexer.status(),
            "ingestion": ingestion_status,
            "models": self.model_status(),
            "config": self.config_payload(),
        }

    def _ingestion_status(self) -> dict[str, Any]:
        try:
            return request_json(self.config.ingestion_base_url(), "/status")
        except ServiceError as exc:
            return error_payload(exc.code, exc.message, details=exc.details)

    def list_files(self, limit: int = 100, prefix: str | None = None) -> list[dict[str, object]]:
        return self.indexer.list_files(limit=limit, prefix=prefix)

    def request_reindex(self, force: bool = True) -> dict[str, Any]:
        return request_json(self.config.ingestion_base_url(), "/scan", {"force": force})

    def config_payload(self) -> dict[str, Any]:
        runtime = self.config.runtime_settings()
        return {
            "editable": {
                "hive_dir": runtime["hive_dir"],
                "db_path": runtime["db_path"],
                "ollama_base_url": runtime["ollama_base_url"],
                "chat_model": runtime["chat_model"],
                "embedding_model": runtime["embedding_model"],
                "poll_interval_seconds": runtime["poll_interval_seconds"],
                "top_k": runtime["top_k"],
                "chunk_size": runtime["chunk_size"],
                "chunk_overlap": runtime["chunk_overlap"],
                "include_globs": runtime["include_globs"],
                "exclude_globs": runtime["exclude_globs"],
                "supported_suffixes": runtime["supported_suffixes"],
            },
            "readonly": {
                "config_path": runtime["config_path"],
                "api_url": self.config.api_base_url(),
                "ingestion_url": self.config.ingestion_base_url(),
                "frontend_url": self.config.frontend_base_url(),
            },
        }

    def update_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        editable = self._validate_config_update(payload)
        updated = copy.deepcopy(self.config.persisted_settings())
        updated.update(editable)
        config_path = self.config.resolve_path(self.config.config_path)
        config_path.write_text(json.dumps(updated, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self.reload_config()

        reload_warning = None
        try:
            request_json(self.config.ingestion_base_url(), "/reload-config", {})
        except ServiceError as exc:
            reload_warning = error_payload(exc.code, exc.message, details=exc.details)

        response: dict[str, Any] = {
            "config": self.config_payload(),
            "message": "Logical configuration was persisted and reloaded by the backend.",
        }
        if reload_warning is not None:
            response["warning"] = {
                "message": "The backend reloaded the new configuration, but the ingestion service did not confirm its reload.",
                "details": reload_warning,
            }
        return response

    def _validate_config_update(self, payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "hive_dir",
            "db_path",
            "ollama_base_url",
            "chat_model",
            "embedding_model",
            "poll_interval_seconds",
            "top_k",
            "chunk_size",
            "chunk_overlap",
            "include_globs",
            "exclude_globs",
            "supported_suffixes",
        }
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ServiceError(
                "unknown_config_keys",
                "Configuration update included unsupported keys.",
                details={"keys": unknown},
            )

        validated: dict[str, Any] = {}
        for key, value in payload.items():
            if key in {"poll_interval_seconds", "top_k", "chunk_size", "chunk_overlap"}:
                try:
                    validated[key] = int(value)
                except (TypeError, ValueError) as exc:
                    raise ServiceError(
                        "invalid_integer_config",
                        f"Configuration key '{key}' must be an integer.",
                    ) from exc
                continue
            if key in {"include_globs", "exclude_globs", "supported_suffixes"}:
                if isinstance(value, str):
                    validated[key] = value
                elif isinstance(value, list) and all(isinstance(item, str) for item in value):
                    validated[key] = ",".join(item.strip() for item in value if item.strip())
                else:
                    raise ServiceError(
                        "invalid_list_config",
                        f"Configuration key '{key}' must be a string or a list of strings.",
                    )
                continue
            if not isinstance(value, str) or not value.strip():
                raise ServiceError(
                    "invalid_string_config",
                    f"Configuration key '{key}' must be a non-empty string.",
                )
            validated[key] = value.strip()
        return validated

    def query(self, question: str, top_k: int | None = None) -> QueryResult:
        started_at = time()
        query_embedding = self.ollama.embed(self.config.embedding_model, question)
        matches = self.indexer.search(query_embedding, top_k or self.config.top_k)
        prompt = self._build_prompt(question, matches)
        answer = self.ollama.generate(self.config.chat_model, prompt)
        return QueryResult(
            question=question,
            answer=answer,
            model=self.config.chat_model,
            sources=[
                {
                    "path": match.path,
                    "chunk_index": match.chunk_index,
                    "score": round(match.score, 4),
                    "content_preview": match.content[:240],
                }
                for match in matches
            ],
            elapsed_seconds=round(time() - started_at, 3),
        )

    def _build_prompt(self, question: str, matches: list[Any]) -> str:
        context_blocks = []
        for match in matches:
            context_blocks.append(
                f"[Source: {match.path}#{match.chunk_index} score={match.score:.4f}]\n{match.content}"
            )
        context = "\n\n".join(context_blocks) if context_blocks else "No indexed context was found."
        return (
            "You are answering questions using a local knowledge hive. "
            "Prefer the provided context. If the answer is not grounded in the context, say that clearly.\n\n"
            f"Question: {question}\n\n"
            f"Context:\n{context}\n\n"
            "Answer with a concise response and cite the most relevant source paths."
        )


HiveRageService = HiveRageBackendService
