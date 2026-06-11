from __future__ import annotations

import json
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.parse import urlparse

from .config import Config
from .service_support import ReusableThreadingHTTPServer, ServiceError, request_json


class FrontendService:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._http_server: ReusableThreadingHTTPServer | None = None

    def start(self) -> None:
        self.config.ensure_paths()

    def stop(self) -> None:
        if self._http_server is not None:
            self._http_server.shutdown()

    def run_http(self) -> None:
        server = ReusableThreadingHTTPServer(
            (self.config.frontend_host, self.config.frontend_port),
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
                if parsed.path == "/" or parsed.path == "/index.html":
                    service._write_html(self, HTTPStatus.OK, service.index_html())
                    return
                if parsed.path == "/health":
                    service._write_json(
                        self,
                        HTTPStatus.OK,
                        {
                            "ok": True,
                            "service": "frontend",
                            "backend_url": service.config.api_base_url(),
                        },
                    )
                    return
                if parsed.path.startswith("/api/"):
                    service._proxy_json(self, parsed.path[4:], method="GET")
                    return
                service._write_json(
                    self,
                    HTTPStatus.NOT_FOUND,
                    {"ok": False, "error": {"code": "not_found", "message": "The requested frontend route was not found."}},
                )

            def do_POST(self) -> None:  # noqa: N802
                if self.path.startswith("/api/"):
                    service._proxy_json(self, self.path[4:], method="POST")
                    return
                if self.path == "/shutdown":
                    service._write_json(self, HTTPStatus.OK, {"ok": True, "message": "Frontend shutdown requested."})
                    threading.Thread(target=service.stop, name="hive-rage-frontend-shutdown", daemon=True).start()
                    return
                service._write_json(
                    self,
                    HTTPStatus.NOT_FOUND,
                    {"ok": False, "error": {"code": "not_found", "message": "The requested frontend route was not found."}},
                )

            def log_message(self, format: str, *args: object) -> None:
                return

        return Handler

    def _proxy_json(self, handler: BaseHTTPRequestHandler, path: str, method: str) -> None:
        body: dict[str, Any] | None = None
        if method == "POST":
            content_length = int(handler.headers.get("Content-Length", "0"))
            raw = handler.rfile.read(content_length) if content_length else b"{}"
            body = json.loads(raw.decode("utf-8")) if raw else {}
        try:
            payload = request_json(self.config.api_base_url(), path, body, method=method)
            self._write_json(handler, HTTPStatus.OK, payload)
        except ServiceError as exc:
            self._write_json(
                handler,
                exc.status,
                {"ok": False, "error": {"code": exc.code, "message": exc.message, "details": exc.details}},
            )

    def _write_json(self, handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def _write_html(self, handler: BaseHTTPRequestHandler, status: int, payload: str) -> None:
        body = payload.encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def index_html(self) -> str:
        backend_url = self.config.api_base_url()
        return f"""<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>Hive Rage Control Surface</title>
  <link rel=\"icon\" href=\"data:,\">
  <style>
    :root {{
      --bg: #f5f0e6;
      --panel: rgba(255,255,255,0.82);
      --ink: #1f1b16;
      --muted: #615748;
      --accent: #9f3b28;
      --accent-2: #264653;
      --line: rgba(31,27,22,0.12);
      --ok: #2a6f3e;
      --warn: #8d5b17;
      --err: #a61e22;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Georgia, 'Iowan Old Style', 'Palatino Linotype', serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(159,59,40,0.18), transparent 32%),
        radial-gradient(circle at right 20%, rgba(38,70,83,0.18), transparent 30%),
        linear-gradient(180deg, #f7f2e8 0%, #efe6d5 100%);
      min-height: 100vh;
    }}
    main {{
      max-width: 1200px;
      margin: 0 auto;
      padding: 32px 20px 48px;
    }}
    header {{
      display: grid;
      gap: 14px;
      margin-bottom: 24px;
    }}
    h1 {{
      margin: 0;
      font-size: clamp(2.4rem, 6vw, 4.8rem);
      line-height: 0.95;
      letter-spacing: -0.05em;
    }}
    .subhead {{
      max-width: 72ch;
      font-size: 1.05rem;
      color: var(--muted);
    }}
    .layout {{
      display: grid;
      grid-template-columns: 1.15fr 0.85fr;
      gap: 18px;
    }}
    .panel {{
      background: var(--panel);
      backdrop-filter: blur(14px);
      border: 1px solid var(--line);
      border-radius: 22px;
      padding: 18px;
      box-shadow: 0 18px 40px rgba(31,27,22,0.08);
    }}
    .panel h2 {{ margin: 0 0 12px; font-size: 1.25rem; }}
    .status-grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }}
    .metric {{ border: 1px solid var(--line); border-radius: 16px; padding: 12px; background: rgba(255,255,255,0.55); }}
    .metric strong {{ display: block; font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); margin-bottom: 6px; }}
    .metric span {{ font-size: 1.2rem; }}
    form {{ display: grid; gap: 12px; }}
    label {{ display: grid; gap: 6px; font-size: 0.94rem; }}
    textarea, input {{
      width: 100%;
      border-radius: 14px;
      border: 1px solid var(--line);
      padding: 12px 14px;
      background: rgba(255,255,255,0.92);
      color: var(--ink);
      font: inherit;
    }}
    textarea {{ min-height: 120px; resize: vertical; }}
    .two-col {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 12px; }}
    .actions {{ display: flex; gap: 10px; flex-wrap: wrap; }}
    button {{
      border: 0;
      border-radius: 999px;
      padding: 11px 18px;
      font: inherit;
      cursor: pointer;
      background: var(--accent);
      color: #fff;
    }}
    button.secondary {{ background: var(--accent-2); }}
    pre {{
      white-space: pre-wrap;
      word-break: break-word;
      margin: 0;
      padding: 14px;
      border-radius: 14px;
      background: rgba(31,27,22,0.05);
      border: 1px solid var(--line);
      min-height: 80px;
    }}
    .message {{
      border-radius: 14px;
      padding: 12px 14px;
      font-size: 0.95rem;
      display: none;
    }}
    .message.visible {{ display: block; }}
    .message.ok {{ background: rgba(42,111,62,0.12); color: var(--ok); }}
    .message.warn {{ background: rgba(141,91,23,0.12); color: var(--warn); }}
    .message.err {{ background: rgba(166,30,34,0.12); color: var(--err); }}
    .footnote {{ color: var(--muted); font-size: 0.9rem; }}
    @media (max-width: 960px) {{
      .layout {{ grid-template-columns: 1fr; }}
      .status-grid, .two-col {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <h1>Hive Rage</h1>
      <div class=\"subhead\">Three local components, one control surface. The ingestion daemon watches scope changes and updates the database only when the inventory fingerprint changes. The backend coordinates ingestion status, retrieval, and LLM calls. This frontend gives you local configuration and query access without shelling into the service stack.</div>
      <div class=\"footnote\">Backend URL: {backend_url}</div>
    </header>
    <section class=\"panel\" style=\"margin-bottom:18px;\">
      <h2>Live Status</h2>
      <div class=\"status-grid\">
        <div class=\"metric\"><strong>Backend</strong><span id=\"backend-state\">Checking</span></div>
        <div class=\"metric\"><strong>Ingestion</strong><span id=\"ingestion-state\">Checking</span></div>
        <div class=\"metric\"><strong>Tracked Files</strong><span id=\"tracked-files\">-</span></div>
      </div>
    </section>
    <div class=\"layout\">
      <section class=\"panel\">
        <h2>Query</h2>
        <form id=\"query-form\">
          <label>Question
            <textarea id=\"question\" placeholder=\"Ask the local hive a grounded question.\"></textarea>
          </label>
          <div class=\"two-col\">
            <label>Top K
              <input id=\"top-k\" type=\"number\" value=\"4\" min=\"1\">
            </label>
            <label>Quick Reindex
              <button class=\"secondary\" type=\"button\" id=\"reindex-btn\">Run Ingestion Check</button>
            </label>
          </div>
          <div class=\"actions\">
            <button type=\"submit\">Run Query</button>
          </div>
        </form>
        <div id=\"query-message\" class=\"message\"></div>
        <pre id=\"query-output\">No query has been run yet.</pre>
      </section>
      <section class=\"panel\">
        <h2>Logical Configuration</h2>
        <form id=\"config-form\">
          <div class=\"two-col\">
            <label>Hive Directory
              <input id=\"hive-dir\" type=\"text\">
            </label>
            <label>Database Path
              <input id=\"db-path\" type=\"text\">
            </label>
          </div>
          <div class=\"two-col\">
            <label>Chat Model
              <input id=\"chat-model\" type=\"text\">
            </label>
            <label>Embedding Model
              <input id=\"embedding-model\" type=\"text\">
            </label>
          </div>
          <div class=\"two-col\">
            <label>Poll Seconds
              <input id=\"poll-seconds\" type=\"number\" min=\"1\">
            </label>
            <label>Top K
              <input id=\"config-top-k\" type=\"number\" min=\"1\">
            </label>
          </div>
          <div class=\"two-col\">
            <label>Chunk Size
              <input id=\"chunk-size\" type=\"number\" min=\"100\">
            </label>
            <label>Chunk Overlap
              <input id=\"chunk-overlap\" type=\"number\" min=\"0\">
            </label>
          </div>
          <label>Include Globs
            <input id=\"include-globs\" type=\"text\" placeholder=\"files/**/*.pdf,files/**/*.docx\">
          </label>
          <label>Exclude Globs
            <input id=\"exclude-globs\" type=\"text\" placeholder=\"files/**/Archive/**\">
          </label>
          <label>Supported Suffixes
            <input id=\"supported-suffixes\" type=\"text\" placeholder=\".txt,.md,.pdf\">
          </label>
          <div class=\"actions\">
            <button type=\"submit\">Persist Configuration</button>
            <button class=\"secondary\" type=\"button\" id=\"refresh-btn\">Refresh Status</button>
          </div>
        </form>
        <div id=\"config-message\" class=\"message\"></div>
      </section>
    </div>
  </main>
  <script>
    const setMessage = (element, type, text) => {{
      element.className = `message visible ${{type}}`;
      element.textContent = text;
    }};

    const clearMessage = (element) => {{
      element.className = 'message';
      element.textContent = '';
    }};

    const parseList = (value) => value.split(',').map((item) => item.trim()).filter(Boolean);

    async function callApi(path, options = {{}}) {{
      const response = await fetch(`/api${{path}}`, {{
        headers: {{ 'Content-Type': 'application/json' }},
        ...options,
      }});
      const payload = await response.json();
      if (!response.ok || payload.ok === false) {{
        const message = payload?.error?.message || 'Request failed.';
        throw new Error(message);
      }}
      return payload;
    }}

    async function refreshStatus() {{
      try {{
        const statusPayload = await callApi('/status');
        const status = statusPayload;
        document.getElementById('backend-state').textContent = status.health?.ok ? 'Ready' : 'Attention';
        document.getElementById('ingestion-state').textContent = status.ingestion?.ok ? 'Ready' : 'Attention';
        document.getElementById('tracked-files').textContent = String(status.index?.tracked_files ?? '-');
      }} catch (error) {{
        document.getElementById('backend-state').textContent = 'Unavailable';
        document.getElementById('ingestion-state').textContent = 'Unavailable';
      }}
    }}

    async function loadConfig() {{
      const payload = await callApi('/config');
      const config = payload.editable;
      document.getElementById('hive-dir').value = config.hive_dir || '';
      document.getElementById('db-path').value = config.db_path || '';
      document.getElementById('chat-model').value = config.chat_model || '';
      document.getElementById('embedding-model').value = config.embedding_model || '';
      document.getElementById('poll-seconds').value = config.poll_interval_seconds || 10;
      document.getElementById('config-top-k').value = config.top_k || 4;
      document.getElementById('chunk-size').value = config.chunk_size || 1200;
      document.getElementById('chunk-overlap').value = config.chunk_overlap || 200;
      document.getElementById('include-globs').value = (config.include_globs || []).join(',');
      document.getElementById('exclude-globs').value = (config.exclude_globs || []).join(',');
      document.getElementById('supported-suffixes').value = (config.supported_suffixes || []).join(',');
    }}

    document.getElementById('query-form').addEventListener('submit', async (event) => {{
      event.preventDefault();
      const message = document.getElementById('query-message');
      clearMessage(message);
      try {{
        const payload = await callApi('/query', {{
          method: 'POST',
          body: JSON.stringify({{
            question: document.getElementById('question').value,
            top_k: Number(document.getElementById('top-k').value || 4),
          }}),
        }});
        const result = payload.query;
        const sourceLines = (result.sources || []).map((item) => `- ${{item.path}}#${{item.chunk_index}} score=${{item.score}}`);
        document.getElementById('query-output').textContent = `${{result.answer}}\\n\\nSources:\\n${{sourceLines.join('\\n') || 'No sources returned.'}}`;
        setMessage(message, 'ok', 'Query completed successfully.');
      }} catch (error) {{
        document.getElementById('query-output').textContent = error.message;
        setMessage(message, 'err', error.message);
      }}
    }});

    document.getElementById('config-form').addEventListener('submit', async (event) => {{
      event.preventDefault();
      const message = document.getElementById('config-message');
      clearMessage(message);
      try {{
        await callApi('/config', {{
          method: 'POST',
          body: JSON.stringify({{
            hive_dir: document.getElementById('hive-dir').value,
            db_path: document.getElementById('db-path').value,
            chat_model: document.getElementById('chat-model').value,
            embedding_model: document.getElementById('embedding-model').value,
            poll_interval_seconds: Number(document.getElementById('poll-seconds').value || 10),
            top_k: Number(document.getElementById('config-top-k').value || 4),
            chunk_size: Number(document.getElementById('chunk-size').value || 1200),
            chunk_overlap: Number(document.getElementById('chunk-overlap').value || 200),
            include_globs: parseList(document.getElementById('include-globs').value),
            exclude_globs: parseList(document.getElementById('exclude-globs').value),
            supported_suffixes: parseList(document.getElementById('supported-suffixes').value),
          }}),
        }});
        setMessage(message, 'ok', 'Configuration persisted. The backend reloaded it and notified ingestion.');
        await loadConfig();
        await refreshStatus();
      }} catch (error) {{
        setMessage(message, 'err', error.message);
      }}
    }});

    document.getElementById('refresh-btn').addEventListener('click', async () => {{
      await loadConfig();
      await refreshStatus();
    }});

    document.getElementById('reindex-btn').addEventListener('click', async () => {{
      const message = document.getElementById('query-message');
      clearMessage(message);
      try {{
        const payload = await callApi('/reindex', {{ method: 'POST', body: JSON.stringify({{ force: true }}) }});
        setMessage(message, 'ok', payload.reindex?.reason ? `Ingestion result: ${{payload.reindex.reason}}.` : 'Ingestion check requested.');
        await refreshStatus();
      }} catch (error) {{
        setMessage(message, 'err', error.message);
      }}
    }});

    loadConfig().then(refreshStatus).catch((error) => {{
      setMessage(document.getElementById('config-message'), 'err', error.message);
    }});
  </script>
</body>
</html>"""