"""The files under deploy/ must stay true to the code they describe."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
ALERTS = ROOT / "deploy" / "prometheus" / "alerts.yml"


def defined_metric_names() -> set[str]:
    """Every Prometheus metric name declared in src/, with the suffixes the client library adds."""
    names: set[str] = set()
    for path in (ROOT / "src").rglob("*.py"):
        for match in re.finditer(r'(?:Counter|Gauge|Histogram|Summary)\(\s*"([a-z_][a-z0-9_]*)"', path.read_text(encoding="utf-8")):
            base = match.group(1)
            names.add(base)
            names.update({f"{base}_bucket", f"{base}_sum", f"{base}_count", f"{base}_total", f"{base}_created"})
    return names


def load_rules() -> list[dict]:
    doc = yaml.safe_load(ALERTS.read_text(encoding="utf-8"))
    return [rule for group in doc["groups"] for rule in group["rules"]]


def test_the_alert_file_is_valid_and_well_formed():
    rules = load_rules()
    assert len(rules) >= 15
    names = [r["alert"] for r in rules]
    assert len(names) == len(set(names)), "duplicate alert names"
    for rule in rules:
        assert rule["expr"].strip(), rule["alert"]
        assert rule["labels"]["severity"] in ("page", "warn"), rule["alert"]
        assert rule["annotations"]["summary"], rule["alert"]


def test_every_metric_in_an_alert_exists_in_the_code():
    known = defined_metric_names() | {"up"}
    missing: dict[str, set[str]] = {}
    for rule in load_rules():
        expr = re.sub(r'"[^"]*"', '""', rule["expr"])  # drop label values so words in them are not read as metrics
        expr = re.sub(r"\{[^}]*\}", "", expr)  # drop selectors
        words = set(re.findall(r"\b([a-z_][a-z0-9_]*)\b", expr))
        functions = {"sum", "rate", "increase", "histogram_quantile", "by", "le", "instance", "without", "avg", "max", "min", "on", "ignoring"}
        metrics = {w for w in words if w not in functions and not w.isdigit() and (w.startswith(("rt_", "db_", "rate_limit_", "otp_")) or w == "up")}
        unknown = metrics - known
        if unknown:
            missing[rule["alert"]] = unknown
    assert not missing, f"alerts reference metrics the code does not define: {missing}"


def test_label_values_used_in_alerts_are_ones_the_code_emits():
    """e.g. rt_publish_total{result="fenced"}: the value must exist in the source."""
    source = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "src").rglob("*.py"))
    checks = {
        "rt_publish_total": ("result", ["fenced", "error"]),
        "rt_updates_total": ("result", ["out_of_order"]),
        "rt_clients_dropped_total": ("reason", ["slow_consumer", "server_at_capacity"]),
        "rt_snapshots_sent_total": ("reason", ["behind"]),
        "rt_leader_changes_total": ("event", ["acquired"]),
        "otp_delivery_total": ("result", ["error"]),
    }
    for rule in load_rules():
        for metric, (label, values) in checks.items():
            for value in re.findall(rf'{metric}\{{[^}}]*{label}="([^"]+)"', rule["expr"]):
                assert value in values, f"{rule['alert']}: unexpected {label}={value}"
                assert f'"{value}"' in source, f"{rule['alert']}: the code never emits {label}={value}"


def test_runbook_anchors_referenced_by_alerts_exist():
    runbook = (ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
    anchors = {re.sub(r"[^a-z0-9 -]", "", h.lower()).strip().replace(" ", "-") for h in re.findall(r"^#+\s+(.*)$", runbook, flags=re.M)}
    wanted = set()
    for rule in load_rules():
        text = rule["annotations"].get("description", "")
        wanted.update(re.findall(r"docs/RUNBOOK\.md#([a-z0-9-]+)", text))
    assert wanted, "alerts should point to the runbook"
    assert wanted <= anchors, f"missing runbook sections: {sorted(wanted - anchors)}"


@pytest.mark.parametrize("name", ["rt_is_leader", "rt_feed_age_seconds", "rt_redis_up"])
def test_gauges_used_for_paging_exist(name):
    assert name in defined_metric_names()
