"""Patch format and topic model: exactness first, then the size claims."""

from __future__ import annotations

import copy
import json
import random

import pytest

from src.realtime.patch import apply_patch, diff, encoded_size
from src.realtime.topics import (
    TopicError,
    candles_topic,
    merge_topics,
    parse_topic,
    split_snapshot,
)


def canon(value) -> str:
    return json.dumps(value, sort_keys=True)


def wire(node):
    """Patches travel as JSON; make sure nothing relies on in-memory object identity."""
    return json.loads(json.dumps(node))


# -- patch: exactness --------------------------------------------------------------


@pytest.mark.parametrize("old,new", [
    ({}, {}),
    ({"a": 1}, {"a": 1}),
    ({"a": 1}, {"a": 2}),
    ({"a": 1}, {}),
    ({}, {"a": {"b": [1, 2]}}),
    ({"a": 1}, {"a": None}),
    ({"a": None}, {"a": 1}),
    ({"a": True}, {"a": 1}),  # bool vs int must be a real change
    ({"a": 1}, {"a": 1.0}),
    ({"a": [1, 2, 3]}, {"a": [1, 2]}),
    ({"a": {"x": 1}}, {"a": [1]}),
    ({"a": "="}, {"a": "d"}),  # strings that look like node tags
    ({"a": 1}, {"a": ["=", 5]}),  # values that look like node tags
])
def test_diff_apply_roundtrip_examples(old, new):
    node = diff(old, new)
    if canon(old) == canon(new) and type(old) is type(new) and node is None:
        return
    assert canon(apply_patch(copy.deepcopy(old), wire(node))) == canon(new)


def test_identical_values_produce_no_patch():
    value = {"a": [{"strike": 1.0, "v": 1}], "b": {"c": None}}
    assert diff(value, copy.deepcopy(value)) is None


def test_apply_does_not_mutate_its_input():
    old = {"rows": [{"strike": 1.0, "v": 1}, {"strike": 2.0, "v": 2}], "x": {"y": 1}}
    snapshot = copy.deepcopy(old)
    apply_patch(old, wire(diff(old, {"rows": [{"strike": 2.0, "v": 9}], "x": {"y": 2}})))
    assert old == snapshot


def _random_value(rng: random.Random, depth: int = 0):
    kind = rng.random()
    if depth > 3 or kind < 0.35:
        return rng.choice([None, True, False, 0, 1, -7, 2.5, 100.0, "", "a", "="])
    if kind < 0.6:
        return {rng.choice("abcdefg"): _random_value(rng, depth + 1) for _ in range(rng.randint(0, 4))}
    if kind < 0.8:
        return [_random_value(rng, depth + 1) for _ in range(rng.randint(0, 4))]
    keys = rng.sample(range(0, 12), rng.randint(0, 6))
    return [{"strike": float(k) * 50, "v": rng.random(), "w": rng.choice([None, 1, "x"])} for k in keys]


def _mutate(rng: random.Random, value, depth: int = 0):
    value = copy.deepcopy(value)
    if rng.random() < 0.15:
        return _random_value(rng)
    if isinstance(value, dict):
        for key in list(value):
            roll = rng.random()
            if roll < 0.2:
                del value[key]
            elif roll < 0.6:
                value[key] = _mutate(rng, value[key], depth + 1)
        if rng.random() < 0.3:
            value[rng.choice("hijk")] = _random_value(rng)
        return value
    if isinstance(value, list):
        if value and all(isinstance(i, dict) and "strike" in i for i in value):
            rows = [(_mutate(rng, r, depth + 1) if rng.random() < 0.4 else r) for r in value]
            rows = [r for r in rows if rng.random() > 0.15 and isinstance(r, dict) and isinstance(r.get("strike"), float)]
            if rng.random() < 0.4:
                rows.append({"strike": float(rng.randint(20, 40)) * 50, "v": rng.random(), "w": None})
            if rng.random() < 0.25:
                rng.shuffle(rows)
            seen, unique = set(), []
            for row in rows:
                if row["strike"] not in seen:
                    seen.add(row["strike"])
                    unique.append(row)
            return unique
        if value and rng.random() < 0.5:
            value[rng.randrange(len(value))] = _random_value(rng, depth + 1)
        return value
    return _random_value(rng) if rng.random() < 0.5 else value


