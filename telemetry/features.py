"""Shared rolling-window calculations for offline training and inference."""

import math
from collections import deque
from dataclasses import dataclass
from typing import Iterable

from schemas import DriveSample

WINDOW_MS = 30_000
MIN_COVERED_MS = 29_000
RATE_FIELDS = (
    "read_iops", "write_iops", "read_mib_per_sec", "write_mib_per_sec", "queue_depth"
)
# NVMe critical-warning bits for low spare capacity, degraded reliability, and read-only media.
MEDIA_WARNING_BITS = 0x0D
FEATURE_NAMES = (
    *RATE_FIELDS, "read_latency_ms", "write_latency_ms", "write_rate_change",
    "write_share", "smart_media_warning", "media_error_increase",
)


@dataclass(frozen=True)
class TimedSample:
    """One measurement and the Unix time and length of its interval."""

    time_ms: int
    interval_ms: int
    drive: DriveSample


@dataclass(frozen=True)
class Window:
    """Ordered features and the amount of observed time behind them."""

    values: tuple[float, ...]
    samples: int
    covered_ms: int

    @property
    def ready(self) -> bool:
        """Return whether this window has enough readings and time to classify."""
        return self.samples >= 3 and self.covered_ms >= MIN_COVERED_MS


def append_sample(history: deque[TimedSample], sample: TimedSample) -> Window:
    """Add an ordered sample, discard expired readings and return the current features."""
    history.append(sample)
    cutoff = sample.time_ms - WINDOW_MS
    while history[0].time_ms <= cutoff:
        history.popleft()
    return summarize(history, cutoff)


def mean_latency(
    intervals: list[tuple[DriveSample, int]], latency_field: str, iops_field: str
) -> float:
    """Return mean latency weighted by the number of completed operations in each interval."""
    weighted = completions = 0.0
    for drive, duration in intervals:
        latency = getattr(drive, latency_field)
        if latency is not None:
            operations = getattr(drive, iops_field) * duration / 1_000
            weighted += latency * operations
            completions += operations
    return weighted / completions if completions else 0.0


def summarize(history: Iterable[TimedSample], cutoff: int) -> Window:
    """Turn ordered samples after the cutoff into features without counting time gaps as data."""
    intervals = []
    previous_end = cutoff
    for sample in history:
        duration = sample.time_ms - max(previous_end, sample.time_ms - sample.interval_ms)
        if duration > 0:
            intervals.append((sample.drive, duration))
            previous_end = sample.time_ms

    covered = sum(duration for _, duration in intervals)
    if len(intervals) < 3 or covered < MIN_COVERED_MS:
        return Window((), len(intervals), covered)

    rates = tuple(
        sum(getattr(drive, field) * duration for drive, duration in intervals) / covered
        for field in RATE_FIELDS
    )
    write_change = intervals[-1][0].write_mib_per_sec - intervals[0][0].write_mib_per_sec
    read_rate, write_rate = rates[2:4]
    throughput = read_rate + write_rate
    media_warning = any(
        (drive.smart_critical_warning or 0) & MEDIA_WARNING_BITS for drive, _ in intervals
    )
    error_counts = [
        int(drive.media_errors_total) for drive, _ in intervals if drive.media_errors_total is not None
    ]
    error_increase = max(0, error_counts[-1] - error_counts[0]) if error_counts else 0
    return Window(
        (
            *rates,
            mean_latency(intervals, "read_latency_ms", "read_iops"),
            mean_latency(intervals, "write_latency_ms", "write_iops"),
            write_change,
            write_rate / throughput if throughput else 0.0,
            float(media_warning),
            math.log1p(error_increase),
        ),
        len(intervals),
        covered,
    )
