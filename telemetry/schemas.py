"""Versioned JSON contract for the collectors and SpacetimeDB handoff."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

MAX_SAFE_INTEGER = 2**53 - 1
DriveId = Annotated[str, Field(min_length=1, max_length=200)]
NodeId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")]
Timestamp = Annotated[int, Field(gt=0, le=MAX_SAFE_INTEGER)]
Interval = Annotated[int, Field(ge=100, le=60_000)]
NonNegative = Annotated[float, Field(ge=0, le=1e12)]
Classification = Literal[
    "healthy", "heavy_writes", "drive_fault", "warming_up"
]
DEMO_NODE_COUNTS = (1, 10, 100, 500, 1_000, 2_500, 5_000, 10_000)
MODEL_LABELS = ("healthy", "heavy_writes", "drive_fault")


class WireModel(BaseModel):
    """Use snake_case in Python and camelCase in the JSON contract."""

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid", allow_inf_nan=False
    )


class DriveSample(WireModel):
    """One drive's metrics measured over the enclosing batch interval."""

    drive_id: DriveId
    device: DriveId
    read_iops: NonNegative
    write_iops: NonNegative
    read_mib_per_sec: NonNegative
    write_mib_per_sec: NonNegative
    read_latency_ms: NonNegative | None = None
    write_latency_ms: NonNegative | None = None
    queue_depth: NonNegative
    in_flight: Annotated[int, Field(ge=0, le=MAX_SAFE_INTEGER)] = 0
    smart_critical_warning: Annotated[int, Field(ge=0, le=255)] | None = None
    media_errors_total: Annotated[str, Field(pattern=r"^[0-9]{1,39}$")] | None = None


class VolumeSample(WireModel):
    """Capacity for an explicitly monitored filesystem mount on a node."""

    mount_path: Annotated[str, Field(min_length=1, max_length=1024)]
    available_bytes: Annotated[int, Field(ge=0, le=MAX_SAFE_INTEGER)]
    total_bytes: Annotated[int, Field(gt=0, le=MAX_SAFE_INTEGER)]


class TelemetryBatch(WireModel):
    """Collector input for one node's drives at one sampling time."""

    schema_version: Literal[1] = 1
    node_id: NodeId
    sample_time_ms: Timestamp
    interval_ms: Interval
    drives: list[DriveSample] = Field(min_length=1, max_length=2048)
    volumes: list[VolumeSample] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def unique_drive_ids(self) -> "TelemetryBatch":
        """Return this batch after checking it has one sample per drive ID."""
        if len({drive.drive_id for drive in self.drives}) != len(self.drives):
            raise ValueError("A batch must contain unique drive IDs")
        return self


class DriveAssessment(DriveSample):
    """Latest measurements and the resulting label for a single drive."""

    classification: Classification
    model_score: Annotated[float, Field(ge=0, le=1)] | None
    window_samples: int
    window_covered_ms: int
    next_check: str


class AssessmentBatch(WireModel):
    """The same document returned by FastAPI and sent to the reducer."""

    schema_version: Literal[1] = 1
    node_id: NodeId
    sample_time_ms: Timestamp
    interval_ms: Interval
    model_version: str
    drives: list[DriveAssessment]
    volumes: list[VolumeSample]
