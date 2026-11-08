"""Generate labeled training runs or replay synthetic nodes through the live API."""

import argparse
import asyncio
import csv
import math
import os
import random
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from itertools import count
from pathlib import Path
from urllib.parse import urljoin

import aiohttp

from features import WINDOW_MS
from schemas import DEMO_NODE_COUNTS, MODEL_LABELS, DriveSample, TelemetryBatch

DEFAULT_API = "http://127.0.0.1:8000/telemetry"
DriveProfile = tuple[str, float]
BASE_INTERVAL = 3
FAULT_SHARE = 0.10



@dataclass
class NodeScenario:
    """Sustain an injected condition long enough for the rolling classifier to see it."""

    label: str = "healthy"
    next_transition: int = 0

    def advance(self, rng: random.Random, tick: int, window_ticks: int, failure_chance: float) -> None:
        if tick < self.next_transition:
            return
        if self.label != "healthy":
            self.label = "healthy"
            self.next_transition = tick + window_ticks
        elif rng.random() < failure_chance:
            self.label = "drive_fault" if rng.random() < FAULT_SHARE else "heavy_writes"
            self.next_transition = tick + rng.randint(window_ticks, 2 * window_ticks)
        elif tick == 0:
            self.next_transition = window_ticks


@dataclass
class ReplayStats:
    """Fleet totals and a bounded buffer of recent API timings."""

    rtts: deque[float] = field(default_factory=lambda: deque(maxlen=8192))
    counts: Counter[str] = field(default_factory=Counter)
    accepted: int = 0
    retries: int = 0
    failures: int = 0
    missed: int = 0
    first_error: str | None = None

    def report(self, elapsed: float) -> None:
        print(f"{elapsed:.1f}s: {self.accepted} accepted batches, {self.failures} failures, "
              f"{self.retries} retries, {self.missed} missed intervals", flush=True)
        samples = sum(self.counts.values())
        print(f"{samples} accepted SSD samples, {samples / elapsed:.0f} samples/sec", flush=True)
        if self.rtts:
            p95 = sorted(self.rtts)[math.ceil(0.95 * len(self.rtts)) - 1]
            print(f"Recent p95 API round-trip {p95:.1f} ms", flush=True)
        print(dict(sorted(self.counts.items())), flush=True)
        if self.first_error:
            print(f"First error: {self.first_error}", flush=True)


def simulate_drive(rng: random.Random, drive_id: str, label: str, load: float, previous_errors: int = 0) -> DriveSample:
    """Return one synthetic drive sample with load and latency drawn for the given scenario."""
    read_rate = rng.uniform(7, 55) * load
    write_rate = rng.uniform(12, 200) * load
    read_latency = rng.uniform(0.25, 1.5) * (1 + load / 2)
    write_latency = rng.uniform(0.25, 2.2) * (1 + load / 2)
    queue_depth = rng.uniform(0.1, 4) * load
    media_errors = previous_errors
    warning = 0
    if label == "heavy_writes":
        write_rate *= rng.uniform(2.1, 4.4)
        read_latency += rng.uniform(5, 15)
        write_latency += rng.uniform(2, 12)
        queue_depth += rng.uniform(3, 20)
    elif label == "drive_fault":
        # A faulty drive can still have an active write workload.
        if rng.random() < 0.3:
            write_rate *= rng.uniform(2.1, 4.4)
        warning = rng.choice((0, 0, 0x04, 0x05))
        media_errors += rng.randrange(4)
        read_latency += rng.uniform(5, 15)
        write_latency += rng.uniform(1, 13)
        queue_depth += rng.uniform(1, 16)

    return DriveSample(
        drive_id=drive_id, device=drive_id,
        read_iops=read_rate * rng.uniform(64, 256),
        write_iops=write_rate * rng.uniform(8, 64),
        read_mib_per_sec=read_rate, write_mib_per_sec=write_rate,
        read_latency_ms=read_latency, write_latency_ms=write_latency, queue_depth=queue_depth,
        in_flight=round(queue_depth),
        smart_critical_warning=warning, media_errors_total=str(media_errors),
    )


def write_training_csv(path: Path, runs_per_class: int, seed: int) -> None:
    """Write a CSV of independent, labeled 65-second synthetic runs for each class."""
    rng = random.Random(seed)
    fields = ["run_id", "label", "sample_time_ms", "interval_ms", *DriveSample.model_fields]
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for label in MODEL_LABELS:
            for run in range(runs_per_class):
                run_id = f"{label}-{run}"
                load = rng.uniform(0.5, 2.8)
                errors = 0
                for second in range(1, 66):
                    sample = simulate_drive(rng, "ssd-0000", label, load * rng.uniform(0.8, 1.2), errors)
                    errors = int(sample.media_errors_total or 0)
                    writer.writerow({
                        "run_id": run_id, "label": label, "sample_time_ms": second * 1_000,
                        "interval_ms": 1_000, **sample.model_dump(),
                    })


