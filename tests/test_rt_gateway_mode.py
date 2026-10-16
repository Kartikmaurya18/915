"""The production topology end to end, in one test process:

    real ingestor (leader, owns the engine)  --Redis-->  gateway app (REALTIME_MODE=gateway)

Nothing about the engine is shared in memory: the gateway sees only what comes through Redis
and the database. Skipped unless TEST_REDIS_URL is set.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid

import pytest
import redis

from src.api import server
from src.config import load_config
from src.ingestor import Ingestor
from src.realtime import settings as rt_settings
from tests.test_rt_ws import expect_close, recv_json, recv_until

URL = os.getenv("TEST_REDIS_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_REDIS_URL not set")


def fast_cfg() -> dict:
    cfg = load_config()
    cfg["poll_interval_seconds"] = 0.15
    cfg["persistence"] = {"enabled": False}
    return cfg


def wait_for(predicate, timeout=10.0, interval=0.03):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


@pytest.fixture
def prefix(monkeypatch):
    p = f"rt-gw-{uuid.uuid4().hex[:8]}:"
    monkeypatch.setenv("RT_PREFIX", p)
    monkeypatch.setenv("REDIS_URL", URL)
    yield p
    cleanup = redis.Redis.from_url(URL)
    for key in cleanup.scan_iter(match=f"{p}*"):
        cleanup.delete(key)
    cleanup.close()


@pytest.fixture
def stack(auth_env, prefix, monkeypatch):
    """ingestor thread + gateway TestClient. `auth_env` provides AUTH_MODE=required and a migrated DB."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("REALTIME_MODE", "gateway")
    ingestor = Ingestor(URL, prefix, node_id="ingestor-1", ttl_ms=1500, cfg=fast_cfg())
    thread = threading.Thread(target=ingestor.run_forever, daemon=True)
    thread.start()
    try:
        assert wait_for(lambda: ingestor.became_leader.is_set()), "ingestor never became leader"
        server.rt_hub = server.Hub()
        with TestClient(server.app) as client:
            assert wait_for(lambda: server.rt_hub.latest("options:NIFTY") is not None), "no market data reached the gateway"
            yield client, ingestor
    finally:
        ingestor.request_stop()
        thread.join(timeout=10)


def register(client, email="trader@example.com"):
    body = client.post("/api/auth/register", json={"email": email, "password": "correct horse battery"}).json()
    return {"Authorization": f"Bearer {body['access_token']}"}


def make_admin(client, email):
    from src.auth import service
    from src.db.session import session_scope

    with session_scope() as session:
        service.set_role(session, email, "admin")


# -- topology guards -----------------------------------------------------------------------


