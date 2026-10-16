"""Serve the built React app (web/dist) with client-side routing.

    cd web && npm ci && npm run build

When `web/dist/index.html` exists, `/` and every non-API path return the SPA shell and the hashed
bundles are served from `/assets` with a one-year immutable cache. Set FRONTEND=legacy to keep
serving the original single-file UI from `public/` (an instant rollback that needs no rebuild).

The SPA shell carries a Content-Security-Policy; the legacy UI does not (it loads a CDN script).
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

# Paths that belong to the backend. An unknown path under one of them is a real 404, never the SPA
# shell: a mistyped /api/... URL must not answer 200 with HTML.
BACKEND_PREFIXES = frozenset({"api", "ws", "metrics", "assets", "static", "docs", "redoc", "openapi.json"})

# 'unsafe-inline' for styles only: React sets style attributes and the chart library injects styles.
# No inline or remote scripts are allowed. ws:/wss: are listed explicitly because Safari's 'self'
# does not cover WebSocket connections to the same host.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "connect-src 'self' ws: wss:; font-src 'self'; object-src 'none'; base-uri 'self'; "
    "form-action 'self'; frame-ancestors 'none'"
)


def spa_dist(root: Path) -> Path | None:
    """The build directory to serve, or None when the SPA is absent or switched off."""
    if os.getenv("FRONTEND", "spa").strip().lower() == "legacy":
        return None
    dist = root / "web" / "dist"
    return dist if (dist / "index.html").is_file() else None


def install_spa(app: FastAPI, dist: Path) -> None:
    index = dist / "index.html"
    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="spa-assets")

    def shell() -> FileResponse:
        return FileResponse(index, headers={"Content-Security-Policy": CONTENT_SECURITY_POLICY, "Cache-Control": "no-cache"})

    @app.get("/", include_in_schema=False)
    async def spa_root() -> FileResponse:
        return shell()

    @app.get("/{path:path}", include_in_schema=False)
    async def spa_fallback(path: str) -> FileResponse:
        first = path.split("/", 1)[0]
        if first in BACKEND_PREFIXES:
            raise HTTPException(status_code=404, detail="Not found")
        candidate = (dist / path).resolve()
        if dist.resolve() in candidate.parents and candidate.is_file():
            return FileResponse(candidate)  # e.g. /favicon.ico placed in web/public
        if "." in path.rsplit("/", 1)[-1]:
            raise HTTPException(status_code=404, detail="Not found")  # a missing file, not an app route
        return shell()
