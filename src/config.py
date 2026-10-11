from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    root_dir = Path(__file__).resolve().parent.parent
    for name in [".env", ".env.example"]:
        env_file = root_dir / name
        if env_file.exists():
            load_dotenv(env_file)
    cwd_env = Path.cwd() / ".env"
    if cwd_env.exists():
        load_dotenv(cwd_env)

    if path is None:
        path = Path(__file__).resolve().parent.parent / "config.yaml"
    with open(path, encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    if not isinstance(config, dict):
        raise ValueError("Configuration must contain a YAML mapping.")

    # Override with environment variables if present
    # MOCK/Replay expiry: an explicit pin (e.g. "28-Aug-2026"), or None/unset
    # to auto-compute the next weekly expiry — see resolve_mock_expiry(). Not
    # used by Dhan, which resolves the real expiry from its own API.
    config["expiry"] = os.getenv("EXPIRY", config.get("expiry")) or None
    config["underlying"] = os.getenv("UNDERLYING", config.get("underlying", "NIFTY"))
    # Market data provider — the ONE switch between MOCK and DHAN (see
    # src/data_sources/registry.py). MARKET_DATA_PROVIDER / market_data_provider
    # is the primary name; DATA_SOURCE / data_source is the legacy alias.
    # Both keys are kept equal so existing readers of "data_source" work.
    provider = (
        os.getenv("MARKET_DATA_PROVIDER")
        or os.getenv("DATA_SOURCE")
        or config.get("market_data_provider")
        or config.get("data_source")
        or "mock"
    )
    config["market_data_provider"] = config["data_source"] = str(provider).lower()
    # Where Greeks come from (see src/option_chain/greeks.py). Only
    # "provider" exists today; an internal calculator is a future addition.
    config["greeks_source"] = os.getenv("GREEKS_SOURCE", config.get("greeks_source", "provider"))
    # Order execution backend (see src/execution/). Only "paper" exists —
    # live broker execution is deliberately not implemented.
    config["execution_mode"] = os.getenv("EXECUTION_MODE", config.get("execution_mode", "paper"))
    config["poll_interval_seconds"] = float(
        os.getenv("POLL_INTERVAL_SECONDS", config.get("poll_interval_seconds", 5))
    )
    config["price_calculator"] = os.getenv(
        "PRICE_CALCULATOR", config.get("price_calculator", "mock")
    )
    config["log_level"] = os.getenv("LOG_LEVEL", config.get("log_level", "INFO"))
    config["timezone"] = os.getenv("TIMEZONE", config.get("timezone", "Asia/Kolkata"))
    config["session_start"] = os.getenv("SESSION_START", config.get("session_start", "09:15"))
    config["session_end"] = os.getenv("SESSION_END", config.get("session_end", "15:30"))

    # ATM selection method: "delta" (Greeks-based, default) or "price" (nearest to spot).
    config["atm_method"] = os.getenv("ATM_METHOD", config.get("atm_method", "delta"))
    config["delta_threshold"] = float(
        os.getenv("DELTA_THRESHOLD", config.get("delta_threshold", 0.5))
    )
    config["candle_price_basis"] = os.getenv(
        "CANDLE_PRICE_BASIS", config.get("candle_price_basis", "underlying")
    )
    config["replay_sequence"] = os.getenv(
        "REPLAY_SEQUENCE", config.get("replay_sequence", "trend_up")
    )
    config["replay_speed"] = float(os.getenv("REPLAY_SPEED", config.get("replay_speed", 1.0)))

    # Parse candle intervals from environment if provided
    if env_interval := os.getenv("CANDLE_INTERVALS"):
        try:
            config["candle_intervals"] = [int(x.strip()) for x in env_interval.split(",")]
        except ValueError:
            pass  # Keep default from YAML if parsing fails

    # Paper trading (simulation-only) risk config — temporary defaults
    # TP=10%, SL=5%, overridable via config.yaml, env vars, or the API.
    paper_cfg = dict(config.get("paper_trading") or {})
    paper_cfg["take_profit_percent"] = float(
        os.getenv("PAPER_TAKE_PROFIT_PERCENT", paper_cfg.get("take_profit_percent", 10.0))
    )
    paper_cfg["stop_loss_percent"] = float(
        os.getenv("PAPER_STOP_LOSS_PERCENT", paper_cfg.get("stop_loss_percent", 5.0))
    )
    paper_cfg["default_quantity"] = int(
        os.getenv("PAPER_DEFAULT_QUANTITY", paper_cfg.get("default_quantity", 50))
    )
    # ₹ (absolute P&L) Profit Target / Loss Limit — an additional, optional
    # exit trigger alongside the percent ones above. Left unset (None) by
    # default so existing percent-only behavior is unchanged; never a
    # hard-coded ₹10/₹5, always read from config.yaml/env/the API.
    tp_amount = os.getenv("PAPER_TAKE_PROFIT_AMOUNT", paper_cfg.get("take_profit_amount"))
    sl_amount = os.getenv("PAPER_STOP_LOSS_AMOUNT", paper_cfg.get("stop_loss_amount"))
    paper_cfg["take_profit_amount"] = float(tp_amount) if tp_amount is not None else None
    paper_cfg["stop_loss_amount"] = float(sl_amount) if sl_amount is not None else None
    config["paper_trading"] = paper_cfg

    # Stale-data detection threshold.
    if env_stale := os.getenv("STALE_THRESHOLD_SECONDS"):
        try:
            config["stale_threshold_seconds"] = float(env_stale)
        except ValueError:
            pass  # Keep YAML default (or the engine's own fallback) if parsing fails

    # Persistence (SQLite) for completed candles and option chain/ATM snapshots.
    persistence_cfg = dict(config.get("persistence") or {})
    if env_persist_enabled := os.getenv("PERSISTENCE_ENABLED"):
        persistence_cfg["enabled"] = env_persist_enabled.strip().lower() in ("1", "true", "yes")
    else:
        persistence_cfg.setdefault("enabled", False)

    # In serverless environments like Vercel Lambda, the deployment directory is read-only.
    # Default SQLite database path to /tmp/market_data.db if running on Vercel and no override is set.
    default_db_path = persistence_cfg.get("db_path", "data/market_data.db")
    if os.getenv("VERCEL") or os.getenv("AWS_LAMBDA_FUNCTION_NAME"):
        default_db_path = "/tmp/market_data.db"
    persistence_cfg["db_path"] = os.getenv("PERSISTENCE_DB_PATH", default_db_path)

    # Committed seed copy of historical MOCK data, restored into `db_path` on
    # startup only if `db_path` doesn't exist yet (e.g. a fresh ephemeral
    # container on a host with no persistent disk) — see
    # src/persistence.py::ensure_seeded(). Never touches an already-existing
    # db_path (localhost's own accumulated history is left alone). Set to a
    # blank/empty value to disable seeding entirely.
    seed_path = persistence_cfg.get("seed_db_path", "data/seed/market_data.seed.db")
    if seed_path and not Path(seed_path).is_absolute() and (root_dir / seed_path).exists():
        seed_path = str(root_dir / seed_path)
    persistence_cfg["seed_db_path"] = os.getenv("PERSISTENCE_SEED_DB_PATH", seed_path)

    persistence_cfg["option_chain_snapshot_interval_seconds"] = float(
        os.getenv(
            "PERSISTENCE_CHAIN_INTERVAL_SECONDS",
            persistence_cfg.get("option_chain_snapshot_interval_seconds", 5),
        )
    )
    config["persistence"] = persistence_cfg

    return config
