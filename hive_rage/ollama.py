from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib import error, request


class OllamaError(RuntimeError):
    pass


@dataclass(slots=True)
class OllamaClient:
    base_url: str
    timeout_seconds: int = 120

    def _url(self, path: str) -> str:
        return f"{self.base_url.rstrip('/')}" + path

    def _request(self, path: str, payload: dict[str, Any] | None = None, method: str = "POST") -> Any:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        req = request.Request(
            self._url(path),
            data=body,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except error.HTTPError as exc:
            message = exc.read().decode("utf-8", errors="replace")
            raise OllamaError(f"Ollama HTTP {exc.code}: {message}") from exc
        except error.URLError as exc:
            raise OllamaError(f"Unable to reach Ollama at {self.base_url}: {exc.reason}") from exc

        if not raw:
            return {}
        return json.loads(raw)

    def list_models(self) -> list[str]:
        payload = self._request("/api/tags", payload=None, method="GET")
        return [model["name"] for model in payload.get("models", [])]

    def embed(self, model: str, text: str) -> list[float]:
        payload = self._request("/api/embeddings", {"model": model, "prompt": text})
        embedding = payload.get("embedding")
        if not isinstance(embedding, list):
            raise OllamaError(f"Embedding response missing vector for model {model}")
        return [float(value) for value in embedding]

    def generate(self, model: str, prompt: str) -> str:
        payload = self._request(
            "/api/generate",
            {"model": model, "prompt": prompt, "stream": False},
        )
        response = payload.get("response")
        if not isinstance(response, str):
            raise OllamaError(f"Generate response missing text for model {model}")
        return response.strip()
