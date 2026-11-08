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
from schemas import (
    ACTIVE_DEMO_ACTION_STATES, DEMO_DRIVES_PER_NODE, DEMO_NODE_COUNTS, MAX_DEMO_NODES,
    MODEL_LABELS, DriveSample, TelemetryBatch,
)

DEFAULT_API = "http://127.0.0.1:8000/telemetry"
DriveProfile = tuple[str, float]
DemoActions = dict[str, list[dict[str, str]]]
DEMO_INTERVAL = 10
BASE_INTERVAL = 3
FAULT_SHARE = 0.25



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


def route_writes(
    readings: list[DriveSample], profiles: list[DriveProfile], actions: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Redistribute new writes within a node and return policies applied to this sample."""
    if not actions:
        return []
    actions = [action for action in actions if action["state"] in ACTIVE_DEMO_ACTION_STATES]
    if not actions:
        return []
    positions = {drive.drive_id: i for i, drive in enumerate(readings)}
    targets = {action["driveId"] for action in actions}
    quarantined = {action["driveId"] for action in actions if action["kind"] == "quarantine"}
    receivers = [i for i, drive in enumerate(readings)
                 if drive.drive_id not in targets and not drive.smart_critical_warning]
    moved_rate = moved_iops = 0.0
    applied = []
    for action in sorted(actions, key=lambda item: item["kind"] != "quarantine"):
        drive_id = action["driveId"]
        if drive_id not in positions:
            continue
        i = positions[drive_id]
        drive = readings[i]
        quarantine = action["kind"] == "quarantine"
        if not quarantine and (not receivers or drive_id in quarantined):
            continue
        rate = 0.0 if quarantine else min(drive.write_mib_per_sec, 60 * profiles[i][1])
        iops = drive.write_iops * rate / drive.write_mib_per_sec if drive.write_mib_per_sec else 0.0
        moved_rate += drive.write_mib_per_sec - rate
        moved_iops += drive.write_iops - iops
        readings[i] = drive.model_copy(update={
            "write_mib_per_sec": rate, "write_iops": iops,
            "read_latency_ms": drive.read_latency_ms if quarantine else min(drive.read_latency_ms or 0.0, 2.0),
            "in_flight": min(drive.in_flight, round(2 * profiles[i][1])),
            "write_latency_ms": None if quarantine else min(drive.write_latency_ms or 0.0, 2.0),
            "queue_depth": min(drive.queue_depth, 2 * profiles[i][1]),
            "smart_critical_warning": (drive.smart_critical_warning or 0) | 4 if quarantine else drive.smart_critical_warning,
        })
        applied.append(action)
    for i in receivers:
        drive = readings[i]
        readings[i] = drive.model_copy(update={
            "write_mib_per_sec": drive.write_mib_per_sec + moved_rate / len(receivers),
            "write_iops": drive.write_iops + moved_iops / len(receivers),
        })
    return applied


async def confirm_actions(client: aiohttp.ClientSession, url: str, actions: list[dict[str, str]]) -> None:
    for action in actions:
        if action["state"] != "pending":
            continue
        key = {field: action[field] for field in ("nodeId", "driveId", "kind", "actionId")}
        try:
            async with client.post(url, json=key, raise_for_status=True) as response:
                action["state"] = (await response.json())["state"]
        except (aiohttp.ClientError, TimeoutError):
            pass  # Retry confirmation after the next successful telemetry batch.


async def replay_node(
    client: aiohttp.ClientSession, args: argparse.Namespace, index: int, drives: list[DriveProfile],
    start: float, epoch_ms: int, phase: float, stats: ReplayStats, actions: DemoActions, first_tick: int = 0,
) -> None:
    """Replay one independently timed node and record its deliveries in the fleet totals."""
    rng = random.Random(args.seed + index)
    interval_ms = round(args.interval * 1_000)
    window_ticks = math.ceil(WINDOW_MS / interval_ms)
    incident_chance = 1 - (1 - args.failure_chance) ** (args.interval / BASE_INTERVAL)
    ticks = range(math.ceil(args.seconds / args.interval)) if args.seconds else count(first_tick)
    token = os.getenv("TELEMETRY_TOKEN")
    scenario = NodeScenario()
    node_id = f"sim-node-{index:04}"
    affected_drive = drives[index % len(drives)][0]
    confirm_url = urljoin(args.url, "/demo/actions/applied")
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
        readings = [simulate_drive(rng, drive, scenario.label if drive == affected_drive else "healthy",
                                   load, errors.get(drive, 0))
                    for drive, load in drives]
        errors.update((reading.drive_id, int(reading.media_errors_total or 0)) for reading in readings)
        applied = route_writes(readings, drives, actions.get(node_id, []))
        batch = TelemetryBatch(
            node_id=node_id, sample_time_ms=epoch_ms + round(offset * 1_000),
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
            if applied:
                await confirm_actions(client, confirm_url, applied)


def group_profiles(drives: int, drives_per_node: int, seed: int) -> list[list[DriveProfile]]:
    rng = random.Random(seed)
    profiles = [(f"ssd-{index:04}", rng.uniform(0.5, 2.8)) for index in range(drives)]
    return [profiles[i:i + drives_per_node] for i in range(0, drives, drives_per_node)]


async def replay(args: argparse.Namespace) -> int:
    """Replay a fixed fleet and print throughput, request latency and failures."""
    groups = group_profiles(args.drives, args.drives_per_node, args.seed)
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
                    fleet.create_task(replay_node(client, args, node, group, start, initial_sample_time, phase, stats, {}))
                while not args.seconds:
                    await asyncio.sleep(30)
                    stats.report(loop.time() - start)
            duration = math.ceil(args.seconds / args.interval) * args.interval
            await asyncio.sleep(max(0, start + duration - loop.time()))
        finally:
            stats.report(loop.time() - start)
    return int(stats.failures > 0 or stats.missed > 0)


async def dashboard_replay(args: argparse.Namespace) -> int:
    """Resize the fleet while keeping each retained node's sampling clock and state."""
    control_url = urljoin(args.url, "/demo/fleet")
    actions_url = urljoin(args.url, "/demo/actions")
    config = argparse.Namespace(**vars(args))
    config.interval = DEMO_INTERVAL
    groups = group_profiles(MAX_DEMO_NODES * DEMO_DRIVES_PER_NODE, DEMO_DRIVES_PER_NODE, args.seed)
    actions: DemoActions = {}
    tasks: list[asyncio.Task[None]] = []
    stats = ReplayStats()
    loop = asyncio.get_running_loop()
    start = loop.time()
    epoch_ms = time.time_ns() // 1_000_000
    next_report = start + 30
    unavailable = False
    async with (
        aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2)) as controls,
        aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=200),
                              timeout=aiohttp.ClientTimeout(total=15)) as client,
        asyncio.TaskGroup() as fleet,
    ):
        try:
            while True:
                try:
                    async with controls.get(control_url, raise_for_status=True) as response:
                        nodes = (await response.json())["nodes"]
                    async with controls.get(actions_url, raise_for_status=True) as response:
                        latest_actions = (await response.json())["actions"]
                    actions.clear()
                    for action in latest_actions:
                        actions.setdefault(action["nodeId"], []).append(action)
                    unavailable = False
                except (aiohttp.ClientError, TimeoutError):
                    if not unavailable:
                        print("Waiting for the local demo fleet control", flush=True)
                        unavailable = True
                    await asyncio.sleep(1)
                    continue

                if nodes in DEMO_NODE_COUNTS and nodes != len(tasks):
                    retired = tasks[nodes:]
                    del tasks[nodes:]
                    for task in retired:
                        task.cancel()
                    if retired:
                        await asyncio.gather(*retired, return_exceptions=True)
                    for node in range(len(tasks), nodes):
                        # Spread even a small prefix of the fleet across the sampling interval.
                        phase = (node * 619 % MAX_DEMO_NODES) * DEMO_INTERVAL / MAX_DEMO_NODES
                        first_tick = max(0, math.ceil((loop.time() - start - phase) / DEMO_INTERVAL))
                        tasks.append(fleet.create_task(replay_node(
                            client, config, node, groups[node], start, epoch_ms, phase, stats, actions, first_tick,
                        )))
                    print(f"Monitoring {nodes:,} nodes with {DEMO_DRIVES_PER_NODE} SSDs each, "
                          f"sampled every {DEMO_INTERVAL}s with a {config.failure_chance:.2%} incident chance "
                          f"per eligible three seconds", flush=True)
                if loop.time() >= next_report:
                    stats.report(loop.time() - start)
                    next_report = loop.time() + 30
                await asyncio.sleep(1)
        finally:
            for task in tasks:
                task.cancel()