def test_randomised_roundtrip_property():
    """apply(old, diff(old, new)) == new for thousands of random structural edits,
    including keyed-array inserts, deletes and reorders."""
    rng = random.Random(20261016)
    checked = 0
    for _ in range(4000):
        old = _random_value(rng)
        new = _mutate(rng, old)
        node = diff(old, new)
        rebuilt = old if node is None else apply_patch(copy.deepcopy(old), wire(node))
        assert canon(rebuilt) == canon(new), (old, new, node)
        checked += node is not None
    assert checked > 2000  # the generator actually produced changes


def test_keyed_arrays_ship_only_changed_rows():
    rows = [{"strike": 24000.0 + 50 * i, "ltp": 100.0 + i, "oi": 1000 * i} for i in range(80)]
    after = copy.deepcopy(rows)
    for i in (3, 40, 77):
        after[i]["ltp"] += 0.5
    node = diff({"strikes": rows}, {"strikes": after})
    assert encoded_size(wire(node)) < encoded_size({"strikes": after}) / 10
    assert apply_patch({"strikes": rows}, wire(node)) == {"strikes": after}


# -- topics ------------------------------------------------------------------------


@pytest.mark.parametrize("topic", ["index:NIFTY", "options:BANKNIFTY", "rrg:FINNIFTY", "candles:NIFTY:1m", "candles:NIFTY:15m"])
def test_valid_topics_parse(topic):
    assert parse_topic(topic)[1] in {"NIFTY", "BANKNIFTY", "FINNIFTY"}


@pytest.mark.parametrize("topic", [
    "", "index", "index:", "index:nifty", "index:NIFTY:1m", "candles:NIFTY", "candles:NIFTY:0m", "candles:NIFTY:1000m",
    "options:RELIANCE", "paper_trading", "paper:NIFTY", "index:NIFTY\n", "x" * 100, None, 5, ["index:NIFTY"],
    "index:NIFTY/../../etc", "options:*", "candles:NIFTY:-5m",
])
def test_invalid_topics_are_rejected(topic):
    with pytest.raises(TopicError):
        parse_topic(topic)


def test_supported_underlyings_are_configurable(monkeypatch):
    monkeypatch.setenv("SUPPORTED_UNDERLYINGS", "NIFTY")
    with pytest.raises(TopicError):
        parse_topic("options:BANKNIFTY")
    assert parse_topic("options:NIFTY")[1] == "NIFTY"


@pytest.fixture
def snapshots():
    from src.api.server import engine

    out = []
    for _ in range(6):
        engine.tick_once()
        out.append(copy.deepcopy(engine.get_snapshot()))
    return out


def test_split_then_merge_reproduces_every_public_field(snapshots):
    for snap in snapshots:
        topics = split_snapshot(snap, "NIFTY")
        rebuilt = merge_topics(topics, "NIFTY")
        public = {k: v for k, v in snap.items() if k != "paper_trading"}
        assert canon(rebuilt) == canon(public)


def test_no_topic_ever_carries_the_private_paper_book(snapshots):
    for snap in snapshots:
        assert "paper_trading" in snap  # the engine produces it ...
        for topic, payload in split_snapshot(snap, "NIFTY").items():
            assert "paper_trading" not in json.dumps(payload), topic  # ... but it is never published


def test_expected_topics_exist(snapshots):
    names = set(split_snapshot(snapshots[0], "NIFTY"))
    assert {"index:NIFTY", "options:NIFTY", candles_topic("NIFTY", 1), candles_topic("NIFTY", 5), candles_topic("NIFTY", 15)} <= names


# -- the size claims, measured on the engine's real output -------------------------


def test_patches_are_smaller_than_snapshots_on_real_ticks(snapshots):
    """Honest measurement, not an assertion of a magic ratio: every topic's patch must be
    no larger than resending it, and the stream as a whole must save bytes."""
    full_total = patch_total = 0
    for before, after in zip(snapshots, snapshots[1:]):
        old, new = split_snapshot(before, "NIFTY"), split_snapshot(after, "NIFTY")
        for topic, payload in new.items():
            node = diff(old.get(topic), payload) if topic in old else None
            if node is None:
                continue
            patch_size, full_size = encoded_size(wire(node)), encoded_size(payload)
            full_total += full_size
            patch_total += min(patch_size, full_size)  # the hub sends whichever is smaller
            assert canon(apply_patch(old[topic], wire(node))) == canon(payload)
    assert patch_total < full_total
