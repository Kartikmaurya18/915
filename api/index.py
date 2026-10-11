"""Vercel Serverless Function entrypoint for Live Market Data API."""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure the repository root is on sys.path so 'src' packages can be imported on Vercel
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.api.server import app

__all__ = ["app"]