def main() -> int:
    """Parse CLI arguments and generate training data or run the live replay."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--training-csv", type=Path)
    parser.add_argument("--runs-per-class", type=int, default=24)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--url", default=DEFAULT_API)
    fleet_size = parser.add_mutually_exclusive_group()
    fleet_size.add_argument("--dashboard-control", action="store_true", help="Follow the local dashboard fleet size with ten SSDs per node")
    fleet_size.add_argument("--nodes", type=int, help=f"Number of nodes from 1 to {MAX_DEMO_NODES:,} (default {MAX_DEMO_NODES:,})")
    fleet_size.add_argument("--drives", type=int, help="Total drives for the legacy replay mode")
    parser.add_argument("--drives-per-node", type=int, help="SSDs per node in fixed-size replay (default 1)")
    parser.add_argument("--interval", type=float, help="Seconds between samples in fixed-size replay (default 3)")
    parser.add_argument("--seconds", type=float, default=0, help="Duration in seconds, 0 runs until interrupted")
    parser.add_argument("--failure-chance", type=float, default=0.10,
                        help="Probability that an eligible node starts a heavy-write or drive-fault incident in three seconds")
    parser.add_argument("--burst", action="store_true", help="Start every node's sampling clock together")
    args = parser.parse_args()
    if args.training_csv:
        if args.runs_per_class < 5:
            parser.error("Use at least five independent runs per class")
        write_training_csv(args.training_csv, args.runs_per_class, args.seed)
        print(f"Wrote labeled synthetic runs to {args.training_csv}")
        return 0
    if args.nodes is not None and not 1 <= args.nodes <= MAX_DEMO_NODES:
        parser.error(f"Set nodes between 1 and {MAX_DEMO_NODES:,}")
    if args.dashboard_control and args.drives_per_node not in (None, DEMO_DRIVES_PER_NODE):
        parser.error("Dashboard control uses ten drives per node")
    if args.drives_per_node is None:
        args.drives_per_node = DEMO_DRIVES_PER_NODE if args.dashboard_control else 1
    if args.drives is None:
        args.drives = (args.nodes or MAX_DEMO_NODES) * args.drives_per_node
    if args.interval is None:
        args.interval = BASE_INTERVAL
    if not (args.drives > 0 and 1 <= args.drives_per_node <= 2048 and 0.1 <= args.interval <= 60
            and math.isfinite(args.seconds) and args.seconds >= 0):
        parser.error("Set positive drives, nonnegative seconds, drives-per-node from 1 to 2048, interval from 0.1 to 60")
    if math.ceil(args.drives / args.drives_per_node) > MAX_DEMO_NODES:
        parser.error(f"Set at most {MAX_DEMO_NODES:,} nodes")
    if not 0 <= args.failure_chance <= 1:
        parser.error("Set failure-chance between 0 and 1")
    if args.dashboard_control and args.seconds:
        parser.error("Dashboard control needs continuous replay")
    try:
        return asyncio.run(dashboard_replay(args) if args.dashboard_control else replay(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