async def post_batch(
    client: aiohttp.ClientSession, url: str, batch: TelemetryBatch, token: str | None
) -> tuple[float, Counter[str], str | None, int]:
    """Submit one node batch and report its timing, class counts, error and any retry."""
    start = time.perf_counter()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    body = batch.model_dump(by_alias=True)
    retry = 0
    try:
        for attempt in range(2):
            try:
                async with client.post(url, json=body, headers=headers, allow_redirects=False) as response:
                    if not 200 <= response.status < 300:
                        raise aiohttp.ClientResponseError(
                            response.request_info, response.history, status=response.status
                        )
                    result = await response.json()
                break
            except (aiohttp.ClientError, TimeoutError) as exc:
                if attempt or (isinstance(exc, aiohttp.ClientResponseError) and exc.status < 500):
                    raise
                retry = 1
                await asyncio.sleep(0.05)
        labels = Counter(drive["classification"] for drive in result["drives"])
        if sum(labels.values()) != len(batch.drives):
            raise ValueError("Response count differs from the submitted drive count")
        return (time.perf_counter() - start) * 1_000, labels, None, retry
    except (aiohttp.ClientError, TimeoutError, KeyError, TypeError, ValueError) as exc:
        return (time.perf_counter() - start) * 1_000, Counter(), f"{type(exc).__name__}: {exc}", retry


async def replay_node(
    client: aiohttp.ClientSession, args: argparse.Namespace, index: int, drives: list[DriveProfile],
    start: float, epoch_ms: int, phase: float, stats: ReplayStats,
) -> None:
    """Replay one independently timed node and record its deliveries in the fleet totals."""
    rng = random.Random(args.seed + index)
    interval_ms = round(args.interval * 1_000)
    window_ticks = math.ceil(WINDOW_MS / interval_ms)
    incident_chance = 1 - (1 - args.failure_chance) ** (args.interval / BASE_INTERVAL)
    ticks = range(math.ceil(args.seconds / args.interval)) if args.seconds else count()
    token = os.getenv("TELEMETRY_TOKEN")
    scenario = NodeScenario()
    errors: dict[str, int] = {}
    loop = asyncio.get_running_loop()
    for tick in ticks:
        offset = phase + tick * args.interval
        scheduled = start + offset
        delay = scheduled - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)
        scenario.advance(rng, tick, window_ticks, incident_chance)
        if loop.time() >= scheduled + args.interval:
            stats.missed += 1
            continue
        readings = [simulate_drive(rng, drive, scenario.label, load, errors.get(drive, 0))
                    for drive, load in drives]
        errors.update((reading.drive_id, int(reading.media_errors_total or 0)) for reading in readings)
        batch = TelemetryBatch(
            node_id=f"sim-node-{index:04}", sample_time_ms=epoch_ms + round(offset * 1_000),
            interval_ms=interval_ms,
            drives=readings,
        )
        latency, labels, error, retry = await post_batch(client, args.url, batch, token)
        stats.retries += retry
        if error is not None:
            stats.failures += 1
            stats.first_error = stats.first_error or error
        else:
            stats.accepted += 1
            stats.rtts.append(latency)
            stats.counts.update(labels)


async def replay(args: argparse.Namespace) -> int:
    """Replay the configured logical nodes and print throughput, request latency and failures."""
    rng = random.Random(args.seed)
    profiles = [
        (f"ssd-{index:04}", rng.uniform(0.5, 2.8))
        for index in range(args.drives)
    ]
    groups = [profiles[i:i + args.drives_per_node] for i in range(0, len(profiles), args.drives_per_node)]
    stats = ReplayStats()
    loop = asyncio.get_running_loop()
    print(f"Sending {args.drives:,} drive readings across {len(groups):,} "
          f"{'node' if len(groups) == 1 else 'nodes'} every {args.interval:g}s "
          f"with a {args.failure_chance:.2%} incident chance per eligible three seconds", flush=True)
    async with aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit=200), timeout=aiohttp.ClientTimeout(total=15)
    ) as client:
        start = loop.time()
        initial_sample_time = time.time_ns() // 1_000_000
        try:
            async with asyncio.TaskGroup() as fleet:
                for node, group in enumerate(groups):
                    phase = 0 if args.burst else node * args.interval / len(groups)
                    fleet.create_task(replay_node(client, args, node, group, start, initial_sample_time, phase, stats))
                while not args.seconds:
                    await asyncio.sleep(30)
                    stats.report(loop.time() - start)
            duration = math.ceil(args.seconds / args.interval) * args.interval
            await asyncio.sleep(max(0, start + duration - loop.time()))
        finally:
            stats.report(loop.time() - start)
    return int(stats.failures > 0 or stats.missed > 0)


