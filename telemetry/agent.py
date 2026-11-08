"""Collect Linux block-device metrics and stream one batch per node."""

import argparse
import asyncio
import csv
import json
import logging
import math
import os
import re
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from schemas import MAX_SAFE_INTEGER, MODEL_LABELS, DriveSample, TelemetryBatch, VolumeSample

LOGGER = logging.getLogger("ssd-agent")
WHOLE_NVME_DEVICE = re.compile(r"nvme[0-9]+n[0-9]+$")
SECTOR_BYTES = 512
MIB_BYTES = 1024**2


@dataclass(frozen=True)
class Counters:
    """The Linux disk counters used to derive one interval's metrics."""

    reads: int
    read_sectors: int
    read_ms: int
    writes: int
    write_sectors: int
    write_ms: int
    in_flight: int
    weighted_ms: int


def read_diskstats(path: Path = Path("/proc/diskstats")) -> dict[str, Counters]:
    """Read a Linux diskstats snapshot and return the counters keyed by kernel device name."""
    snapshot = {}
    for line in path.read_text(encoding="ascii").splitlines():
        fields = line.split()
        if len(fields) >= 14:
            numbers = list(map(int, fields[3:14]))
            snapshot[fields[2]] = Counters(*(numbers[i] for i in (0, 2, 3, 4, 6, 7, 8, 10)))
    return snapshot


def drive_identifier(name: str) -> str:
    """Return the device's persistent sysfs identifier when available, else its kernel name."""
    for identifier in ("wwid", "device/wwid"):
        try:
            value = (Path("/sys/block") / name / identifier).read_text().strip()
            if value:
                return value
        except OSError:
            continue
    return name


def measure(
    device: str, drive_id: str, previous: Counters, current: Counters,
    elapsed_ms: int, smart: tuple[int, str] | None = None,
) -> DriveSample | None:
    """Convert two snapshots into interval metrics, returning None after a counter reset."""
    reads = current.reads - previous.reads
    writes = current.writes - previous.writes
    read_sectors = current.read_sectors - previous.read_sectors
    write_sectors = current.write_sectors - previous.write_sectors
    read_ms = current.read_ms - previous.read_ms
    write_ms = current.write_ms - previous.write_ms
    weighted_ms = current.weighted_ms - previous.weighted_ms
    if elapsed_ms <= 0 or min(reads, writes, read_sectors, write_sectors, read_ms, write_ms, weighted_ms) < 0:
        return None
    seconds = elapsed_ms / 1_000
    return DriveSample(
        drive_id=drive_id, device=device, read_iops=reads / seconds, write_iops=writes / seconds,
        read_mib_per_sec=read_sectors * SECTOR_BYTES / MIB_BYTES / seconds,
        write_mib_per_sec=write_sectors * SECTOR_BYTES / MIB_BYTES / seconds,
        read_latency_ms=read_ms / reads if reads else None,
        write_latency_ms=write_ms / writes if writes else None,
        queue_depth=weighted_ms / elapsed_ms, in_flight=current.in_flight,
        smart_critical_warning=smart[0] if smart else None,
        media_errors_total=smart[1] if smart else None,
    )


def read_smart(devices: list[str]) -> dict[str, tuple[int, str]]:
    """Read NVMe SMART warning bits and media-error counters for accessible devices."""
    health = {}
    for device in devices:
        if not WHOLE_NVME_DEVICE.fullmatch(device):
            continue
        try:
            result = subprocess.run(
                ["nvme", "smart-log", f"/dev/{device}", "-o", "json"],
                capture_output=True, text=True, check=True, timeout=3,
            )
            smart = json.loads(result.stdout)
            warning = int(str(smart["critical_warning"]), 0)
            errors = str(smart["media_errors"])
            if 0 <= warning <= 255 and re.fullmatch(r"[0-9]{1,39}", errors):
                health[device] = (warning, errors)
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
            LOGGER.debug("SMART data unavailable for %s", device)
    return health


def read_volumes(mounts: list[str]) -> list[VolumeSample]:
    """Return filesystem capacity for the mount paths supplied on the command line."""
    volumes = []
    for mount in mounts:
        try:
            stats = os.statvfs(mount)
        except OSError:
            LOGGER.warning("Cannot read filesystem capacity for %s", mount)
            continue
        total = stats.f_blocks * stats.f_frsize
        available = stats.f_bavail * stats.f_frsize
        if 0 < total <= MAX_SAFE_INTEGER and 0 <= available <= total:
            volumes.append(VolumeSample(mount_path=mount, available_bytes=available, total_bytes=total))
    return volumes


