"""Tests verifying Vercel deployment configuration, entrypoint, routing, and endpoint availability."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import time
from pathlib import Path
from fastapi import FastAPI
import pytest
import requests
import uvicorn


def get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_vercel_adapter_exposes_existing_fastapi_app():
    """Verify that api/index.py exposes the exact existing FastAPI app from src.api.server."""
    import api.index as vercel_entrypoint
    from src.api.server import app as real_app

    assert hasattr(vercel_entrypoint, "app"), "api/index.py must expose top-level 'app'"
    assert isinstance(vercel_entrypoint.app, FastAPI), "app must be a FastAPI instance"
    assert vercel_entrypoint.app is real_app, "api/index.py must use the existing FastAPI app instance, not create a duplicate"


def test_src_main_kept_for_local_development():
    """Verify src/main.py is preserved as local CLI runner and satisfies top-level 'app' requirement."""
    import src.main as local_main
    from src.api.server import app as real_app

    assert hasattr(local_main, "main"), "src/main.py must retain its CLI entrypoint"
    assert hasattr(local_main, "run"), "src/main.py must retain its local runner"
    assert hasattr(local_main, "app"), "src/main.py must expose top-level 'app' instance"
    assert local_main.app is real_app, "src/main.py app must be the exact existing FastAPI instance"


def test_vercel_configuration_files_exist():
    """Verify configuration files prevent Vercel from mistaking src/main.py and set api/index.py."""
    root = Path(__file__).resolve().parent.parent

    # .vercelignore
    vercelignore = root / ".vercelignore"
    assert vercelignore.exists(), ".vercelignore must exist"
    content = vercelignore.read_text(encoding="utf-8")
    assert "src/main.py" in content, ".vercelignore must ignore src/main.py"

    # vercel.json
    vercel_json = root / "vercel.json"
    assert vercel_json.exists(), "vercel.json must exist"
    v_content = vercel_json.read_text(encoding="utf-8")
    assert "api/index.py" in v_content, "vercel.json must route to api/index.py"

    # pyproject.toml
    pyproject = root / "pyproject.toml"
    assert pyproject.exists(), "pyproject.toml must exist"
    p_content = pyproject.read_text(encoding="utf-8")
    assert 'entrypoint = "api.index:app"' in p_content, "pyproject.toml must declare api.index:app as entrypoint"


def test_fastapi_endpoints_reachable_via_vercel_entrypoint():
    """Run uvicorn against 'api.index:app' and test reachable endpoints with requests."""
    port = get_free_port()
    config = uvicorn.Config("api.index:app", host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"

    # Wait for server to start
    for _ in range(50):
        try:
            res = requests.get(f"{base_url}/api/health", timeout=1)
            if res.status_code == 200:
                break
        except Exception:
            time.sleep(0.1)
    else:
        pytest.fail("Server did not start in time")

    try:
        # 1. Health check
        res = requests.get(f"{base_url}/api/health", timeout=3)
        assert res.status_code == 200
        data = res.json()
        assert data.get("status") == "ok"
        assert data.get("engine_running") is True

        # 2. Config endpoint
        res = requests.get(f"{base_url}/api/config", timeout=3)
        assert res.status_code == 200
        cfg = res.json()
        assert cfg.get("underlying") == "NIFTY"
        assert cfg.get("data_source") in ("mock", "nse", "dhan")

        # 3. Snapshot endpoint
        res = requests.get(f"{base_url}/api/snapshot", timeout=3)
        assert res.status_code == 200
        snap = res.json()
        assert "underlying" in snap
        assert "candles" in snap
        assert "paper_trading" in snap

        # 4. Settings endpoint
        res = requests.get(f"{base_url}/api/settings", timeout=3)
        assert res.status_code == 200

        # 5. Logs endpoint
        res = requests.get(f"{base_url}/api/logs", timeout=3)
        assert res.status_code == 200
        assert "logs" in res.json()

        # 6. Paper trading state
        res = requests.get(f"{base_url}/api/paper/state", timeout=3)
        assert res.status_code == 200
        assert "open_positions" in res.json()

        # 7. Paper trading open and close simulation
        open_res = requests.post(
            f"{base_url}/api/paper/open",
            json={"option_type": "CE", "strike": 24850.0, "quantity": 50, "side": "BUY"},
            timeout=3,
        )
        assert open_res.status_code == 200
        pos = open_res.json()
        assert pos.get("option_type") == "CE"
        pos_id = pos.get("id")

        close_res = requests.post(f"{base_url}/api/paper/close/{pos_id}", timeout=3)
        assert close_res.status_code == 200
        assert close_res.json().get("status") == "EXITED"

        # 8. History dates
        res = requests.get(f"{base_url}/api/history/dates", timeout=3)
        assert res.status_code == 200
        assert "dates" in res.json()
        assert "today" in res.json()

        # 9. Frontend root route
        res = requests.get(f"{base_url}/", timeout=3)
        assert res.status_code == 200
        assert "text/html" in res.headers.get("content-type", "")
        # The legacy page, or the React shell when web/dist has been built (see src/api/spa.py).
        assert "NIFTY Option Terminal" in res.text or '<div id="root">' in res.text

        # 10. Frontend static asset routes
        res = requests.get(f"{base_url}/static/css/styles.css", timeout=3)
        assert res.status_code == 200
        assert res.status_code == 200

        res = requests.get(f"{base_url}/static/js/app.js", timeout=3)
        assert res.status_code == 200
        assert "connectWebSocket" in res.text

    finally:
        server.should_exit = True
        thread.join(timeout=3)


def test_vercel_environment_persistence_fallback(monkeypatch):
    """Verify that when VERCEL=1 is set, persistence defaults to /tmp and seeds correctly."""
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.delenv("PERSISTENCE_DB_PATH", raising=False)

    from src.config import load_config
    cfg = load_config()
    db_path = cfg.get("persistence", {}).get("db_path")
    assert db_path == "/tmp/market_data.db", f"Expected /tmp/market_data.db under VERCEL=1, got {db_path}"
