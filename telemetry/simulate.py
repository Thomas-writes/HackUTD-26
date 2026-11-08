"""Generate labeled training runs or replay synthetic nodes through the live API."""

import argparse
import asyncio
import csv
import math
import os
import random
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp

from schemas import MODEL_LABELS, DriveSample, TelemetryBatch

DEFAULT_API = "http://127.0.0.1:8000/telemetry"
DriveProfile = tuple[str, str, float]


@dataclass
class ReplayStats:
    """Delivery timings, classifications and counts for one simulated node."""

    rtts: list[float] = field(default_factory=list)
    counts: Counter[str] = field(default_factory=Counter)
    retries: int = 0
    failures: int = 0
    missed: int = 0
    first_error: str | None = None


def simulate_drive(rng: random.Random, drive_id: str, label: str, load: float) -> DriveSample:
    """Return one synthetic drive sample with load and latency drawn for the given scenario."""
    read_rate = rng.uniform(7, 55) * load
    write_rate = rng.uniform(12, 200) * load
    read_latency = rng.uniform(0.25, 1.5) * (1 + load / 2)
    write_latency = rng.uniform(0.25, 2.2) * (1 + load / 2)
    queue_depth = rng.uniform(0.1, 4) * load
    if label == "likely_workload_contention":
        write_rate *= rng.uniform(2.1, 4.4)
        read_latency += rng.uniform(5, 15)
        write_latency += rng.uniform(2, 12)
        queue_depth += rng.uniform(3, 20)
    elif label == "unexpected_slowdown":
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
                for second in range(1, 66):
                    sample = simulate_drive(rng, "ssd-0000", label, load * rng.uniform(0.8, 1.2))
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
    start: float, epoch_ms: int, phase: float,
) -> ReplayStats:
    """Replay one independently timed node and return its observed delivery statistics."""
    rng = random.Random(args.seed + index)
    interval_ms = round(args.interval * 1_000)
    iterations = math.ceil(args.seconds / args.interval)
    token = os.getenv("TELEMETRY_TOKEN")
    stats = ReplayStats()
    loop = asyncio.get_running_loop()
    for tick in range(iterations):
        offset = phase + tick * args.interval
        scheduled = start + offset
        delay = scheduled - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)
        if loop.time() >= scheduled + args.interval:
            stats.missed += 1
            continue
        batch = TelemetryBatch(
            node_id=f"sim-node-{index:04}", sample_time_ms=epoch_ms + round(offset * 1_000),
            interval_ms=interval_ms,
            drives=[simulate_drive(rng, drive, label, load) for drive, label, load in drives],
        )
        latency, labels, error, retry = await post_batch(client, args.url, batch, token)
        stats.retries += retry
        if error is not None:
            stats.failures += 1
            stats.first_error = stats.first_error or error
        else:
            stats.rtts.append(latency)
            stats.counts.update(labels)
    return stats


async def replay(args: argparse.Namespace) -> int:
    """Replay the configured logical nodes and print throughput, request latency and failures."""
    rng = random.Random(args.seed)
    profiles = [
        (f"ssd-{index:04}", MODEL_LABELS[index % len(MODEL_LABELS)], rng.uniform(0.5, 2.8))
        for index in range(args.drives)
    ]
    groups = [profiles[i:i + args.drives_per_node] for i in range(0, len(profiles), args.drives_per_node)]
    iterations = math.ceil(args.seconds / args.interval)
    rtts = []
    counts: Counter[str] = Counter()
    failures = retries = missed = 0
    first_error = None
    loop = asyncio.get_running_loop()
    async with aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit=200), timeout=aiohttp.ClientTimeout(total=15)
    ) as client:
        start = loop.time()
        initial_sample_time = time.time_ns() // 1_000_000
        results = await asyncio.gather(*(
            replay_node(
                client, args, node, group, start, initial_sample_time,
                0 if args.burst else node * args.interval / len(groups),
            )
            for node, group in enumerate(groups)
        ))
        for stats in results:
            rtts.extend(stats.rtts)
            counts.update(stats.counts)
            failures += stats.failures
            retries += stats.retries
            missed += stats.missed
            first_error = first_error or stats.first_error
        await asyncio.sleep(max(0, start + iterations * args.interval - loop.time()))

    elapsed = loop.time() - start
    print(f"{args.drives} SSDs across {len(groups)} nodes, {elapsed:.1f} seconds")
    print(f"{len(rtts)} accepted batches, {failures} failures, {retries} retries, {missed} missed intervals")
    samples = sum(counts.values())
    print(f"{samples} accepted SSD samples, {samples / elapsed:.0f} samples/sec")
    if rtts:
        p95 = sorted(rtts)[math.ceil(0.95 * len(rtts)) - 1]
        print(f"p95 API round-trip {p95:.1f} ms")
    print(dict(sorted(counts.items())))
    if first_error:
        print(f"First error: {first_error}")
    return int(failures > 0 or missed > 0)


def main() -> int:
    """Parse CLI arguments and generate training data or run the live replay."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--training-csv", type=Path)
    parser.add_argument("--runs-per-class", type=int, default=24)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--url", default=DEFAULT_API)
    parser.add_argument("--drives", type=int, default=10)
    parser.add_argument("--drives-per-node", type=int, default=10)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--seconds", type=float, default=40)
    parser.add_argument("--burst", action="store_true", help="Start every node's sampling clock together")
    args = parser.parse_args()
    if args.training_csv:
        if args.runs_per_class < 5:
            parser.error("Use at least five independent runs per class")
        write_training_csv(args.training_csv, args.runs_per_class, args.seed)
        print(f"Wrote labeled synthetic runs to {args.training_csv}")
        return 0
    if not (args.drives > 0 and 1 <= args.drives_per_node <= 2048 and 0.1 <= args.interval <= 60
            and math.isfinite(args.seconds) and args.seconds > 0):
        parser.error("Set positive drives and seconds, drives-per-node from 1 to 2048, interval from 0.1 to 60")
    return asyncio.run(replay(args))


if __name__ == "__main__":
    raise SystemExit(main())
