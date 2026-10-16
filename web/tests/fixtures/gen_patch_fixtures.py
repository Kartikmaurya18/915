"""Generate cross-language patch fixtures: the REAL Python diff() output for consecutive engine
snapshots of every topic, plus hand-written edge cases. The TypeScript test applies each patch and
must reproduce `new` exactly, which pins the two implementations together.

    python web/tests/fixtures/gen_patch_fixtures.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("MARKET_DATA_PROVIDER", "mock")
os.environ.setdefault("POLL_INTERVAL_SECONDS", "0.2")

from src.config import load_config  # noqa: E402
from src.engine import MarketEngine  # noqa: E402
from src.realtime.patch import apply_patch, diff  # noqa: E402
from src.realtime.topics import split_snapshot  # noqa: E402


def roundtrip(old, new):
    node = diff(old, new)
    if node is not None:
        assert apply_patch(old, node) == new, "python diff/apply disagree"
    return node


def real_cases(ticks: int = 40) -> list[dict]:
    engine = MarketEngine(load_config())
    seen: list[dict] = []
    engine.subscribe(lambda snap: seen.append(json.loads(json.dumps(snap, default=str))))
    engine.start()
    deadline = time.time() + 30
    while len(seen) < ticks and time.time() < deadline:
        time.sleep(0.1)
    engine.close()

    cases, previous = [], {}
    for snap in seen:
        for topic, payload in split_snapshot(snap, engine.underlying).items():
            if topic in previous and previous[topic] != payload:
                cases.append({"name": f"real:{topic}", "old": previous[topic], "new": payload, "patch": roundtrip(previous[topic], payload)})
            previous[topic] = payload
    return cases[:80]


def edge_cases() -> list[dict]:
    chain = lambda *rows: {"strikes": [dict(zip(("strike", "ltp"), r)) for r in rows]}  # noqa: E731
    pairs = {
        "scalar_change": ({"a": 1}, {"a": 2}),
        "string_change": ({"a": "x"}, {"a": "y"}),
        "to_null": ({"a": 1}, {"a": None}),
        "from_null": ({"a": None}, {"a": 5}),
        "bool_vs_int": ({"a": True}, {"a": 1}),
        "int_vs_float_equal": ({"a": 1}, {"a": 1.0}),
        "key_added_and_removed": ({"a": 1, "b": 2}, {"a": 1, "c": 3}),
        "nested_object": ({"a": {"b": {"c": 1}}}, {"a": {"b": {"c": 2}}}),
        "object_to_scalar": ({"a": {"b": 1}}, {"a": 7}),
        "list_of_scalars": ({"a": [1, 2, 3]}, {"a": [1, 2, 4]}),
        "empty_to_filled_list": ({"a": []}, {"a": [{"strike": 1.0}]}),
        "keyed_value_change": (chain((24800.0, 1.0), (24850.0, 2.0)), chain((24800.0, 1.0), (24850.0, 2.5))),
        "keyed_integer_float_keys": (chain((24800, 1.0), (24850, 2.0)), chain((24800.0, 1.0), (24850.0, 3.0))),
        "keyed_add_row": (chain((100.0, 1.0)), chain((100.0, 1.0), (150.0, 2.0))),
        "keyed_remove_row": (chain((100.0, 1.0), (150.0, 2.0)), chain((100.0, 1.0))),
        "keyed_reorder": (chain((100.0, 1.0), (150.0, 2.0), (200.0, 3.0)), chain((200.0, 3.0), (100.0, 1.0), (150.0, 2.0))),
        "keyed_window_shift": (chain((100.0, 1.0), (150.0, 2.0), (200.0, 3.0)), chain((150.0, 2.0), (200.0, 3.5), (250.0, 4.0))),
        "keyed_fractional_keys": (chain((100.5, 1.0), (101.25, 2.0)), chain((100.5, 1.5), (101.25, 2.0))),
        "keyed_string_keys": ({"s": [{"open_time": "t1", "v": 1}]}, {"s": [{"open_time": "t1", "v": 2}, {"open_time": "t2", "v": 3}]}),
        "duplicate_keys_fall_back": ({"a": [{"strike": 1, "v": 1}, {"strike": 1, "v": 2}]}, {"a": [{"strike": 1, "v": 3}, {"strike": 1, "v": 2}]}),
        "identical": ({"a": [1, {"b": 2}]}, {"a": [1, {"b": 2}]}),
    }
    return [{"name": f"edge:{name}", "old": old, "new": new, "patch": roundtrip(old, new)} for name, (old, new) in pairs.items()]


if __name__ == "__main__":
    out = real_cases() + edge_cases()
    path = Path(__file__).with_name("patch_cases.json")
    path.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
    print(f"wrote {len(out)} cases ({path.stat().st_size // 1024} KB) to {path}")
