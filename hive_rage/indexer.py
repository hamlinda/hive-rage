from __future__ import annotations

import fnmatch
import hashlib
import json
import math
import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from time import time
from typing import Iterator

try:
    from docx import Document
except ImportError:
    Document = None

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

from .config import Config
from .ollama import OllamaClient


@dataclass(slots=True)
class Match:
    path: str
    chunk_index: int
    score: float
    content: str


class FileIndexer:
    def __init__(self, config: Config, ollama: OllamaClient) -> None:
        self.config = config
        self.ollama = ollama
        self._lock = threading.Lock()
        self._progress_lock = threading.Lock()
        self._last_scan_started_at: float | None = None
        self._last_scan_completed_at: float | None = None
        self._last_error: str | None = None
        self._recent_file_errors: list[str] = []
        self._scan_progress: dict[str, object] = {
            "currently_scanning": False,
            "files_seen": 0,
            "files_indexed": 0,
            "files_failed": 0,
            "files_skipped": 0,
            "files_removed": 0,
            "current_path": None,
        }

    def initialize(self) -> None:
        self.config.ensure_paths()
        with self._connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS files (
                    path TEXT PRIMARY KEY,
                    mtime REAL NOT NULL,
                    size INTEGER NOT NULL,
                    indexed_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    path TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    embedding TEXT NOT NULL,
                    FOREIGN KEY(path) REFERENCES files(path)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks(path)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS service_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.config.db_path)

    @contextmanager
    def _connection(self) -> sqlite3.Connection:
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def get_state(self, key: str) -> str | None:
        with self._connection() as conn:
            row = conn.execute("SELECT value FROM service_state WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row[0])

    def set_state(self, key: str, value: str) -> None:
        with self._connection() as conn:
            conn.execute(
                "REPLACE INTO service_state(key, value) VALUES (?, ?)",
                (key, value),
            )

    def get_state_json(self, key: str) -> dict[str, object] | None:
        raw = self.get_state(key)
        if raw is None:
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    def set_state_json(self, key: str, value: dict[str, object]) -> None:
        self.set_state(key, json.dumps(value, sort_keys=True))

    def _iter_supported_files(self) -> Iterator[Path]:
        root = self.config.hive_dir
        if not root.exists():
            return

        visited_dirs: set[tuple[int, int]] = set()
        for current_root, dirnames, filenames in os.walk(root, followlinks=True):
            current_path = Path(current_root)
            try:
                current_stat = current_path.stat()
            except OSError:
                dirnames[:] = []
                continue

            current_key = (current_stat.st_dev, current_stat.st_ino)
            if current_key in visited_dirs:
                dirnames[:] = []
                continue
            visited_dirs.add(current_key)

            next_dirnames: list[str] = []
            for dirname in dirnames:
                child_path = current_path / dirname
                child_relative = self._relative_path(child_path)
                if self._is_excluded_dir(child_relative):
                    self._increment_progress("files_skipped")
                    continue
                try:
                    child_stat = child_path.stat()
                except OSError:
                    continue

                child_key = (child_stat.st_dev, child_stat.st_ino)
                if child_key not in visited_dirs:
                    next_dirnames.append(dirname)
            dirnames[:] = next_dirnames

            for filename in filenames:
                path = current_path / filename
                if path.name.startswith("~$"):
                    self._increment_progress("files_skipped")
                    continue
                relative_path = self._relative_path(path)
                if not self._should_include_file(relative_path):
                    self._increment_progress("files_skipped")
                    continue
                try:
                    is_supported = path.is_file() and path.suffix.lower() in self.config.supported_suffixes
                except OSError:
                    continue
                if is_supported:
                    yield path

    def _relative_path(self, path: Path) -> str:
        return path.relative_to(self.config.hive_dir).as_posix()

    def _matches_any(self, relative_path: str, patterns: tuple[str, ...]) -> bool:
        return any(fnmatch.fnmatch(relative_path, pattern) for pattern in patterns)

    def _is_excluded_dir(self, relative_path: str) -> bool:
        if not self.config.exclude_globs:
            return False
        candidates = (relative_path, f"{relative_path}/", f"{relative_path}/**")
        return any(
            fnmatch.fnmatch(candidate, pattern)
            for candidate in candidates
            for pattern in self.config.exclude_globs
        )

    def _should_include_file(self, relative_path: str) -> bool:
        if self.config.include_globs and not self._matches_any(relative_path, self.config.include_globs):
            return False
        if self.config.exclude_globs and self._matches_any(relative_path, self.config.exclude_globs):
            return False
        return True

    def scope_signature(self) -> str:
        payload = {
            "hive_dir": str(self.config.hive_dir),
            "db_path": str(self.config.db_path),
            "include_globs": list(self.config.include_globs),
            "exclude_globs": list(self.config.exclude_globs),
            "supported_suffixes": list(self.config.supported_suffixes),
            "chunk_size": self.config.chunk_size,
            "chunk_overlap": self.config.chunk_overlap,
            "embedding_model": self.config.embedding_model,
        }
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8"))
        return digest.hexdigest()

    def inventory_snapshot(self) -> dict[str, object]:
        manifest_lines: list[str] = []
        file_count = 0
        for path in self._iter_supported_files():
            try:
                stat = path.stat()
            except OSError:
                continue
            relative_path = self._relative_path(path)
            manifest_lines.append(f"{relative_path}|{stat.st_mtime_ns}|{stat.st_size}")
            file_count += 1

        digest = hashlib.sha256("\n".join(sorted(manifest_lines)).encode("utf-8")).hexdigest()
        return {
            "digest": digest,
            "file_count": file_count,
            "scope_signature": self.scope_signature(),
        }

    def plan_scan(self, force: bool = False) -> dict[str, object]:
        snapshot = self.inventory_snapshot()
        previous = self.get_state_json("inventory_snapshot") or {}
        previous_digest = previous.get("digest")
        previous_scope = previous.get("scope_signature")

        if force:
            reason = "forced"
            should_scan = True
        elif previous_scope != snapshot["scope_signature"]:
            reason = "scope_changed"
            should_scan = True
        elif previous_digest != snapshot["digest"]:
            reason = "inventory_changed"
            should_scan = True
        else:
            reason = "no_scope_or_inventory_changes"
            should_scan = False

        return {
            "should_scan": should_scan,
            "reason": reason,
            "snapshot": snapshot,
            "previous_snapshot": previous,
        }

    def _reset_progress(self) -> None:
        with self._progress_lock:
            self._scan_progress = {
                "currently_scanning": True,
                "files_seen": 0,
                "files_indexed": 0,
                "files_failed": 0,
                "files_skipped": 0,
                "files_removed": 0,
                "current_path": None,
            }

    def _increment_progress(self, key: str, amount: int = 1) -> None:
        with self._progress_lock:
            self._scan_progress[key] = int(self._scan_progress.get(key, 0)) + amount

    def _set_current_path(self, path: str | None) -> None:
        with self._progress_lock:
            self._scan_progress["current_path"] = path

    def _finalize_progress(self) -> None:
        with self._progress_lock:
            self._scan_progress["currently_scanning"] = False
            self._scan_progress["current_path"] = None

    def scan_once(self) -> dict[str, int]:
        with self._lock:
            self._last_scan_started_at = time()
            indexed = 0
            removed = 0
            file_errors: list[str] = []
            self._reset_progress()
            try:
                with self._connection() as conn:
                    tracked = {
                        row[0]: (row[1], row[2])
                        for row in conn.execute("SELECT path, mtime, size FROM files")
                    }

                seen_paths: set[str] = set()

                for path in self._iter_supported_files():
                    self._increment_progress("files_seen")
                    try:
                        stat = path.stat()
                    except OSError as exc:
                        self._increment_progress("files_failed")
                        file_errors.append(f"{path}: {exc}")
                        continue

                    relative_path = str(path.relative_to(self.config.hive_dir))
                    self._set_current_path(relative_path)
                    seen_paths.add(relative_path)
                    mtime = stat.st_mtime
                    size = stat.st_size

                    prior = tracked.get(relative_path)
                    if prior is None or prior != (mtime, size):
                        try:
                            self._reindex_path(relative_path, path, mtime, size)
                        except Exception as exc:
                            self._increment_progress("files_failed")
                            file_errors.append(f"{relative_path}: {exc}")
                            continue
                        indexed += 1
                        self._increment_progress("files_indexed")

                missing = set(tracked) - seen_paths
                for relative_path in missing:
                    self._remove_path(relative_path)
                    removed += 1
                    self._increment_progress("files_removed")

                self._recent_file_errors = file_errors[:10]
                self._last_error = "; ".join(file_errors[:3]) if file_errors else None
            except Exception as exc:
                self._last_error = str(exc)
                raise
            finally:
                self._last_scan_completed_at = time()
                self._finalize_progress()
        return {
            "indexed": indexed,
            "removed": removed,
            "failed": len(file_errors),
            "skipped": int(self._scan_progress.get("files_skipped", 0)),
        }

    def _reindex_path(self, relative_path: str, path: Path, mtime: float, size: int) -> None:
        content = self._read_path_content(path)
        chunks = self._chunk_text(content)
        embeddings = [self.ollama.embed(self.config.embedding_model, chunk) for chunk in chunks]
        now = time()
        with self._connection() as conn:
            conn.execute("DELETE FROM chunks WHERE path = ?", (relative_path,))
            conn.execute(
                "REPLACE INTO files(path, mtime, size, indexed_at) VALUES (?, ?, ?, ?)",
                (relative_path, mtime, size, now),
            )
            for index, (chunk, embedding) in enumerate(zip(chunks, embeddings, strict=True)):
                conn.execute(
                    "INSERT INTO chunks(path, chunk_index, content, embedding) VALUES (?, ?, ?, ?)",
                    (relative_path, index, chunk, json.dumps(embedding)),
                )

    def _read_path_content(self, path: Path) -> str:
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            return self._read_pdf(path)
        if suffix == ".docx":
            return self._read_docx(path)
        return path.read_text(encoding="utf-8", errors="ignore")

    def _read_pdf(self, path: Path) -> str:
        if PdfReader is None:
            raise RuntimeError("PDF support requires the 'pypdf' package to be installed")
        reader = PdfReader(str(path))
        pages = []
        for page in reader.pages:
            pages.append(page.extract_text() or "")
        return "\n\n".join(pages)

    def _read_docx(self, path: Path) -> str:
        if Document is None:
            raise RuntimeError("DOCX support requires the 'python-docx' package to be installed")
        document = Document(str(path))
        paragraphs = [paragraph.text for paragraph in document.paragraphs if paragraph.text.strip()]
        return "\n".join(paragraphs)

    def _remove_path(self, relative_path: str) -> None:
        with self._connection() as conn:
            conn.execute("DELETE FROM chunks WHERE path = ?", (relative_path,))
            conn.execute("DELETE FROM files WHERE path = ?", (relative_path,))

    def _chunk_text(self, text: str) -> list[str]:
        clean = text.strip()
        if not clean:
            return [""]

        chunks: list[str] = []
        start = 0
        step = max(1, self.config.chunk_size - self.config.chunk_overlap)
        while start < len(clean):
            end = min(len(clean), start + self.config.chunk_size)
            chunks.append(clean[start:end])
            if end == len(clean):
                break
            start += step
        return chunks

    def search(self, query_embedding: list[float], top_k: int) -> list[Match]:
        matches: list[Match] = []
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT path, chunk_index, content, embedding FROM chunks"
            ).fetchall()
        for path, chunk_index, content, embedding_json in rows:
            score = cosine_similarity(query_embedding, json.loads(embedding_json))
            matches.append(Match(path=path, chunk_index=chunk_index, score=score, content=content))
        matches.sort(key=lambda item: item.score, reverse=True)
        return matches[:top_k]

    def list_files(self, limit: int = 100, prefix: str | None = None) -> list[dict[str, object]]:
        query = "SELECT path, size, indexed_at FROM files"
        params: list[object] = []
        if prefix:
            query += " WHERE path LIKE ?"
            params.append(f"{prefix}%")
        query += " ORDER BY path LIMIT ?"
        params.append(limit)

        with self._connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            {"path": path, "size": size, "indexed_at": indexed_at}
            for path, size, indexed_at in rows
        ]

    def status(self) -> dict[str, object]:
        with self._connection() as conn:
            file_count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            chunk_count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        with self._progress_lock:
            progress = dict(self._scan_progress)
        return {
            "base_dir": str(self.config.base_dir),
            "hive_dir": str(self.config.hive_dir),
            "db_path": str(self.config.db_path),
            "tracked_files": file_count,
            "tracked_chunks": chunk_count,
            "include_globs": list(self.config.include_globs),
            "exclude_globs": list(self.config.exclude_globs),
            "scan_progress": progress,
            "last_scan_started_at": self._last_scan_started_at,
            "last_scan_completed_at": self._last_scan_completed_at,
            "last_error": self._last_error,
            "recent_file_errors": list(self._recent_file_errors),
            "scope_signature": self.scope_signature(),
            "inventory_snapshot": self.get_state_json("inventory_snapshot"),
        }


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return -1.0
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return -1.0
    return numerator / (left_norm * right_norm)
