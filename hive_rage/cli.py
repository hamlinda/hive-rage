from __future__ import annotations

import argparse
import json
import os
import re
import signal
import socket
import subprocess
import sys
from time import monotonic, sleep
from urllib import error, request

from .app import HiveRageBackendService, HiveRageService
from .config import Config
from .frontend import FrontendService
from .ingestion import IngestionService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hive-rage")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run-service", help="Run the backend API service (compatibility alias).")
    run_parser.add_argument("--host", default=None)
    run_parser.add_argument("--port", type=int, default=None)

    run_backend_parser = subparsers.add_parser("run-backend", help="Run the backend API service.")
    run_backend_parser.add_argument("--host", default=None)
    run_backend_parser.add_argument("--port", type=int, default=None)

    run_ingestion_parser = subparsers.add_parser("run-ingestion", help="Run the autonomous ingestion daemon.")
    run_ingestion_parser.add_argument("--host", default=None)
    run_ingestion_parser.add_argument("--port", type=int, default=None)

    run_frontend_parser = subparsers.add_parser("run-frontend", help="Run the local web control surface.")
    run_frontend_parser.add_argument("--host", default=None)
    run_frontend_parser.add_argument("--port", type=int, default=None)

    start_stack_parser = subparsers.add_parser(
        "start-stack",
        help="Start ingestion, backend, and frontend as a coordinated local stack.",
    )
    start_stack_parser.add_argument("--timeout", type=int, default=45)

    stop_stack_parser = subparsers.add_parser(
        "stop-stack",
        help="Stop the frontend, backend, and ingestion services in dependency order.",
    )
    stop_stack_parser.add_argument("--force", action="store_true")

    reset_start_parser = subparsers.add_parser(
        "reset-start",
        help="Stop the full stack, delete the local index database, and restart the stack.",
    )

    status_parser = subparsers.add_parser("status", help="Read backend status from the HTTP API.")
    status_parser.add_argument("--url", default=None)
    status_parser.add_argument("--json", action="store_true")

    stack_status_parser = subparsers.add_parser("stack-status", help="Report health for all three services.")
    stack_status_parser.add_argument("--json", action="store_true")

    query_parser = subparsers.add_parser("query", help="Send a query to the running service.")
    query_parser.add_argument("question")
    query_parser.add_argument("--top-k", type=int, default=None)
    query_parser.add_argument("--url", default=None)
    query_parser.add_argument("--json", action="store_true")

    models_parser = subparsers.add_parser("models", help="Inspect configured and recommended models.")
    models_parser.add_argument("--url", default=None)

    files_parser = subparsers.add_parser("list-files", help="List indexed file paths from the running service.")
    files_parser.add_argument("--url", default=None)
    files_parser.add_argument("--limit", type=int, default=100)
    files_parser.add_argument("--prefix", default=None)
    files_parser.add_argument("--json", action="store_true")

    reindex_parser = subparsers.add_parser("reindex", help="Force a scan immediately.")
    reindex_parser.add_argument("--url", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = Config.from_env()

    if args.command in {"run-service", "run-backend"}:
        if args.host:
            config.host = args.host
        if args.port:
            config.port = args.port
        return run_backend_service(config)

    if args.command == "run-ingestion":
        if args.host:
            config.ingestion_host = args.host
        if args.port:
            config.ingestion_port = args.port
        return run_ingestion_service(config)

    if args.command == "run-frontend":
        if args.host:
            config.frontend_host = args.host
        if args.port:
            config.frontend_port = args.port
        return run_frontend_service(config)

    if args.command == "start-stack":
        return start_stack(config, timeout_seconds=args.timeout)

    if args.command == "stop-stack":
        return stop_stack(config, force=args.force)

    if args.command == "reset-start":
        return reset_start(config)

    if args.command == "status":
        payload = _api_request(args.url or default_api_url(config), "/status")
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print_status(payload)
        return 0

    if args.command == "stack-status":
        payload = stack_status(config)
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print_stack_status(payload)
        return 0

    if args.command == "query":
        body = {"question": args.question}
        if args.top_k is not None:
            body["top_k"] = args.top_k
        payload = _api_request(args.url or default_api_url(config), "/query", body)
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            result = payload["query"]
            print(result["answer"])
            if result.get("sources"):
                print("\nSources:")
                for source in result["sources"]:
                    print(f"- {source['path']}#{source['chunk_index']} score={source['score']}")
        return 0

    if args.command == "models":
        payload = _api_request(args.url or default_api_url(config), "/models")
        print(json.dumps(payload, indent=2))
        return 0

    if args.command == "list-files":
        path = f"/files?limit={args.limit}"
        if args.prefix:
            path += f"&prefix={request.pathname2url(args.prefix)}"
        payload = _api_request(args.url or default_api_url(config), path)
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            for item in payload.get("files", []):
                print(item["path"])
        return 0

    if args.command == "reindex":
        payload = _api_request(args.url or default_api_url(config), "/reindex", {})
        print(json.dumps(payload, indent=2))
        return 0

    parser.error(f"Unsupported command: {args.command}")
    return 2


def run_backend_service(config: Config) -> int:
    service = HiveRageBackendService(config)
    return _run_http_service(
        service=service,
        host=config.host,
        port=config.port,
        component_name="backend",
        replacement_hint="Use 'hive-rage start-stack' to start the full stack or 'hive-rage stop-stack' to replace it cleanly.",
    )


def run_service(config: Config) -> int:
    return run_backend_service(config)


def run_ingestion_service(config: Config) -> int:
    service = IngestionService(config)
    return _run_http_service(
        service=service,
        host=config.ingestion_host,
        port=config.ingestion_port,
        component_name="ingestion",
        replacement_hint="Use 'hive-rage start-stack' to start coordinated services or 'hive-rage stop-stack' to stop stale ones.",
    )


def run_frontend_service(config: Config) -> int:
    service = FrontendService(config)
    return _run_http_service(
        service=service,
        host=config.frontend_host,
        port=config.frontend_port,
        component_name="frontend",
        replacement_hint="Use 'hive-rage start-stack' to manage the whole stack together.",
    )


def _run_http_service(
    *,
    service: object,
    host: str,
    port: int,
    component_name: str,
    replacement_hint: str,
) -> int:
    service.start()

    def shutdown(*_: object) -> None:
        service.stop()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    try:
        service.run_http()
    except OSError as exc:
        service.stop()
        if exc.errno == 98:
            raise SystemExit(
                f"Port {port} is already in use, so the {component_name} service could not start. {replacement_hint}"
            ) from exc
        raise
    return 0


def reset_start(config: Config) -> int:
    config.ensure_paths()
    stop_stack(config, force=True)
    if config.db_path.exists():
        config.db_path.unlink()
        print(f"deleted index: {config.db_path}")
    else:
        print(f"index not found, starting fresh: {config.db_path}")
    return start_stack(config, timeout_seconds=45)


def start_stack(config: Config, timeout_seconds: int) -> int:
    config.ensure_paths()
    config.save_runtime_settings()
    _ensure_runtime_dirs(config)

    for spec in _component_specs(config):
        if _health_ok(spec["health_url"]):
            print(f"{spec['name']} already running at {spec['health_url']}")
            continue
        _stop_component(config, spec, force=False)
        _spawn_component(config, spec)
        if not _wait_for_health(spec["health_url"], timeout_seconds=timeout_seconds):
            log_excerpt = _tail_file(_log_file_path(config, spec["name"]))
            raise SystemExit(
                f"{spec['name']} failed to become healthy within {timeout_seconds}s. "
                f"Check { _log_file_path(config, spec['name']) }\n\n{log_excerpt}"
            )
        print(f"started {spec['name']} at {spec['public_url']}")

    print(f"frontend: {config.frontend_base_url()}")
    print(f"backend: {config.api_base_url()}")
    print(f"ingestion: {config.ingestion_base_url()}")
    return 0


def stop_stack(config: Config, force: bool) -> int:
    stopped_any = False
    for spec in reversed(_component_specs(config)):
        stopped = _stop_component(config, spec, force=force)
        stopped_any = stopped_any or stopped
    if stopped_any:
        print("stack stopped")
    else:
        print("stack already stopped")
    return 0


def stack_status(config: Config) -> dict[str, object]:
    return {
        "backend": _safe_health(config.api_base_url()),
        "ingestion": _safe_health(config.ingestion_base_url()),
        "frontend": _safe_health(config.frontend_base_url()),
    }


def default_api_url(config: Config) -> str:
    return config.api_base_url()


def _component_specs(config: Config) -> list[dict[str, object]]:
    return [
        {
            "name": "ingestion",
            "command": [sys.executable, "-m", "hive_rage", "run-ingestion"],
            "health_url": f"{config.ingestion_base_url()}/health",
            "shutdown_url": f"{config.ingestion_base_url()}/shutdown",
            "public_url": config.ingestion_base_url(),
            "port": config.ingestion_port,
        },
        {
            "name": "backend",
            "command": [sys.executable, "-m", "hive_rage", "run-backend"],
            "health_url": f"{config.api_base_url()}/health",
            "shutdown_url": f"{config.api_base_url()}/shutdown",
            "public_url": config.api_base_url(),
            "port": config.port,
        },
        {
            "name": "frontend",
            "command": [sys.executable, "-m", "hive_rage", "run-frontend"],
            "health_url": f"{config.frontend_base_url()}/health",
            "shutdown_url": f"{config.frontend_base_url()}/shutdown",
            "public_url": config.frontend_base_url(),
            "port": config.frontend_port,
        },
    ]


def _ensure_runtime_dirs(config: Config) -> None:
    (config.base_dir / "var" / "log").mkdir(parents=True, exist_ok=True)


def _log_file_path(config: Config, component_name: str) -> str:
    return str(config.base_dir / "var" / "log" / f"{component_name}.log")


def _spawn_component(config: Config, spec: dict[str, object]) -> None:
    log_path = _log_file_path(config, str(spec["name"]))
    with open(log_path, "ab") as log_handle:
        subprocess.Popen(
            list(spec["command"]),
            cwd=str(config.base_dir),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )


def _safe_health(base_url: str) -> dict[str, object]:
    try:
        return _api_request(base_url, "/health")
    except SystemExit as exc:
        return {"ok": False, "error": str(exc), "url": base_url}


def _health_ok(health_url: str) -> bool:
    try:
        with request.urlopen(health_url, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (error.URLError, error.HTTPError, json.JSONDecodeError):
        return False
    return bool(payload.get("ok"))


def _wait_for_health(health_url: str, timeout_seconds: int) -> bool:
    deadline = monotonic() + timeout_seconds
    while monotonic() < deadline:
        if _health_ok(health_url):
            return True
        sleep(0.5)
    return _health_ok(health_url)


def _tail_file(path: str, max_lines: int = 40) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            lines = handle.readlines()
    except OSError:
        return "No log output was captured."
    tail = "".join(lines[-max_lines:]).strip()
    return tail or "No log output was captured."


def _stop_existing_service(config: Config) -> None:
    pid = _find_listener_pid(config.port)
    if pid is None:
        return

    if _request_shutdown(config):
        if _wait_for_port_state(config.host, config.port, should_be_open=False, timeout_seconds=10):
            print(f"stopped existing service on port {config.port}")
            return

    try:
        os.kill(pid, signal.SIGTERM)
    except PermissionError as exc:
        raise SystemExit(f"Unable to stop existing process {pid} on port {config.port}: {exc}") from exc

    if _wait_for_port_state(config.host, config.port, should_be_open=False, timeout_seconds=5):
        print(f"stopped existing service process {pid} on port {config.port}")
        return

    try:
        os.kill(pid, signal.SIGKILL)
    except PermissionError as exc:
        raise SystemExit(
            f"Existing process {pid} did not stop after SIGTERM, and SIGKILL was not permitted: {exc}"
        ) from exc

    if not _wait_for_port_state(config.host, config.port, should_be_open=False, timeout_seconds=5):
        raise SystemExit(f"Existing process {pid} did not release port {config.port} after SIGKILL.")
    print(f"killed existing service process {pid} on port {config.port}")


def _request_shutdown(config: Config) -> bool:
    base_url = default_api_url(config)
    req = request.Request(
        f"{base_url.rstrip('/')}" + "/shutdown",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=5):
            return True
    except error.HTTPError:
        return False
    except error.URLError:
        return False


def _request_shutdown_url(url: str) -> bool:
    req = request.Request(
        url,
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=5):
            return True
    except (error.HTTPError, error.URLError):
        return False


def _stop_component(config: Config, spec: dict[str, object], force: bool) -> bool:
    health_url = str(spec["health_url"])
    shutdown_url = str(spec["shutdown_url"])
    port = int(spec["port"])
    if _request_shutdown_url(shutdown_url):
        if _wait_for_port_state("127.0.0.1", port, should_be_open=False, timeout_seconds=10):
            print(f"stopped {spec['name']}")
            return True

    pid = _find_listener_pid(port)
    if pid is None:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except PermissionError as exc:
        raise SystemExit(f"Unable to stop existing process {pid} on port {port}: {exc}") from exc

    if _wait_for_port_state("127.0.0.1", port, should_be_open=False, timeout_seconds=5):
        print(f"stopped {spec['name']} process {pid}")
        return True

    if force:
        os.kill(pid, signal.SIGKILL)
        if _wait_for_port_state("127.0.0.1", port, should_be_open=False, timeout_seconds=5):
            print(f"killed {spec['name']} process {pid}")
            return True
    raise SystemExit(
        f"{spec['name']} process {pid} did not release port {port}. Health endpoint: {health_url}"
    )


def _find_listener_pid(port: int) -> int | None:
    result = subprocess.run(
        ["ss", "-ltnp", f"( sport = :{port} )"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None

    match = re.search(r'\("([^\"]+)",pid=(\d+),', result.stdout)
    if not match:
        return None

    process_name = match.group(1)
    pid = int(match.group(2))
    if process_name not in {"hive-rage", "python3", "hive-rag"}:
        return None
    return pid


def _is_port_open(host: str, port: int) -> bool:
    target_host = "127.0.0.1" if host == "0.0.0.0" else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        return sock.connect_ex((target_host, port)) == 0


def _wait_for_port_state(host: str, port: int, should_be_open: bool, timeout_seconds: float) -> bool:
    deadline = monotonic() + timeout_seconds
    while monotonic() < deadline:
        if _is_port_open(host, port) == should_be_open:
            return True
        sleep(0.2)
    return _is_port_open(host, port) == should_be_open


def _api_request(base_url: str, path: str, payload: dict[str, object] | None = None) -> dict[str, object]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = request.Request(
        f"{base_url.rstrip('/')}" + path,
        data=body,
        headers={"Content-Type": "application/json"},
        method="GET" if payload is None else "POST",
    )
    try:
        with request.urlopen(req, timeout=300) as response:
            decoded = json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"API request failed: HTTP {exc.code}: {detail}") from exc
    except error.URLError as exc:
        raise SystemExit(f"Unable to reach service at {base_url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise SystemExit(
            f"Request to {base_url}{path} timed out. The service is still running, but the operation took too long to answer synchronously."
        ) from exc
    except socket.timeout as exc:
        raise SystemExit(
            f"Request to {base_url}{path} timed out. The service is still running, but the operation took too long to answer synchronously."
        ) from exc
    if decoded.get("ok") is False:
        detail = decoded.get("error", {})
        raise SystemExit(f"API request failed: {detail.get('message', 'unknown error')} ({detail.get('code', 'error')})")
    return decoded


def print_status(payload: dict[str, object]) -> None:
    service = payload["service"]
    index = payload["index"]
    health = payload.get("health", {})
    ingestion = payload.get("ingestion", {})
    progress = index.get("scan_progress", {})
    print(f"service: {service['host']}:{service['port']}")
    print(f"service healthy: {health.get('ok')}")
    if index.get("base_dir"):
        print(f"base dir: {index['base_dir']}")
    print(f"hive dir: {index.get('hive_dir', 'unknown')}")
    if index.get("db_path"):
        print(f"db path: {index['db_path']}")
    if index.get("include_globs"):
        print(f"include globs: {', '.join(index['include_globs'])}")
    if index.get("exclude_globs"):
        print(f"exclude globs: {', '.join(index['exclude_globs'])}")
    print(f"chat model: {service['chat_model']}")
    print(f"embedding model: {service['embedding_model']}")
    print(f"tracked files: {index['tracked_files']}")
    print(f"tracked chunks: {index['tracked_chunks']}")
    if ingestion:
        print(f"ingestion healthy: {ingestion.get('ok')}")
    if progress:
        print(f"currently scanning: {progress.get('currently_scanning')}" )
        print(f"files seen: {progress.get('files_seen', 0)}")
        print(f"files indexed this pass: {progress.get('files_indexed', 0)}")
        print(f"files failed this pass: {progress.get('files_failed', 0)}")
        print(f"files skipped this pass: {progress.get('files_skipped', 0)}")
        print(f"files removed this pass: {progress.get('files_removed', 0)}")
        if progress.get('current_path'):
            print(f"current path: {progress['current_path']}")
    if index.get("last_error"):
        print(f"last error: {index['last_error']}")


def print_stack_status(payload: dict[str, object]) -> None:
    for name in ("ingestion", "backend", "frontend"):
        status = payload.get(name, {})
        print(f"{name}: ok={status.get('ok')} url={status.get('url', 'n/a')}")


if __name__ == "__main__":
    sys.exit(main())
