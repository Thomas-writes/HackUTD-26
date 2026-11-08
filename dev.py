#!/usr/bin/env python3
"""Run the five-terminal WriteOrWrong phone demo from one terminal."""

from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import resource
import shutil
import signal
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
TELEMETRY = ROOT / "telemetry"
WEB = ROOT / "my-spacetime-app"
PYTHON = TELEMETRY / ".venv" / "bin" / "python"
MODEL = TELEMETRY / "model.joblib"
HOST = "127.0.0.1"
DATABASE = "ssd-monitor"
DB_PORT = 3000
API_PORT = 8000
LOCAL_PORT = 5173
SHARE_PORT = 4173
MIN_OPEN_FILES = 1024


def local_url(port: int, path: str = "") -> str:
    return f"http://{HOST}:{port}{path}"


def find_spacetime() -> str | None:
    installed = shutil.which("spacetime")
    if installed:
        return installed
    for directory in (".local/bin", ".spacetime/bin"):
        path = Path.home() / directory / "spacetime"
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def reachable(url: str) -> bool:
    try:
        with urlopen(url, timeout=0.5) as response:
            return response.status == 200
    except (URLError, TimeoutError):
        return False


def port_in_use(port: int) -> bool:
    try:
        with socket.create_connection((HOST, port), timeout=0.5):
            return True
    except ConnectionRefusedError:
        return False


def ensure_socket_capacity() -> None:
    """Give the API room for both incoming and forwarded telemetry connections."""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft == resource.RLIM_INFINITY or soft >= MIN_OPEN_FILES:
        return
    if hard != resource.RLIM_INFINITY and hard < MIN_OPEN_FILES:
        raise RuntimeError(
            f"The demo needs an open-file limit of at least {MIN_OPEN_FILES}. This shell allows {hard}."
        )
    resource.setrlimit(resource.RLIMIT_NOFILE, (MIN_OPEN_FILES, hard))


