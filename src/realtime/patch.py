"""Structural diff / patch for JSON-like values.

`diff(old, new)` returns a patch node (or None if nothing changed) such that
`apply_patch(old, node) == new`. The same format is implemented client-side.

Node encoding (chosen so unchanged data costs nothing and changed scalars have no
wrapper):

* a JSON scalar (bool / number / string)         -> replace the value with it
* ["=", value]                                   -> replace with an object, array or null
* ["d", {key: node, ...}, [deleted keys]]        -> update an object in place
* ["k", field, {keyval: node}, [deleted], order] -> update an array of objects that
                                                    are identified by `field`
                                                    (order is the new key order, or
                                                    null if unchanged)

Arrays of keyed objects (option-chain strikes, candle series) are diffed per
element, so a tick that moves 3 of 80 strikes ships 3 rows, not 80.
"""

from __future__ import annotations

import json
from typing import Any

KEY_FIELDS = ("strike", "open_time")
_SCALARS = (str, int, float, bool, type(None))


def _wrap(value: Any) -> Any:
    # Bare scalars are only for non-null values: a bare null would be indistinguishable
    # from "no change" (diff() returns None for that), so null is always wrapped.
    return value if isinstance(value, _SCALARS) and value is not None else ["=", value]


def _same_scalar(a: Any, b: Any) -> bool:
    # `True == 1` in Python, but they are different JSON values.
    return type(a) is type(b) and a == b


def _key_field(items: list) -> str | None:
    """The field that identifies each element, if every element is an object that has
    it and the values are unique and hashable; otherwise None."""
    if not items or not all(isinstance(i, dict) for i in items):
        return None
    for field in KEY_FIELDS:
        if all(field in i for i in items):
            values = [i[field] for i in items]
            if all(isinstance(v, (str, int, float)) and not isinstance(v, bool) for v in values) and len(set(values)) == len(values):
                return field
    return None


def diff(old: Any, new: Any) -> Any:
    """Patch node turning `old` into `new`, or None when they are identical."""
    if isinstance(old, dict) and isinstance(new, dict):
        changes: dict[str, Any] = {}
        for key, value in new.items():
            if key not in old:
                changes[key] = _wrap(value)
            else:
                node = diff(old[key], value)
                if node is not None:
                    changes[key] = node
        deleted = [key for key in old if key not in new]
        if not changes and not deleted:
            return None
        return ["d", changes, deleted]

    if isinstance(old, list) and isinstance(new, list):
        field = _key_field(new)
        if field is not None and _key_field(old) == field:
            old_by_key = {item[field]: item for item in old}
            changes = {}
            for item in new:
                key = item[field]
                if key not in old_by_key:
                    changes[_k(key)] = ["=", item]
                else:
                    node = diff(old_by_key[key], item)
                    if node is not None:
                        changes[_k(key)] = node
            new_keys = [item[field] for item in new]
            new_set = set(new_keys)
            deleted = [k for k in old_by_key if k not in new_set]
            # Without an explicit order the client rebuilds "surviving old items in
            # their old order, then new items in arrival order". Only send the
            # order when that would not reproduce `new`.
            implied = [k for k in old_by_key if k in new_set] + [k for k in new_keys if k not in old_by_key]
            order = None if implied == new_keys else new_keys
            if not changes and not deleted and order is None:
                return None
            return ["k", field, changes, deleted, order]
        return None if _json_equal(old, new) else ["=", new]

    if isinstance(old, _SCALARS) and isinstance(new, _SCALARS):
        return None if _same_scalar(old, new) else _wrap(new)
    return ["=", new]  # type changed (e.g. object -> scalar)


def _k(key: Any) -> str:
    """JSON object keys are strings, so keyed-array keys are stringified on the wire.

    Numbers are normalised (24850 and 24850.0 are the same strike) and tagged apart
    from strings, so a numeric key can never collide with a string key."""
    if isinstance(key, (int, float)):
        return "n:" + repr(float(key))
    return "s:" + str(key)


def _json_equal(a: Any, b: Any) -> bool:
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def apply_patch(base: Any, node: Any) -> Any:
    """Apply a patch node to `base` and return the new value (`base` is not modified)."""
    if not isinstance(node, list):
        return node
    tag = node[0]
    if tag == "=":
        return node[1]
    if tag == "d":
        result = dict(base)
        for key, child in node[1].items():
            result[key] = apply_patch(result.get(key), child)
        for key in node[2]:
            result.pop(key, None)
        return result
    if tag == "k":
        _, field, changes, deleted, order = node
        by_key = {_k(item[field]): item for item in base}
        for key, child in changes.items():
            by_key[key] = apply_patch(by_key.get(key), child)
        for raw in deleted:
            by_key.pop(_k(raw), None)
        if order is None:
            return list(by_key.values())
        return [by_key[_k(k)] for k in order]
    raise ValueError(f"unknown patch node {tag!r}")


def encoded_size(value: Any) -> int:
    return len(json.dumps(value, separators=(",", ":")))