def fleet_interval(nodes: int) -> int:
    return min(12, max(BASE_INTERVAL, math.ceil(nodes * 12 / 10_000)))


async def dashboard_replay(args: argparse.Namespace) -> int:
    """Follow the local dashboard's requested fleet size until interrupted."""
    control_url = urljoin(args.url, "/demo/fleet")
    active_nodes = 0
    replay_task: asyncio.Task[int] | None = None
    unavailable = False
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2)) as client:
        try:
            while True:
                try:
                    async with client.get(control_url, raise_for_status=True) as response:
                        nodes = (await response.json())["nodes"]
                    unavailable = False
                except (aiohttp.ClientError, TimeoutError):
                    if not unavailable:
                        print("Waiting for the local demo fleet control", flush=True)
                        unavailable = True
                    await asyncio.sleep(1)
                    continue
                if nodes in DEMO_NODE_COUNTS and nodes != active_nodes:
                    if replay_task is not None:
                        replay_task.cancel()
                        await asyncio.gather(replay_task, return_exceptions=True)
                    config = argparse.Namespace(**vars(args))
                    config.drives = nodes
                    config.interval = fleet_interval(nodes)
                    active_nodes = nodes
                    replay_task = asyncio.create_task(replay(config))
                if replay_task is not None and replay_task.done():
                    return replay_task.result()
                await asyncio.sleep(1)
        finally:
            if replay_task is not None:
                replay_task.cancel()
                await asyncio.gather(replay_task, return_exceptions=True)


def main() -> int:
    """Parse CLI arguments and generate training data or run the live replay."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--training-csv", type=Path)
    parser.add_argument("--runs-per-class", type=int, default=24)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--url", default=DEFAULT_API)
    fleet_size = parser.add_mutually_exclusive_group()
    fleet_size.add_argument("--dashboard-control", action="store_true", help="Follow the local dashboard fleet size")
    fleet_size.add_argument("--nodes", type=int, help="Number of nodes from 1 to 10,000 (default 1,000)")
    fleet_size.add_argument("--drives", type=int, help="Total drives for the legacy replay mode")
    parser.add_argument("--drives-per-node", type=int, default=1)
    parser.add_argument("--interval", type=float, help="Seconds between samples (default 3–12 based on fleet size)")
    parser.add_argument("--seconds", type=float, default=0, help="Duration in seconds, 0 runs until interrupted")
    parser.add_argument("--failure-chance", type=float, default=0.003,
                        help="Probability of an incident starting per eligible three seconds (90%% heavy writes, 10%% faults)")
    parser.add_argument("--burst", action="store_true", help="Start every node's sampling clock together")
    args = parser.parse_args()
    if args.training_csv:
        if args.runs_per_class < 5:
            parser.error("Use at least five independent runs per class")
        write_training_csv(args.training_csv, args.runs_per_class, args.seed)
        print(f"Wrote labeled synthetic runs to {args.training_csv}")
        return 0
    if args.nodes is not None and not 1 <= args.nodes <= 10_000:
        parser.error("Set nodes between 1 and 10,000")
    if args.drives is None:
        args.drives = (args.nodes or 1_000) * args.drives_per_node
    if args.interval is None:
        nodes = math.ceil(args.drives / max(1, args.drives_per_node))
        args.interval = fleet_interval(nodes)
    if not (args.drives > 0 and 1 <= args.drives_per_node <= 2048 and 0.1 <= args.interval <= 60
            and math.isfinite(args.seconds) and args.seconds >= 0):
        parser.error("Set positive drives, nonnegative seconds, drives-per-node from 1 to 2048, interval from 0.1 to 60")
    if not 0 <= args.failure_chance <= 1:
        parser.error("Set failure-chance between 0 and 1")
    if args.dashboard_control and (args.seconds or args.drives_per_node != 1):
        parser.error("Dashboard control needs continuous replay with one drive per node")
    try:
        return asyncio.run(dashboard_replay(args) if args.dashboard_control else replay(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
