"""WebSocket load generator for the real-time path.

Opens N clients against /ws/v1 (or the legacy /ws for a baseline), spread over worker
processes so the generator itself does not become the bottleneck, and reports delivery
latency, bytes per update, patch/snapshot mix, resyncs and drops.

    python scripts/loadtest_ws.py --url ws://127.0.0.1:8000 --clients 500 --duration 30
    python scripts/loadtest_ws.py --url ws://127.0.0.1:8000 --legacy --clients 500
    python scripts/loadtest_ws.py --clients 200 --slow-fraction 0.1   # 10% of clients read slowly

Latency is `receive time - frame.pub` (the ingestor's publish time). It is only meaningful when
generator and server share a clock - i.e. the same machine, as in local runs. Against a remote
host, treat it as relative, not absolute.

With --token (an access token) or --register (creates users via the API) it also works with
AUTH_MODE=required. A sample of clients fully parses frames and checks that its mirror equals
the server's state; the rest only regex-extract `pub` so the generator stays cheap.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import multiprocessing as mp
import re
import statistics
import sys
import time
import zlib
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import websockets  # noqa: E402

from src.realtime.client import TopicMirror  # noqa: E402

PUB = re.compile(r'"pub":(\d+)')
TS = re.compile(r'"ts":(\d+)')
LEGACY_TS = re.compile(r'"timestamp": "([^"]+)"')
TOPIC = re.compile(r'"topic":"([^"]+)"')
PROFILES = {
    # what a page typically subscribes to
    "option-chain": ["index:NIFTY", "options:NIFTY"],
    "chart": ["index:NIFTY", "candles:NIFTY:1m"],
    "dashboard": ["index:NIFTY"],
}
PROFILE_MIX = [("option-chain", 0.6), ("chart", 0.25), ("dashboard", 0.15)]


def pick_profile(i: int, total: int) -> str:
    position, acc = (i + 0.5) / total, 0.0
    for name, share in PROFILE_MIX:
        acc += share
        if position <= acc:
            return name
    return PROFILE_MIX[-1][0]


async def one_client(i, total, args, token, stats, deadline, verify):
    profile = pick_profile(i, total)
    topics = PROFILES[profile]
    slow = (i % 100) < int(args.slow_fraction * 100)
    mirror = TopicMirror() if verify else None
    seen_keys: set[str] = set()
    latencies, sizes = stats["latencies"], stats["sizes"]
    try:
        async with websockets.connect(args.url + ("/ws" if args.legacy else "/ws/v1"), max_size=2**22, open_timeout=30,
                                      ping_interval=None) as ws:
            if token:
                await ws.send(json.dumps({"type": "auth", "token": token}))
            if not args.legacy:
                await ws.send(json.dumps({"type": "subscribe", "topics": topics, **({"batch": True} if args.batch else {})}))
            stats["connected"] += 1
            while time.time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.time()))
                except asyncio.TimeoutError:
                    break
                now_ms = time.time() * 1000
                stats["frames"] += 1
                stats["bytes"] += len(raw)
                parts = [raw]
                if args.batch and raw.startswith('{"v":1,"type":"batch"'):
                    # One websocket message carrying several topic updates: unpack and account for each.
                    parts = [json.dumps(inner, separators=(",", ":")) for inner in json.loads(raw)["frames"]]
                for raw in parts:
                    # A client's FIRST frame per topic is the stored state, whose age is "time since that
                    # topic last changed", not a delivery delay - leave it out of the latency figures.
                    topic_match = TOPIC.search(raw) if not args.legacy else None
                    key = topic_match.group(1) if topic_match else "legacy"
                    initial = key not in seen_keys
                    seen_keys.add(key)
                    match = PUB.search(raw)
                    if match and not initial:
                        latencies.append(now_ms - int(match.group(1)))
                    # Source latency: from the tick being created to this client receiving it. It is the
                    # one number comparable between the legacy stream and /ws/v1.
                    if initial:
                        pass
                    elif args.legacy:
                        legacy = LEGACY_TS.search(raw)
                        if legacy:
                            stats["source"].append(now_ms - datetime.fromisoformat(legacy.group(1)).timestamp() * 1000)
                    else:
                        source = TS.search(raw)
                        if source:
                            stats["source"].append(now_ms - int(source.group(1)))
                    if raw.startswith('{"v":1,"type":"patch"'):
                        stats["patches"] += 1
                        sizes["patch"].append(len(raw))
                    elif raw.startswith('{"v":1,"type":"snapshot"'):
                        stats["snapshots"] += 1
                        sizes["snapshot"].append(len(raw))
                    elif args.legacy:
                        sizes["legacy"].append(len(raw))
                    if mirror is not None and not args.legacy and raw.startswith('{"v":1,"type":"'):
                        frame = json.loads(raw)
                        if frame["type"] in ("snapshot", "patch") and mirror.apply(frame) == "resync":
                            stats["resyncs"] += 1
                            await ws.send(json.dumps({"type": "resync", "topic": frame["topic"]}))
                if slow:
                    await asyncio.sleep(args.slow_delay)  # a client that drains its socket slowly
            stats["completed"] += 1
            if mirror is not None:
                stats["verified"] += 1
                stats["mirror_topics"] += len(mirror.topics)
    except Exception as exc:  # connection refused, closed by the server (slow consumer), ...
        stats["errors"][type(exc).__name__ + (f":{getattr(exc, 'code', '')}" if hasattr(exc, "code") else "")] += 1


async def worker_main(worker_id, count, offset, total, args, token_for, out):
    from collections import Counter

    stats = {"connected": 0, "completed": 0, "frames": 0, "bytes": 0, "patches": 0, "snapshots": 0, "resyncs": 0,
             "verified": 0, "mirror_topics": 0, "latencies": [], "source": [], "sizes": {"patch": [], "snapshot": [], "legacy": []},
             "errors": Counter()}
    deadline = time.time() + args.ramp + args.duration
    tasks = []
    for k in range(count):
        i = offset + k
        verify = (i % 20) == 0  # a 5% sample fully parses and checks convergence
        tasks.append(asyncio.create_task(one_client(i, total, args, token_for(i), stats, deadline, verify)))
        if args.ramp:
            await asyncio.sleep(args.ramp / max(count, 1))
    await asyncio.gather(*tasks, return_exceptions=True)
    stats["errors"] = dict(stats["errors"])
    # keep the result small: percentiles need the latency list, so thin it out deterministically
    for key in ("latencies", "source"):
        values = stats[key]
        stats[key] = values[:: max(1, len(values) // 20000)]
    for key, values in stats["sizes"].items():
        stats["sizes"][key] = values[:: max(1, len(values) // 2000)]
    out.put(stats)


def worker_entry(worker_id, count, offset, total, args, tokens, out):
    token_for = (lambda i: tokens[i % len(tokens)]) if tokens else (lambda i: None)
    asyncio.run(worker_main(worker_id, count, offset, total, args, token_for, out))


def scrape(url: str, token: str | None = None) -> dict[str, float]:
    """Sum every sample of each metric family in a Prometheus text exposition."""
    import urllib.request

    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
    totals: dict[str, float] = {}
    for line in urllib.request.urlopen(request, timeout=10).read().decode().splitlines():
        if not line or line.startswith("#"):
            continue
        name, _, value = line.rpartition(" ")
        base = name.split("{")[0]
        try:
            totals[base] = totals.get(base, 0.0) + float(value)
        except ValueError:
            pass
        if base == "rt_fanout_seconds_bucket":
            le = name.split('le="')[1].split('"')[0]
            totals[f"fanout_le_{le}"] = totals.get(f"fanout_le_{le}", 0.0) + float(value)
    return totals


def server_report(before: dict, after: dict, window: float, clients: int) -> dict:
    """Server-side cost over the steady-state window, from the server's own counters.

    CPU seconds per delivered frame is the number to size on: it measures work done, so a busy or
    shared machine inflates latency but barely moves it."""
    d = lambda key: after.get(key, 0.0) - before.get(key, 0.0)  # noqa: E731
    frames = d("rt_messages_sent_total")
    report = {"window_s": round(window, 1), "frames": int(frames), "bytes": int(d("rt_bytes_sent_total")),
              "coalesced": int(d("rt_coalesced_total")), "snapshots": int(d("rt_snapshots_sent_total")),
              "server_cpu_s": round(d("process_cpu_seconds_total"), 2)}
    if frames > 0:
        cpu_ms = d("process_cpu_seconds_total") / frames * 1000
        report["cpu_ms_per_frame"] = round(cpu_ms, 3)
        report["frames_per_s"] = round(frames / window)
        report["cpu_utilisation_pct_of_one_core"] = round(d("process_cpu_seconds_total") / window * 100)
        per_client_fps = frames / window / max(clients, 1)
        report["frames_per_client_per_s"] = round(per_client_fps, 2)
        report["clients_per_core_at_70pct_cpu"] = int(0.7 / (cpu_ms / 1000 * per_client_fps)) if cpu_ms > 0 else None
    total = d("fanout_le_+Inf")
    if total > 0:
        quant = {}
        for q in (0.5, 0.95, 0.99):
            for le in ("0.0005", "0.001", "0.0025", "0.005", "0.01", "0.025", "0.05", "0.1", "0.25", "0.5", "1.0", "2.5", "5.0", "+Inf"):
                if d(f"fanout_le_{le}") >= q * total:
                    quant[f"p{int(q * 100)}"] = le
                    break
        report["server_fanout_seconds_le"] = quant  # histogram upper bound containing the percentile
    return report


def register_users(base_http: str, n: int) -> list[str]:
    import urllib.request

    tokens = []
    for k in range(n):
        body = json.dumps({"email": f"load{int(time.time())}-{k}@example.com", "password": "correct horse battery"}).encode()
        req = urllib.request.Request(base_http + "/api/auth/register", data=body, headers={"Content-Type": "application/json"})
        tokens.append(json.load(urllib.request.urlopen(req))["access_token"])
    return tokens


def pct(values, q):
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * q))]


def summarize(parts, args, elapsed):
    merged = {"latencies": [], "source": [], "sizes": {"patch": [], "snapshot": [], "legacy": []}, "errors": {}}
    totals = {k: 0 for k in ("connected", "completed", "frames", "bytes", "patches", "snapshots", "resyncs", "verified", "mirror_topics")}
    for part in parts:
        for k in totals:
            totals[k] += part[k]
        merged["latencies"] += part["latencies"]
        merged["source"] += part["source"]
        for k, v in part["sizes"].items():
            merged["sizes"][k] += v
        for k, v in part["errors"].items():
            merged["errors"][k] = merged["errors"].get(k, 0) + v
    lat, src = merged["latencies"], merged["source"]
    result = {
        "mode": "legacy /ws" if args.legacy else "/ws/v1",
        "clients_requested": args.clients,
        "connected": totals["connected"],
        "completed": totals["completed"],
        "errors": merged["errors"],
        "duration_s": round(elapsed, 1),
        "frames": totals["frames"],
        "frames_per_s": round(totals["frames"] / args.duration),
        "mbit_per_s_total": round(totals["bytes"] * 8 / args.duration / 1e6, 2),
        "kb_per_s_per_client": round(totals["bytes"] / max(totals["connected"], 1) / args.duration / 1024, 2),
        "latency_ms": {"p50": pct(lat, 0.5), "p95": pct(lat, 0.95), "p99": pct(lat, 0.99), "max": max(lat) if lat else None,
                       "samples": len(lat)},
        "source_latency_ms": {"p50": pct(src, 0.5), "p95": pct(src, 0.95), "p99": pct(src, 0.99),
                              "max": max(src) if src else None, "samples": len(src)},
        "patches": totals["patches"],
        "snapshots": totals["snapshots"],
        "resyncs": totals["resyncs"],
        "verified_clients": totals["verified"],
    }
    for kind, sizes in merged["sizes"].items():
        if sizes:
            result[f"avg_{kind}_bytes"] = round(statistics.mean(sizes))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", default="ws://127.0.0.1:8000")
    parser.add_argument("--clients", type=int, default=100)
    parser.add_argument("--duration", type=float, default=20)
    parser.add_argument("--ramp", type=float, default=3, help="seconds over which clients connect")
    parser.add_argument("--processes", type=int, default=max(1, min(6, (mp.cpu_count() or 2) - 1)))
    parser.add_argument("--legacy", action="store_true", help="baseline: the original single-blob /ws")
    parser.add_argument("--batch", action="store_true", help="opt in to batch frames (all topic updates of a tick in one message)")
    parser.add_argument("--token", default=None)
    parser.add_argument("--register", type=int, default=0, help="register N users and spread clients over them")
    parser.add_argument("--slow-fraction", type=float, default=0.0)
    parser.add_argument("--slow-delay", type=float, default=0.5)
    parser.add_argument("--server-pid", type=int, default=0, help="sample this process's CPU and memory (needs psutil)")
    parser.add_argument("--metrics-url", default=None, help="server /metrics URL to scrape for server-side cost")
    parser.add_argument("--metrics-token", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    tokens = [args.token] if args.token else []
    if args.register:
        tokens = register_users(args.url.replace("ws://", "http://").replace("wss://", "https://"), args.register)

    server = None
    if args.server_pid:
        try:
            import psutil

            server = psutil.Process(args.server_pid)
            server.cpu_percent(None)
        except Exception as exc:
            print(f"(server sampling unavailable: {exc})", file=sys.stderr)
    out = mp.Queue()
    per = [args.clients // args.processes + (1 if p < args.clients % args.processes else 0) for p in range(args.processes)]
    started, offset, procs = time.time(), 0, []
    for p, count in enumerate(per):
        proc = mp.Process(target=worker_entry, args=(p, count, offset, args.clients, args, tokens, out))
        proc.start()
        procs.append(proc)
        offset += count
    cpu = []
    before = after = None
    if args.metrics_url:
        time.sleep(args.ramp + 2)  # skip connection set-up; measure the steady state only
        window_start = time.time()
        try:
            before = scrape(args.metrics_url, args.metrics_token)
        except Exception as exc:
            print(f"(metrics scrape failed: {exc})", file=sys.stderr)
        time.sleep(max(1.0, args.duration - 6))
        window = time.time() - window_start
        try:
            after = scrape(args.metrics_url, args.metrics_token)
        except Exception:
            after = None
    if server is not None:
        time.sleep(args.ramp + 1)
        for _ in range(int(args.duration - 2)):
            try:
                cpu.append(server.cpu_percent(interval=1.0))
            except Exception:
                break
    parts = [out.get(timeout=args.duration + args.ramp + 120) for _ in procs]
    for proc in procs:
        proc.join(timeout=30)
    result = summarize(parts, args, time.time() - started - args.ramp)
    totals_clients = result["connected"]
    if before is not None and after is not None:
        result["server_cost"] = server_report(before, after, window, totals_clients)
    if server is not None:
        try:
            result["server"] = {"rss_mb": round(server.memory_info().rss / 2**20), "threads": server.num_threads(),
                                "cpu_percent_avg": round(statistics.mean(cpu), 1) if cpu else None,
                                "cpu_percent_max": round(max(cpu), 1) if cpu else None}
        except Exception:
            pass
    print(json.dumps(result) if args.json else json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    mp.freeze_support()
    raise SystemExit(main())
