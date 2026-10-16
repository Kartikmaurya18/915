"""History/straddle endpoints go through the shared read cache without leaking one user's data
into another's, and the cache never remembers "no data"."""

from __future__ import annotations

import pytest

from src.api import server


@pytest.fixture(autouse=True)
def fresh_cache():
    server.read_cache.clear()
    yield
    server.read_cache.clear()


@pytest.fixture
def counting_engine(monkeypatch):
    calls: list[tuple] = []
    answers: dict[str, object] = {}

    async def fake(method, *args, **kwargs):
        calls.append((method, args))
        return answers.get(method)

    monkeypatch.setattr(server, "engine_call", fake)
    return calls, answers


def test_trading_dates_are_cached_between_requests(client, new_user, counting_engine):
    calls, answers = counting_engine
    answers["get_available_trading_dates"] = {"dates": ["2026-01-02"], "today": "2026-01-05"}
    _, headers = new_user()
    for _ in range(5):
        assert client.get("/api/history/dates", headers=headers).json()["today"] == "2026-01-05"
    assert len([c for c in calls if c[0] == "get_available_trading_dates"]) == 1


def test_users_share_the_engine_answer_but_each_gets_their_own_paper_section(client, new_user, counting_engine):
    calls, answers = counting_engine
    answers["get_historical_session"] = {
        "trading_date": "2020-01-01",
        "has_data": True,
        "paper_trading": {"marker": "GLOBAL-BOOK-MUST-NOT-LEAK"},
    }
    _, alice = new_user()
    _, bob = new_user()
    a = client.get("/api/history/session?date=2020-01-01", headers=alice).json()
    b = client.get("/api/history/session?date=2020-01-01", headers=bob).json()

    assert len([c for c in calls if c[0] == "get_historical_session"]) == 1  # one engine call for both users
    for body in (a, b):
        assert "marker" not in body["paper_trading"]  # the engine's global book was replaced, not exposed
        assert body["paper_trading"]["history"] == []
    # The cached object itself was not altered by serving the first user.
    cached = next(iter(server.read_cache._data.values()))[1]
    assert cached["paper_trading"] == {"marker": "GLOBAL-BOOK-MUST-NOT-LEAK"}


def test_missing_straddle_data_is_404_and_retried(client, new_user, counting_engine):
    calls, answers = counting_engine
    _, headers = new_user()
    assert client.get("/api/straddle-candles?strike=24850", headers=headers).status_code == 404
    answers["get_strike_straddle_candles"] = {"strike": 24850.0, "by_interval": {}}
    assert client.get("/api/straddle-candles?strike=24850", headers=headers).status_code == 200  # no stale 404
    assert client.get("/api/straddle-candles?strike=24850", headers=headers).status_code == 200
    assert len([c for c in calls if c[0] == "get_strike_straddle_candles"]) == 2


def test_different_strikes_and_dates_do_not_share_entries(client, new_user, counting_engine):
    calls, answers = counting_engine
    answers["get_strike_straddle_candles"] = {"strike": 1.0, "by_interval": {}}
    answers["get_persisted_strike_candles_for_date"] = {"strike": 1.0, "by_interval": {}}
    _, headers = new_user()
    for strike in (24800, 24850):
        client.get(f"/api/straddle-candles?strike={strike}", headers=headers)
    for date in ("2020-01-01", "2020-01-02"):
        client.get(f"/api/straddle-candles?strike=24800&date={date}", headers=headers)
    assert len(calls) == 4


def test_large_responses_are_gzipped(client, new_user, counting_engine):
    _, answers = counting_engine
    answers["get_historical_session"] = {"trading_date": "2020-01-01", "has_data": True, "rows": [{"a": i} for i in range(2000)]}
    _, headers = new_user()
    response = client.get("/api/history/session?date=2020-01-01", headers={**headers, "Accept-Encoding": "gzip"})
    assert response.status_code == 200
    assert response.headers.get("content-encoding") == "gzip"
    assert response.json()["rows"][1999] == {"a": 1999}  # the client library transparently inflates it