def test_a_gateway_refuses_to_start_without_auth(monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("REALTIME_MODE", "gateway")
    monkeypatch.setenv("AUTH_MODE", "disabled")
    with pytest.raises(RuntimeError, match="requires AUTH_MODE=required"):
        with TestClient(server.app):
            pass


def test_a_gateway_never_runs_an_engine(stack):
    client, _ = stack
    assert not server.engine.is_running  # the gateway process owns no provider connection
    client.get("/api/health")
    assert not server.engine.is_running  # not even lazily, via the HTTP middleware


def test_unknown_realtime_mode_is_rejected(monkeypatch):
    monkeypatch.setenv("REALTIME_MODE", "sideways")
    with pytest.raises(ValueError):
        rt_settings.mode()


# -- the live path ---------------------------------------------------------------------------


def test_readiness_reports_redis_database_and_feed(stack):
    client, _ = stack
    body = client.get("/api/ready").json()
    assert body["status"] == "ready"
    assert body["checks"] == {"redis": "ok", "database": "ok"}
    assert body["feed"] == "ok" and body["feed_age_ms"] < 5000


def test_data_produced_by_the_ingestor_reaches_a_websocket_client(stack):
    client, ingestor = stack
    headers = register(client)
    with client.websocket_connect("/ws/v1") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": headers["Authorization"].split(" ", 1)[1]}))
        recv_until(ws, lambda f: f["type"] == "hello")
        ws.send_text(json.dumps({"type": "subscribe", "topics": ["index:NIFTY", "options:NIFTY"]}))
        snap, _ = recv_until(ws, lambda f: f["type"] == "snapshot" and f["topic"] == "options:NIFTY")
        assert snap["epoch"] == ingestor.elector.fence  # stamped by the leader's fencing token
        assert snap["data"]["underlying"] == "NIFTY" and len(snap["data"]["strikes"]) > 3
        # live updates keep arriving, with strictly increasing versions
        versions = []
        deadline = time.time() + 6
        while len(versions) < 4 and time.time() < deadline:
            frame = recv_json(ws)
            if frame["type"] in ("snapshot", "patch") and frame["topic"] == "options:NIFTY":
                versions.append(frame["seq"])
        assert len(versions) >= 3 and versions == sorted(versions)


def test_the_legacy_single_blob_stream_is_not_offered(stack):
    client, _ = stack
    with client.websocket_connect("/ws") as ws:
        expect_close(ws, 1008)


def test_snapshot_endpoint_is_rebuilt_from_topics(stack):
    client, _ = stack
    headers = register(client)
    snap = client.get("/api/snapshot", headers=headers).json()
    for key in ("price", "atm", "option_chain", "candles", "provider", "settings"):
        assert key in snap, key
    assert "paper_trading" not in snap
    assert set(snap["candles"]) >= {"1", "5", "15"}


# -- engine-bound endpoints go through the control channel -------------------------------------


def test_settings_are_read_and_changed_through_the_leader(stack):
    client, ingestor = stack
    email = "boss@example.com"
    headers = register(client, email)
    assert client.post("/api/settings/atm", json={"atm_method": "price"}, headers=headers).status_code == 403  # not an admin
    assert ingestor.engine.atm_method == "delta"

    make_admin(client, email)
    changed = client.post("/api/settings/atm", json={"atm_method": "price"}, headers=headers)
    assert changed.status_code == 200 and changed.json()["atm_method"] == "price"
    assert ingestor.engine.atm_method == "price"  # the change landed in the ingestor's engine, not the gateway's
    assert client.get("/api/settings", headers=headers).json()["atm_method"] == "price"


def test_bad_input_is_a_400_not_a_502(stack):
    client, _ = stack
    email = "boss2@example.com"
    headers = register(client, email)
    make_admin(client, email)
    bad = client.post("/api/settings/atm", json={"atm_method": "nonsense"}, headers=headers)
    assert bad.status_code == 400 and "atm_method" in bad.json()["detail"]


def test_logs_come_from_the_ingestors_ring_buffer(stack):
    client, _ = stack
    email = "boss3@example.com"
    headers = register(client, email)
    make_admin(client, email)
    logs = client.get("/api/logs?limit=5", headers=headers)
    assert logs.status_code == 200 and 0 < len(logs.json()["logs"]) <= 5


def test_history_endpoints_work_through_the_channel(stack):
    client, _ = stack
    headers = register(client)
    dates = client.get("/api/history/dates", headers=headers)
    assert dates.status_code == 200 and "today" in dates.json()
    session = client.get(f"/api/history/session?date={dates.json()['today']}", headers=headers)
    assert session.status_code == 200 and "paper_trading" in session.json()


def test_engine_bound_endpoints_return_503_while_no_ingestor_leads(stack, prefix):
    client, ingestor = stack
    headers = register(client)
    ingestor.request_stop()
    assert wait_for(lambda: not ingestor.became_leader.is_set(), 10)
    time.sleep(0.3)
    started = time.time()
    response = client.get("/api/settings", headers=headers)
    assert response.status_code == 503 and response.json()["detail"]["code"] == "ingestor_unavailable"
    assert response.headers.get("retry-after")
    assert time.time() - started < 8  # bounded wait, never a hang
    assert client.get("/api/health").status_code == 200  # the gateway itself stays up
    stale = client.get("/api/ready").json()
    assert stale["checks"]["redis"] == "ok"  # Redis is fine; only the feed is gone
    assert client.get("/api/paper/state", headers=headers).status_code == 200  # DB-backed features keep working


# -- paper trading across the split ------------------------------------------------------------


def test_paper_trade_is_priced_from_redis_and_exited_by_the_ingestors_worker(stack):
    client, ingestor = stack
    headers = register(client)
    # A hair-trigger threshold so the mock feed's next tick closes the position.
    cfg = client.post("/api/paper/config", json={"take_profit_percent": 0.01, "stop_loss_percent": 0.01}, headers=headers)
    assert cfg.status_code == 200

    chain = server.rt_hub.latest("options:NIFTY")
    strike = chain["strikes"][len(chain["strikes"]) // 2]["strike"]
    opened = client.post("/api/paper/open", json={"option_type": "CE", "strike": strike, "quantity": 25}, headers=headers)
    assert opened.status_code == 200, opened.text
    price_in_redis = next(r for r in chain["strikes"] if r["strike"] == strike)["call_ltp"]
    assert abs(opened.json()["entry_price"] - price_in_redis) < price_in_redis  # filled near the hub's price

    # The gateway holds no book and decides no exits: the ingestor's worker closes it via the database.
    assert wait_for(lambda: client.get("/api/paper/state", headers=headers).json()["history"], 8), "no exit happened"
    state = client.get("/api/paper/state", headers=headers).json()
    assert state["open_positions"] == []
    assert state["history"][0]["id"] == opened.json()["id"]
    assert state["history"][0]["exit_reason"] in ("TAKE_PROFIT", "STOP_LOSS")


def test_a_trade_is_refused_when_the_feed_is_stale(stack, monkeypatch):
    from src.paper import market_view

    client, ingestor = stack
    headers = register(client)
    ingestor.request_stop()
    assert wait_for(lambda: not ingestor.became_leader.is_set(), 10)
    monkeypatch.setattr(market_view, "MAX_QUOTE_AGE_SECONDS", 0.5)
    time.sleep(1.0)  # the hub now holds data older than the limit
    chain = server.rt_hub.latest("options:NIFTY")
    strike = chain["strikes"][2]["strike"]
    refused = client.post("/api/paper/open", json={"option_type": "CE", "strike": strike, "quantity": 5}, headers=headers)
    assert refused.status_code == 400 and "stale" in refused.json()["detail"].lower()
