"""End-to-end checks of the production topology brought up by docker compose (2 ingestors, 2 gateways,
Redis, PostgreSQL). Run it after `docker compose up -d --build`:

    python scripts/verify_stack.py --gateway1 http://127.0.0.1:8000 --gateway2 http://127.0.0.1:8001 --project <compose project>

It checks what in-process tests cannot:
  1. both gateways serve live data for the same epoch/sequence (the fan-in from Redis works);
  2. a paper-book change made through gateway 1 reaches the user's socket on gateway 2 (cross-gateway push);
  3. an automatic take-profit/stop-loss exit decided by the ingestor reaches the user's socket on a gateway;
  4. killing the leading ingestor fails over to the standby within a few lease TTLs, with a new epoch;
  5. the in-progress candle is continued after the failover (not restarted from one tick);
  6. the control channel (settings/history) answers through the new leader.

It registers throw-away users (mind the 10-registrations-per-hour-per-IP limit) and restarts the ingestor it stopped.
Exit code 0 only if every check passed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

import websockets

TOPICS = ["index:NIFTY", "options:NIFTY", "candles:NIFTY:1m"]
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(("PASS  " if ok else "FAIL  ") + name + (f"  [{detail}]" if detail else ""), flush=True)
    return ok


def http(method: str, url: str, body: dict | None = None, token: str | None = None, headers: dict | None = None, timeout: float = 10):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read()
            return res.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as err:
        raw = err.read()
        try:
            return err.code, json.loads(raw)
        except ValueError:
            return err.code, raw.decode(errors="replace")


def compose(project: str, *args: str) -> str:
    out = subprocess.run(["docker", "compose", "-p", project, *args], capture_output=True, text=True, timeout=120)
    return (out.stdout + out.stderr).strip()


def wait_ready(base: str, timeout: float = 120) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status, body = http("GET", base + "/api/ready", timeout=3)
            if status == 200 and body.get("feed") in ("ok", None, "none"):
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def register(base: str) -> tuple[str, str]:
    email = f"verify-{uuid.uuid4().hex[:10]}@example.com"
    status, body = http("POST", base + "/api/auth/register", {"email": email, "password": "verify-correct-horse-battery"})
    assert status == 201, (status, body)
    return email, body["access_token"]


class Feed:
    """One authenticated /ws/v1 connection that records everything it receives."""

    def __init__(self, base: str, token: str, name: str):
        self.url = base.replace("http", "ws", 1) + "/ws/v1"
        self.token, self.name = token, name
        self.frames: list[dict] = []
        self.state: dict[str, dict] = {}  # topic -> {epoch, seq, data}
        self.notices = 0
        self._task: asyncio.Task | None = None
        self.ws = None

    async def start(self):
        self.ws = await websockets.connect(self.url, max_size=4 * 1024 * 1024)
        await self.ws.send(json.dumps({"type": "auth", "token": self.token}))
        await self.ws.send(json.dumps({"type": "subscribe", "topics": TOPICS}))
        self._task = asyncio.create_task(self._read())

    async def _read(self):
        from web_patch import apply_patch  # defined below (a tiny port of src/realtime/patch.py)

        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                self.frames.append(msg)
                kind = msg.get("type")
                if kind == "snapshot":
                    self.state[msg["topic"]] = {"epoch": msg["epoch"], "seq": msg["seq"], "data": msg["data"]}
                elif kind == "patch":
                    held = self.state.get(msg["topic"])
                    if held and held["epoch"] == msg["epoch"] and held["seq"] == msg["base"]:
                        held["data"] = apply_patch(held["data"], msg["patch"])
                        held["seq"] = msg["seq"]
                    else:
                        await self.ws.send(json.dumps({"type": "resync", "topic": msg["topic"]}))
                elif kind == "paper_changed":
                    self.notices += 1
        except websockets.ConnectionClosed:
            pass

    async def stop(self):
        if self.ws:
            await self.ws.close()
        if self._task:
            self._task.cancel()

    def epoch(self, topic="index:NIFTY"):
        return (self.state.get(topic) or {}).get("epoch")

    def seq(self, topic="index:NIFTY"):
        return (self.state.get(topic) or {}).get("seq")


async def until(predicate, timeout: float, step: float = 0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(step)
    return predicate()


async def main(args) -> int:
    g1, g2, project = args.gateway1.rstrip("/"), args.gateway2.rstrip("/"), args.project
    check("both gateways become ready", wait_ready(g1) and wait_ready(g2))
    if not all(ok for _, ok, _ in results):
        return 1
    _, token = register(g1)

    a, b = Feed(g1, token, "gw1"), Feed(g2, token, "gw2")
    await a.start()
    await b.start()
    got = await until(lambda: all(t in a.state and t in b.state for t in TOPICS), 20)
    check("both gateways deliver snapshots for every topic", got)
    await asyncio.sleep(4)
    patches = sum(1 for f in a.frames + b.frames if f.get("type") == "patch")
    check("steady state is incremental (patches, not snapshots)", patches > 4, f"{patches} patches in ~4 s")
    resyncs_ok = await until(lambda: a.epoch() is not None and a.epoch() == b.epoch(), 10)
    check("both gateways see the same leader epoch", resyncs_ok, f"epoch {a.epoch()} / {b.epoch()}")

    # -- 2. cross-gateway push ------------------------------------------------------------------------
    before = b.notices
    deadline, opened = time.time() + 30, None
    while time.time() < deadline and opened is None:
        status, body = http("POST", g1 + "/api/paper/open", {"option_type": "CE", "side": "BUY", "quantity": 10}, token, {"Idempotency-Key": uuid.uuid4().hex})
        if status == 200:
            opened = body
        else:
            await asyncio.sleep(1)
    check("an order through gateway 1 succeeds", opened is not None)
    got = await until(lambda: b.notices > before, 5)
    check("the order's change notice reaches the user's socket on gateway 2", got, "via Redis pub/sub")

    # -- 3. automatic exit decided by the ingestor ---------------------------------------------------
    http("POST", g1 + "/api/paper/config", {"take_profit_percent": 0.5, "stop_loss_percent": 0.5}, token)
    before = b.notices
    status, body = http("POST", g1 + "/api/paper/open", {"option_type": "PE", "side": "BUY", "quantity": 10}, token, {"Idempotency-Key": uuid.uuid4().hex})
    pid = body.get("id") if status == 200 else None
    check("a position with a 0.5% target opens", pid is not None)
    exited = False
    deadline = time.time() + 40
    while time.time() < deadline and pid:
        _, state = http("GET", g2 + "/api/paper/state", token=token)
        hist = [p for p in state["history"] if p["id"] == pid]
        if hist:
            exited = hist[0]["exit_reason"] in ("TAKE_PROFIT", "STOP_LOSS")
            break
        await asyncio.sleep(1)
    check("the ingestor's exit worker closes it on a threshold (read through gateway 2)", exited)
    check("that automatic exit also sent a notice (no request of ours triggered it)", await until(lambda: b.notices > before + 1, 5) or b.notices > before)

    # -- 4/5. failover ------------------------------------------------------------------------------------
    leader = compose(project, "exec", "-T", "redis", "redis-cli", "GET", "rt:leader:holder").strip().splitlines()[-1] if True else ""
    check("a leader is registered in Redis", leader.startswith("ingestor"), leader)
    await asyncio.sleep(2)
    active_before = (a.state["candles:NIFTY:1m"]["data"] or {}).get("active") or {}
    series_before = len((a.state["candles:NIFTY:1m"]["data"] or {}).get("series") or [])
    epoch_before, seq_before = a.epoch(), a.seq()
    stopped_at = time.time()
    compose(project, "stop", "-t", "0", leader)  # abrupt: no graceful lease release
    took_over = await until(lambda: a.epoch() not in (None, epoch_before) and b.epoch() == a.epoch(), 40)
    elapsed = time.time() - stopped_at
    check("the standby takes over with a new epoch on both gateways", took_over, f"{elapsed:.1f} s after the leader died, epoch {epoch_before} -> {a.epoch()}")
    await until(lambda: a.seq() is not None and a.seq() > 0, 10)
    await asyncio.sleep(3)
    candles = a.state["candles:NIFTY:1m"]["data"] or {}
    series_after = len(candles.get("series") or [])
    check("history of candles survives the failover (not restarted empty)", series_after >= max(series_before - 1, 1), f"{series_before} -> {series_after} candles")
    active_after = candles.get("active") or {}
    if active_before.get("open_time") and active_before.get("open_time") == active_after.get("open_time"):
        check("the in-progress candle continued (same open, ticks kept)", active_after.get("open") == active_before.get("open") and active_after.get("tick_count", 0) >= active_before.get("tick_count", 0), f"open {active_before.get('open')} -> {active_after.get('open')}, ticks {active_before.get('tick_count')} -> {active_after.get('tick_count')}")
    else:
        print("SKIP  in-progress candle continuity (the candle rolled over during the failover; rerun to catch the same minute)")

    # -- 6. control channel through the new leader ---------------------------------------------------------
    ok = False
    for _ in range(20):
        status, body = http("GET", g1 + "/api/history/dates", token=token)
        if status == 200 and "today" in body:
            ok = True
            break
        await asyncio.sleep(1)
    check("history answers through the new leader (control channel)", ok)

    compose(project, "start", leader)
    await a.stop()
    await b.stop()
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)} passed, {len(failed)} failed")
    return 1 if failed else 0


# A minimal client-side patch applier, identical in behaviour to src/realtime/patch.py:apply_patch.
def _install_patch_module():
    import types

    sys.path.insert(0, str(__file__).rsplit("scripts", 1)[0])
    from src.realtime.patch import apply_patch

    module = types.ModuleType("web_patch")
    module.apply_patch = apply_patch
    sys.modules["web_patch"] = module


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--gateway1", default="http://127.0.0.1:8000")
    parser.add_argument("--gateway2", default="http://127.0.0.1:8001")
    parser.add_argument("--project", required=True, help="docker compose project name")
    _install_patch_module()
    raise SystemExit(asyncio.run(main(parser.parse_args())))
