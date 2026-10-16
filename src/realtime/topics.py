"""Topic model: how one engine snapshot is split into independently subscribable topics.

    index:NIFTY          price, ATM, calculated prices, provider/status, feed quality
    options:NIFTY        the option chain
    candles:NIFTY:1m     one candle interval (active candle, completed, series)
    rrg:NIFTY            relative rotation graph (null unless enabled)

A client subscribes only to what its page shows. The private `paper_trading` block
is never a topic: per-user state is fetched over authenticated REST.
"""

from __future__ import annotations

import os
import re
from typing import Any

PRIVATE_KEYS = frozenset({"paper_trading"})
_INDEX_EXCLUDED = PRIVATE_KEYS | {"option_chain", "candles", "newly_completed", "rrg"}

_SIMPLE = re.compile(r"(index|options|rrg):([A-Z0-9]{1,16})")
_CANDLES = re.compile(r"candles:([A-Z0-9]{1,16}):([0-9]{1,3})m")


class TopicError(ValueError):
    pass


def supported_underlyings() -> frozenset[str]:
    raw = os.getenv("SUPPORTED_UNDERLYINGS", "NIFTY,BANKNIFTY,FINNIFTY")
    return frozenset(u.strip().upper() for u in raw.split(",") if u.strip())


def parse_topic(topic: Any) -> tuple[str, str, int | None]:
    """(kind, underlying, interval_minutes). Raises TopicError for anything malformed
    or for an underlying this deployment does not serve."""
    if not isinstance(topic, str) or len(topic) > 40:
        raise TopicError("topic must be a short string")
    match = _SIMPLE.fullmatch(topic)  # fullmatch: `$` would accept a trailing newline
    if match:
        kind, underlying, interval = match.group(1), match.group(2), None
    else:
        match = _CANDLES.fullmatch(topic)
        if not match:
            raise TopicError(f"unknown topic {topic!r}")
        kind, underlying, interval = "candles", match.group(1), int(match.group(2))
        if interval <= 0:
            raise TopicError("candle interval must be positive")
    if underlying not in supported_underlyings():
        raise TopicError(f"unsupported underlying {underlying!r}")
    return kind, underlying, interval


def index_topic(underlying: str) -> str:
    return f"index:{underlying}"


def options_topic(underlying: str) -> str:
    return f"options:{underlying}"


def rrg_topic(underlying: str) -> str:
    return f"rrg:{underlying}"


def candles_topic(underlying: str, interval: int) -> str:
    return f"candles:{underlying}:{interval}m"


def split_snapshot(snapshot: dict, underlying: str) -> dict[str, Any]:
    """Break an engine snapshot into {topic: payload}. Nothing private is included."""
    topics: dict[str, Any] = {
        index_topic(underlying): {k: v for k, v in snapshot.items() if k not in _INDEX_EXCLUDED},
    }
    if snapshot.get("option_chain") is not None:
        topics[options_topic(underlying)] = snapshot["option_chain"]
    newly = snapshot.get("newly_completed") or []
    for interval, block in (snapshot.get("candles") or {}).items():
        minutes = int(interval)
        payload = dict(block)
        payload["newly_completed"] = [c for c in newly if c.get("interval_minutes") == minutes]
        topics[candles_topic(underlying, minutes)] = payload
    if snapshot.get("rrg") is not None:
        topics[rrg_topic(underlying)] = {"rrg": snapshot["rrg"]}
    return topics


def merge_topics(states: dict[str, Any], underlying: str) -> dict:
    """Rebuild the legacy single-blob snapshot from topic payloads (for /api/snapshot
    when this process does not own the engine)."""
    snapshot: dict[str, Any] = dict(states.get(index_topic(underlying)) or {})
    if options_topic(underlying) in states:
        snapshot["option_chain"] = states[options_topic(underlying)]
    candles: dict[str, Any] = {}
    newly: list = []
    for topic, payload in states.items():
        match = _CANDLES.fullmatch(topic)
        if match and match.group(1) == underlying:
            block = dict(payload)
            newly.extend(block.pop("newly_completed", []))
            candles[str(int(match.group(2)))] = block
    if candles:
        snapshot["candles"] = candles
        snapshot["newly_completed"] = newly
    rrg = states.get(rrg_topic(underlying))
    snapshot["rrg"] = rrg.get("rrg") if isinstance(rrg, dict) else None
    return snapshot