async def send_batches(queue: asyncio.Queue[TelemetryBatch], url: str, token: str | None) -> None:
    """Send batches in order using a persistent HTTP connection and one retry on server errors."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(timeout=3, headers=headers) as client:
        while True:
            batch = await queue.get()
            body = batch.model_dump(by_alias=True)
            for attempt in range(2):
                try:
                    response = await client.post(url, json=body)
                    response.raise_for_status()
                    break
                except httpx.HTTPError as exc:
                    client_error = isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500
                    if attempt or client_error:
                        LOGGER.warning("Dropped batch at %s after delivery failed: %s", batch.sample_time_ms, exc)
                        break
                    await asyncio.sleep(0.1)


async def collect(args: argparse.Namespace, recording: csv.DictWriter | None = None) -> None:
    """Sample configured Linux devices on a fixed clock and enqueue each node batch for delivery."""
    loop = asyncio.get_running_loop()
    previous = read_diskstats()
    if args.device and not set(args.device) <= previous.keys():
        raise ValueError("Use the kernel device names listed in /proc/diskstats for --device")
    if not args.device and not any(WHOLE_NVME_DEVICE.fullmatch(name) for name in previous):
        raise ValueError("No whole NVMe devices found. Use --device to select a test device")

    previous_time = loop.time()
    clock_offset = time.time_ns() // 1_000_000 - round(previous_time * 1_000)
    deadline = previous_time + args.interval
    pending: asyncio.Queue[TelemetryBatch] = asyncio.Queue(maxsize=4)
    sender = asyncio.create_task(send_batches(pending, args.url, os.getenv("TELEMETRY_TOKEN")))
    use_smart = args.smart_interval > 0 and shutil.which("nvme") is not None
    smart_task = None
    next_smart = previous_time
    smart = {}
    ids: dict[str, str] = {}
    try:
        while True:
            # Keep clock jitter from producing a sample shorter than the API accepts.
            deadline = max(deadline, previous_time + 0.1)
            await asyncio.sleep(max(0, deadline - loop.time()))
            current = read_diskstats()
            now = loop.time()
            elapsed_ms = round((now - previous_time) * 1_000)
            devices = args.device or sorted(name for name in current if WHOLE_NVME_DEVICE.fullmatch(name))
            ids = {name: ids[name] if name in ids else drive_identifier(name) for name in devices if name in current}
            if smart_task is not None and smart_task.done():
                smart = smart_task.result()
                smart_task = None
            if use_smart and smart_task is None and now >= next_smart:
                smart_task = asyncio.create_task(asyncio.to_thread(read_smart, list(ids)))
                next_smart = now + args.smart_interval

            drives = []
            if 100 <= elapsed_ms <= 60_000:
                for name, drive_id in ids.items():
                    if name in previous:
                        sample = measure(name, drive_id, previous[name], current[name], elapsed_ms, smart.get(name))
                        if sample is not None:
                            drives.append(sample)
            if drives:
                batch = TelemetryBatch(
                    node_id=args.node_id, sample_time_ms=clock_offset + round(now * 1_000),
                    interval_ms=elapsed_ms, drives=drives, volumes=read_volumes(args.mount),
                )
                if recording is not None:
                    recording.writerows({
                        "run_id": args.run_id, "label": args.label,
                        "sample_time_ms": batch.sample_time_ms, "interval_ms": batch.interval_ms,
                        **drive.model_dump(),
                    } for drive in batch.drives)
                if pending.full():
                    dropped = pending.get_nowait()
                    LOGGER.warning("Collector falling behind. Dropped batch at %s", dropped.sample_time_ms)
                pending.put_nowait(batch)
                LOGGER.debug("Sampled %s SSDs at %s", len(drives), batch.sample_time_ms)

            previous, previous_time = current, now
            deadline += args.interval
            if deadline - now < args.interval * 0.9:
                deadline = now + args.interval
    finally:
        sender.cancel()
        await asyncio.gather(sender, return_exceptions=True)
        if smart_task is not None:
            smart_task.cancel()
            await asyncio.gather(smart_task, return_exceptions=True)


def main() -> None:
    """Parse collector settings and run the Linux sampler until interrupted."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000/telemetry")
    parser.add_argument("--node-id", default=socket.gethostname())
    parser.add_argument("--device", action="append", default=[], help="Kernel device name, repeat for each drive")
    parser.add_argument("--mount", action="append", default=[], help="Filesystem path to check for free space")
    parser.add_argument("--interval", type=float, default=1.0, help="Seconds between metric snapshots")
    parser.add_argument("--smart-interval", type=float, default=30, help="Seconds between SMART checks, 0 disables")
    parser.add_argument("--training-csv", type=Path, help="Append measured telemetry to a labeled training CSV")
    parser.add_argument("--label", choices=MODEL_LABELS, help="Ground truth for this controlled run")
    parser.add_argument("--run-id", help="Independent run or drive identifier for evaluation splitting")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    if not 0.1 <= args.interval <= 60 or not (math.isfinite(args.smart_interval) and args.smart_interval >= 0):
        parser.error("Use an interval from 0.1 to 60 seconds and a nonnegative SMART interval")
    if len(set(args.device)) != len(args.device) or any("/" in name for name in args.device):
        parser.error("Pass distinct kernel device names such as nvme0n1 or dm-0")
    recording_options = (args.training_csv, args.label, args.run_id)
    if any(recording_options) and not all(recording_options):
        parser.error("Supply --training-csv, --label, and --run-id together")
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    try:
        if args.training_csv is None:
            asyncio.run(collect(args))
        else:
            fields = ["run_id", "label", "sample_time_ms", "interval_ms", *DriveSample.model_fields]
            with args.training_csv.open("a", newline="", encoding="utf-8", buffering=1) as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                if stream.tell() == 0:
                    writer.writeheader()
                asyncio.run(collect(args, writer))
    except KeyboardInterrupt:
        LOGGER.info("Collector stopped")
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Collector error: {exc}\n")


if __name__ == "__main__":
    main()
