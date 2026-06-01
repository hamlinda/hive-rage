from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _split_suffixes(raw: str) -> tuple[str, ...]:
    values = [item.strip().lower() for item in raw.split(",") if item.strip()]
    normalized = []
    for value in values:
        normalized.append(value if value.startswith(".") else f".{value}")
    return tuple(normalized)


def _split_values(raw: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _join_values(values: tuple[str, ...]) -> str:
    return ",".join(values)


def _default_base_dir() -> Path:
    env_base = os.getenv("HIVE_RAGE_BASE_DIR")
    if env_base:
        return Path(env_base).expanduser().resolve()

    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return Path.cwd().resolve()


@dataclass(slots=True)
class Config:
    base_dir: Path = field(default_factory=_default_base_dir)
    hive_dir: Path = Path("hive")
    db_path: Path = Path("var/index.sqlite3")
    config_path: Path = Path("var/runtime-config.json")
    ollama_base_url: str = "http://127.0.0.1:11434"
    chat_model: str = "llama3:8b"
    embedding_model: str = "nomic-embed-text"
    host: str = "0.0.0.0"
    port: int = 8127
    ingestion_host: str = "127.0.0.1"
    ingestion_port: int = 8128
    frontend_host: str = "127.0.0.1"
    frontend_port: int = 8129
    poll_interval_seconds: int = 10
    top_k: int = 4
    chunk_size: int = 1200
    chunk_overlap: int = 200
    include_globs: tuple[str, ...] = field(default_factory=tuple)
    exclude_globs: tuple[str, ...] = field(default_factory=tuple)
    supported_suffixes: tuple[str, ...] = field(
        default_factory=lambda: (
            ".txt",
            ".md",
            ".rst",
            ".json",
            ".yaml",
            ".yml",
            ".log",
            ".csv",
            ".docx",
            ".pdf",
        )
    )

    @classmethod
    def from_env(cls) -> "Config":
        base_dir = _default_base_dir()
        config_path = Path(os.getenv("HIVE_RAGE_CONFIG_PATH", "var/runtime-config.json"))
        resolved_config_path = config_path if config_path.is_absolute() else (base_dir / config_path)
        file_overrides = _load_config_overrides(resolved_config_path)

        hive_dir = Path(file_overrides.get("hive_dir", os.getenv("HIVE_RAGE_HIVE_DIR", "hive")))
        db_path = Path(file_overrides.get("db_path", os.getenv("HIVE_RAGE_DB_PATH", "var/index.sqlite3")))
        suffixes = os.getenv(
            "HIVE_RAGE_SUFFIXES",
            str(file_overrides.get("supported_suffixes", ".txt,.md,.rst,.json,.yaml,.yml,.log,.csv,.docx,.pdf")),
        )
        include_globs = str(file_overrides.get("include_globs", os.getenv("HIVE_RAGE_INCLUDE_GLOBS", "")))
        exclude_globs = str(file_overrides.get("exclude_globs", os.getenv("HIVE_RAGE_EXCLUDE_GLOBS", "")))
        return cls(
            base_dir=base_dir,
            hive_dir=hive_dir,
            db_path=db_path,
            config_path=config_path,
            ollama_base_url=str(file_overrides.get("ollama_base_url", os.getenv("HIVE_RAGE_OLLAMA_URL", "http://127.0.0.1:11434"))),
            chat_model=str(file_overrides.get("chat_model", os.getenv("HIVE_RAGE_CHAT_MODEL", "llama3:8b"))),
            embedding_model=str(file_overrides.get("embedding_model", os.getenv("HIVE_RAGE_EMBEDDING_MODEL", "nomic-embed-text"))),
            host=str(file_overrides.get("api_host", os.getenv("HIVE_RAGE_HOST", "0.0.0.0"))),
            port=int(file_overrides.get("api_port", os.getenv("HIVE_RAGE_PORT", "8127"))),
            ingestion_host=str(file_overrides.get("ingestion_host", os.getenv("HIVE_RAGE_INGESTION_HOST", "127.0.0.1"))),
            ingestion_port=int(file_overrides.get("ingestion_port", os.getenv("HIVE_RAGE_INGESTION_PORT", "8128"))),
            frontend_host=str(file_overrides.get("frontend_host", os.getenv("HIVE_RAGE_FRONTEND_HOST", "127.0.0.1"))),
            frontend_port=int(file_overrides.get("frontend_port", os.getenv("HIVE_RAGE_FRONTEND_PORT", "8129"))),
            poll_interval_seconds=int(file_overrides.get("poll_interval_seconds", os.getenv("HIVE_RAGE_POLL_SECONDS", "10"))),
            top_k=int(file_overrides.get("top_k", os.getenv("HIVE_RAGE_TOP_K", "4"))),
            chunk_size=int(file_overrides.get("chunk_size", os.getenv("HIVE_RAGE_CHUNK_SIZE", "1200"))),
            chunk_overlap=int(file_overrides.get("chunk_overlap", os.getenv("HIVE_RAGE_CHUNK_OVERLAP", "200"))),
            include_globs=_split_values(include_globs),
            exclude_globs=_split_values(exclude_globs),
            supported_suffixes=_split_suffixes(suffixes),
        )

    def ensure_paths(self) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.hive_dir = self.resolve_path(self.hive_dir)
        self.db_path = self.resolve_path(self.db_path)
        self.config_path = self.resolve_path(self.config_path)
        self.hive_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.parent.mkdir(parents=True, exist_ok=True)

    def resolve_path(self, path: Path) -> Path:
        expanded = path.expanduser()
        if expanded.is_absolute():
            return expanded.resolve()
        return (self.base_dir / expanded).resolve()

    def api_base_url(self) -> str:
        return f"http://{self.public_host(self.host)}:{self.port}"

    def ingestion_base_url(self) -> str:
        return f"http://{self.public_host(self.ingestion_host)}:{self.ingestion_port}"

    def frontend_base_url(self) -> str:
        return f"http://{self.public_host(self.frontend_host)}:{self.frontend_port}"

    @staticmethod
    def public_host(host: str) -> str:
        return "127.0.0.1" if host == "0.0.0.0" else host

    def runtime_settings(self) -> dict[str, Any]:
        return {
            "hive_dir": str(self.hive_dir),
            "db_path": str(self.db_path),
            "config_path": str(self.config_path),
            "ollama_base_url": self.ollama_base_url,
            "chat_model": self.chat_model,
            "embedding_model": self.embedding_model,
            "api_host": self.host,
            "api_port": self.port,
            "ingestion_host": self.ingestion_host,
            "ingestion_port": self.ingestion_port,
            "frontend_host": self.frontend_host,
            "frontend_port": self.frontend_port,
            "poll_interval_seconds": self.poll_interval_seconds,
            "top_k": self.top_k,
            "chunk_size": self.chunk_size,
            "chunk_overlap": self.chunk_overlap,
            "include_globs": list(self.include_globs),
            "exclude_globs": list(self.exclude_globs),
            "supported_suffixes": list(self.supported_suffixes),
        }

    def persisted_settings(self) -> dict[str, Any]:
        settings = self.runtime_settings()
        settings["include_globs"] = _join_values(self.include_globs)
        settings["exclude_globs"] = _join_values(self.exclude_globs)
        settings["supported_suffixes"] = _join_values(self.supported_suffixes)
        return settings

    def save_runtime_settings(self) -> None:
        self.ensure_paths()
        self.config_path.write_text(json.dumps(self.persisted_settings(), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _load_config_overrides(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}