def stop_service(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def start_service(
    stack: ExitStack, name: str, command: list, directory: Path, env: dict[str, str],
    readiness_url: str | None = None,
) -> subprocess.Popen:
    print(f"Starting {name}", flush=True)
    process = subprocess.Popen(command, cwd=directory, env=env, start_new_session=True)
    stack.callback(stop_service, process)
    if readiness_url:
        deadline = time.monotonic() + 30
        while not reachable(readiness_url):
            if process.poll() is not None:
                raise RuntimeError(f"{name} exited with status {process.returncode}")
            if time.monotonic() >= deadline:
                raise RuntimeError(f"{name} did not become ready at {readiness_url}")
            time.sleep(0.2)
    return process


def create_writer() -> tuple[str, str]:
    """Give the local telemetry API the only identity allowed to ingest SSD data."""
    request = Request(local_url(DB_PORT, "/v1/identity"), method="POST")
    with urlopen(request, timeout=5) as response:
        writer = json.load(response)
    identity, token = writer.get("identity"), writer.get("token")
    if not identity or not token:
        raise RuntimeError("SpacetimeDB did not return a telemetry writer identity")
    return identity, token


def preview_config(directory: Path) -> Path:
    """Configure both dashboards without editing the checked-in web app."""
    read_routes = {
        rf"^/v1/database/{DATABASE}/subscribe(?:\?|$)": {"target": local_url(DB_PORT), "ws": True},
        r"^/v1/identity/websocket-token(?:\?|$)": {"target": local_url(DB_PORT)},
        r"^/(?:healthz|demo/(?:fleet-size|action-status))(?:\?|$)": {"target": local_url(API_PORT)},
    }
    config = {
        "esbuild": {"jsx": "automatic"},
        "define": {
            "import.meta.env.VITE_SPACETIMEDB_HOST": "window.location.origin",
            "import.meta.env.VITE_SPACETIMEDB_DB_NAME": json.dumps(DATABASE),
        },
        "server": {"proxy": {**read_routes, r"^/demo/(?:fleet|actions|mode)(?:\?|$)": {"target": local_url(API_PORT)}}},
        "preview": {"proxy": read_routes},
    }
    # Reuse the app's existing node filter, with a read-only fleet size that refreshes in both views.
    fleet_effect = """    if (resizing) return;
    const controller = new AbortController();
    let timer: number | undefined;
    async function refreshFleet() {
      try {
        const response = await fetch('/demo/fleet-size', { signal: controller.signal, cache: 'no-store' });
        if (response.ok) {
          const settings = await response.json();
          if (!controller.signal.aborted) setDemoFleet(settings);
        }
      } catch {
        // Keep the last known size until the next successful refresh.
      }
      if (!controller.signal.aborted) timer = window.setTimeout(refreshFleet, 2000);
    }
    void refreshFleet();
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [resizing]);
"""
    app_source = json.dumps(str(WEB / "src" / "App.tsx"))
    adapter_path = json.dumps(str(ROOT / "demo-dashboard.mjs"))
    path = directory / "preview.mjs"
    path.write_text(
        f"import demoDashboard from {adapter_path};\n"
        f"const config = {json.dumps(config)};\n"
        f"config.plugins = [demoDashboard({app_source}, {json.dumps(fleet_effect)})];\n"
        "export default config;\n",
        encoding="utf-8",
    )
    return path


def run_demo(spacetime: str, cloudflared: str) -> None:
    env = os.environ.copy()
    for name in (
        "SPACETIME_REDUCER_URL", "SPACETIME_TOKEN", "SSD_WRITER_IDENTITY", "TELEMETRY_TOKEN",
        "TELEMETRY_NODE_TOKENS", "TELEMETRY_DEMO_NODES",
    ):
        env.pop(name, None)
    model = Path(env.get("TELEMETRY_MODEL_PATH", MODEL)).expanduser().resolve()
    if not PYTHON.is_file() or not model.is_file():
        raise RuntimeError("Prepare telemetry/.venv and the trained model using the README first.")

    vite = WEB / "node_modules" / ".bin" / "vite"
    if not vite.is_file():
        raise RuntimeError("Install the dashboard dependencies using the README first.")

    for port in (DB_PORT, API_PORT, SHARE_PORT):
        if port_in_use(port):
            raise RuntimeError(f"Port {port} is already in use. Stop the previous run first.")

    local_port = next((port for port in range(LOCAL_PORT, LOCAL_PORT + 10) if not port_in_use(port)), None)
    if local_port is None:
        raise RuntimeError(f"No free local dashboard port in {LOCAL_PORT}–{LOCAL_PORT + 9}.")

    ensure_socket_capacity()
    api = local_url(API_PORT)
    preview = local_url(SHARE_PORT)
    local = local_url(local_port)
    with ExitStack() as stack:
        run_dir = Path(stack.enter_context(TemporaryDirectory(prefix="writeorwrong-")))
        data_dir = run_dir / "database"
        database = start_service(
            stack, "SpacetimeDB",
            [spacetime, "start", "--listen-addr", f"{HOST}:{DB_PORT}", "--data-dir", data_dir,
             "--in-memory", "--non-interactive"],
            WEB, env, local_url(DB_PORT, "/v1/ping"),
        )
        writer_identity, writer_token = create_writer()
        subprocess.run(
            [spacetime, "publish", DATABASE, "--module-path", "./spacetimedb",
             "--server", local_url(DB_PORT), "--no-config", "--anonymous",
             "--yes=skip-login", "--delete-data=never"],
            cwd=WEB, env={**env, "SSD_WRITER_IDENTITY": writer_identity}, check=True,
        )
        web_config = preview_config(run_dir)
        build_options = ["--config", web_config, "--outDir", run_dir / "dashboard"]
        subprocess.run([vite, "build", *build_options], cwd=WEB, env=env, check=True)
        dashboard = start_service(
            stack, "Viewing dashboard",
            [vite, "preview", *build_options, "--host", HOST, "--port", str(SHARE_PORT), "--strictPort"],
            WEB, env, preview,
        )
        api_env = {
            **env, "TELEMETRY_MODEL_PATH": str(model), "TELEMETRY_DEMO_NODES": "1000",
            "SPACETIME_TOKEN": writer_token,
            "SPACETIME_REDUCER_URL": local_url(DB_PORT, f"/v1/database/{DATABASE}/call/ingest_ssd_batch"),
        }
        telemetry = start_service(
            stack, "FastAPI",
            [PYTHON, "-m", "uvicorn", "app:app", "--host", HOST, "--port", str(API_PORT),
             "--workers", "1", "--no-access-log"],
            TELEMETRY, api_env, f"{api}/healthz",
        )
        local_dashboard = start_service(
            stack, "Local dashboard",
            [vite, "--config", web_config, "--host", HOST, "--port", str(local_port), "--strictPort"],
            WEB, env, local,
        )
        print(f"Change node count locally at {local}", flush=True)
        print("Open the trycloudflare.com URL printed below to view the fleet on your phone. Ctrl+C stops the demo.", flush=True)
        tunnel = start_service(
            stack, "Cloudflare Tunnel",
            [cloudflared, "tunnel", "--url", preview, "--http-host-header", HOST],
            ROOT, env,
        )
        simulator = start_service(
            stack, "Simulator", [PYTHON, "-u", "simulate.py", "--dashboard-control", "--url", f"{api}/telemetry"],
            TELEMETRY, env,
        )
        services = {
            "SpacetimeDB": database, "Viewing dashboard": dashboard, "FastAPI": telemetry,
            "Local dashboard": local_dashboard, "Cloudflare Tunnel": tunnel, "Simulator": simulator,
        }
        while True:
            for name, process in services.items():
                if process.poll() is not None:
                    raise RuntimeError(f"{name} exited with status {process.returncode}")
            time.sleep(0.25)


def terminate(_signum, _frame) -> None:
    raise SystemExit(143)


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()
    spacetime = find_spacetime()
    cloudflared = shutil.which("cloudflared")
    if not spacetime or not cloudflared:
        print("WriteOrWrong: Install SpacetimeDB and cloudflared before starting the demo.", file=sys.stderr)
        return 1

    signal.signal(signal.SIGTERM, terminate)
    try:
        run_demo(spacetime, cloudflared)
    except KeyboardInterrupt:
        return 130
    except (RuntimeError, OSError, subprocess.CalledProcessError) as error:
        print(f"WriteOrWrong: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
