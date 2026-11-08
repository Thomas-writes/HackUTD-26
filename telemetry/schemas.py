"""Versioned JSON contract for the collectors and SpacetimeDB handoff."""
#Commented by Duane

#Annotated attaches extra metadata (validation rules) to a base type
#Literal restricts a value to an exact set of allowed constants
from typing import Annotated, Literal

#BaseModel: base class for validating data models
#ConfigData: typed dictionary for model-wide settings
#Field: attaches constraints (min/max, regex, defaults) to a field
#model_validator: decorator for validation that looks at the whole model
from pydantic import BaseModel, ConfigDict, Field, model_validator
#Converts snake_case names (read_iops) to camelCase (readIops)
from pydantic.alias_generators import to_camel

#Largest integer JavaScript ccan represent
#Keeping ints below this prevents precision loss when the JSON is
#read by JS/TypeScript clients such as SpacetimeDB
MAX_SAFE_INTEGER = 2**53 - 1
#Drive identifier or device name: 1-200 character string
DriveId = Annotated[str, Field(min_length=1, max_length=200)]
#Node identifier: starts with a letter or digit, then up to 127 more
#characters, digits, underscores, dots, or hypens (128 characters max)
NodeId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")]
#Millisecond timestamp: positive and within the JS-safe int range
Timestamp = Annotated[int, Field(gt=0, le=MAX_SAFE_INTEGER)]
#Sampling interval in milliseconds: between 100ms and 60s
Interval = Annotated[int, Field(ge=100, le=60_000)]
#Metric value: float from 0 up to 1e12 (rejects negatives and absurd values)
NonNegative = Annotated[float, Field(ge=0, le=1e12)]
#The only labels a drive can be assigned
Classification = Literal[
    "normal", "likely_workload_contention", "unexpected_slowdown", "warming_up"
]
#Labels the ML model itself can output
#"warming_up" is excluded because it means "not enough data yet"
#which is decided outside the model
MODEL_LABELS = ("normal", "likely_workload_contention", "unexpected_slowdown")

#Base class for every model in this file
#Applies one shared set of settings so all payloads follow
#the same wire format and strict rules
class WireModel(BaseModel):
    """Use snake_case in Python and camelCase in the JSON contract."""

    #Settings applied to every subclass
    model_config = ConfigDict(
        #Generate camelCase JSON names from snake_case Python attribute names
        alias_generator=to_camel, 
        #Still allow construction with the Python (snake_case) names
        populate_by_name=True, 
        #Reject any unknown keys instead of silently ignoring them
        extra="forbid", 
        #Reject NaN and infinity
        allow_inf_nan=False
    )


class DriveSample(WireModel):
    """One drive's metrics measured over the enclosing batch interval."""

    #Unique ID of the drive within the node
    drive_id: DriveId
    #Device name/path of the drive (e.g. an OS device name)
    device: DriveId
    #Read operatons per second
    read_iops: NonNegative
    #Write operations per second
    write_iops: NonNegative
    #Read throughput in mebibytes per second
    read_mib_per_sec: NonNegative
    #Write throughput in mebibytes per second
    write_mib_per_sec: NonNegative
    #Average read latency in ms; optional, defaults to None when not measured
    read_latency_ms: NonNegative | None = None
    #Average write latency in ms; optional, defaults to None when not measured
    write_latency_ms: NonNegative | None = None
    #Average number of requests waiting in the drive's queue
    queue_depth: NonNegative
    #Requests currently in fligght (int, JS-safe); default to 0
    in_flight: Annotated[int, Field(ge=0, le=MAX_SAFE_INTEGER)] = 0
    #SMART critical-warning bit flags (singly byte, 0-255); optional
    smart_critical_warning: Annotated[int, Field(ge=0, le=255)] | None = None
    #Total media errors as a digit string (up to 39 digits), because the
    #count can exceed what JS numbers represent exactly; optional
    media_errors_total: Annotated[str, Field(pattern=r"^[0-9]{1,39}$")] | None = None


class VolumeSample(WireModel):
    """Capacity for an explicitly monitored filesystem mount on a node."""

    #Filesystem path where the volume is mounted (1-1024 characters)
    mount_path: Annotated[str, Field(min_length=1, max_length=1024)]
    #Free space in bytes (0 allowed, JS-safe int)
    available_bytes: Annotated[int, Field(ge=0, le=MAX_SAFE_INTEGER)]
    #Total size in bytes; must be positive so free-space ratios never divide by 0
    total_bytes: Annotated[int, Field(gt=0, le=MAX_SAFE_INTEGER)]


class TelemetryBatch(WireModel):
    """Collector input for one node's drives at one sampling time."""

    #Contract version; only version 1 is accepted (default)
    schema_version: Literal[1] = 1
    #Which node was produced this batch
    node_id: NodeId
    #When the sample was taken in ms (epoch time)
    sample_time_ms: Timestamp
    #Length of measurement widow the metrics cover in ms
    interval_ms: Interval
    #Per-drive samples (1-2048)
    drives: list[DriveSample] = Field(min_length=1, max_length=2048)
    #Optional monitored volumes: up to 100 entries, empty by default
    volumes: list[VolumeSample] = Field(default_factory=list, max_length=100)

    #Run after all fields have been validated individually so the whole
    #model (self) is available for cross-field checks
    @model_validator(mode="after")

    #Builds a set of drive IDs (which drops duplicates) and compares its
    #size to the list length. If they differe, at least one drive ID
    #repeats and validation fails with a ValueError (reported as a validation error)
    def unique_drive_ids(self) -> "TelemetryBatch":
        """Return this batch after checking it has one sample per drive ID."""

        #Keeps only unique IDs; a smaller set than the list means duplicates
        if len({drive.drive_id for drive in self.drives}) != len(self.drives):
            #Reject the batch
            raise ValueError("A batch must contain unique drive IDs")
        #Validators in "after" mode must return the validated model
        return self


class DriveAssessment(DriveSample):
    """Latest measurements and the resulting label for a single drive."""

    #Label assigned to this drive (one of the allowed classifications)
    classification: Classification
    #Model confidence from 0-1, or None when no model score applies
    model_score: Annotated[float, Field(ge=0, le=1)] | None
    #How many samples were in the analyst window
    window_samples: int
    #How many ms of history that window covered
    window_covered_ms: int
    #Human-readable note and when this drive will be checked next
    next_check: str


class AssessmentBatch(WireModel):
    """The same document returned by FastAPI and sent to the reducer."""

    #Contract version; only version 1 is accepted (default)
    schema_version: Literal[1] = 1
    #Which node this assessment is for
    node_id: NodeId
    #Timestamp of the sample being assessed in ms
    sample_time_ms: Timestamp
    #Length of measurement window in ms
    interval_ms: Interval
    #Identifier of the model version that produced the labels
    model_version: str
    #One assessment per drive (measurements plus classification)
    drives: list[DriveAssessment]
    #Volume capacity samples passes through unchanged from input
    volumes: list[VolumeSample]
