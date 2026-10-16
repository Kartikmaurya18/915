"""The React shell is served for app routes and never for backend paths."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.spa import CONTENT_SECURITY_POLICY, install_spa, spa_dist


@pytest.fixture()
def dist(tmp_path):
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<!doctype html><div id=root></div>", encoding="utf-8")
    (tmp_path / "assets" / "app-abc123.js").write_text("console.log(1)", encoding="utf-8")
    (tmp_path / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    return tmp_path


@pytest.fixture()
def client(dist):
    app = FastAPI()

    @app.get("/api/ping")
    def ping():
        return {"ok": True}

    install_spa(app, dist)  # registered last, like in server.py, so real routes win
    return TestClient(app)


def test_root_and_client_side_routes_return_the_shell(client):
    for path in ("/", "/trading", "/login", "/some/deep/route"):
        res = client.get(path)
        assert res.status_code == 200, path
        assert "id=root" in res.text
        assert res.headers["cache-control"] == "no-cache"


def test_shell_carries_a_restrictive_csp(client):
    csp = client.get("/").headers["content-security-policy"]
    assert csp == CONTENT_SECURITY_POLICY
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert "unsafe-eval" not in csp and "script-src 'self' 'unsafe-inline'" not in csp


def test_real_api_routes_still_win(client):
    assert client.get("/api/ping").json() == {"ok": True}


def test_unknown_backend_paths_are_404_not_html(client):
    for path in ("/api/nope", "/api/auth/whatever", "/ws/v1", "/metrics", "/assets/missing.js", "/static/x.css"):
        res = client.get(path)
        assert res.status_code == 404, path
        assert "id=root" not in res.text, path


def test_assets_and_root_files_are_served(client):
    assert client.get("/assets/app-abc123.js").text == "console.log(1)"
    assert client.get("/favicon.svg").text == "<svg/>"


def test_missing_file_with_extension_is_404(client):
    assert client.get("/robots.txt").status_code == 404


def test_path_traversal_cannot_leave_the_build_directory(client, dist):
    (dist.parent / "secret.txt").write_text("secret", encoding="utf-8")
    for path in ("/../secret.txt", "/%2e%2e/secret.txt", "/..%2fsecret.txt", "/assets/../../secret.txt"):
        res = client.get(path)
        assert "secret" not in res.text, path


def test_spa_dist_detects_a_build_and_honours_the_legacy_switch(tmp_path, monkeypatch):
    monkeypatch.delenv("FRONTEND", raising=False)
    assert spa_dist(tmp_path) is None  # nothing built
    build = tmp_path / "web" / "dist"
    build.mkdir(parents=True)
    (build / "index.html").write_text("x", encoding="utf-8")
    assert spa_dist(tmp_path) == build
    monkeypatch.setenv("FRONTEND", "legacy")
    assert spa_dist(tmp_path) is None
