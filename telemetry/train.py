"""Train the SSD random forest offline on labeled telemetry."""

import argparse
import csv
from collections import defaultdict, deque
from pathlib import Path

import joblib
from sklearn.ensemble import RandomForestClassifier

from features import FEATURE_NAMES, TimedSample, append_sample
from schemas import MODEL_LABELS, DriveSample


def read_windows(path: Path) -> tuple[list[tuple[float, ...]], list[str], list[str]]:
    """Read labeled CSV samples and return model features, labels and their independent run IDs."""
    runs = defaultdict(list)
    with path.open(newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            label = row["label"]
            if label not in MODEL_LABELS:
                raise ValueError(f"Unknown training class {label!r}")
            sample = DriveSample.model_validate({
                field: row[field] for field in DriveSample.model_fields if row.get(field) not in (None, "")
            })
            interval_ms = int(row["interval_ms"])
            if not 100 <= interval_ms <= 60_000:
                raise ValueError("Training intervals must be 100–60,000 ms")
            timed = TimedSample(int(row["sample_time_ms"]), interval_ms, sample)
            runs[(row["run_id"], sample.drive_id)].append((timed, label))

    features, labels, groups = [], [], []
    for (run_id, _), samples in runs.items():
        history: deque[TimedSample] = deque(maxlen=320)
        previous_time = None
        previous_label = None
        for sample, label in sorted(samples, key=lambda item: item[0].time_ms):
            if previous_time == sample.time_ms:
                raise ValueError(f"Duplicate sample time in run {run_id}")
            if previous_label != label:
                history.clear()
            window = append_sample(history, sample)
            if window.ready:
                features.append(window.values)
                labels.append(label)
                groups.append(run_id)
            previous_time, previous_label = sample.time_ms, label
    return features, labels, groups


def train(input_path: Path, output_path: Path, version: str, data_source: str = "Labeled CSV") -> None:
    """Fit on labeled runs and write a versioned joblib artifact."""
    rows, labels, runs = read_windows(input_path)
    for label in MODEL_LABELS:
        label_runs = {run for observed, run in zip(labels, runs, strict=True) if observed == label}
        if len(label_runs) < 5:
            raise ValueError(f"Class {label!r} needs at least five independent runs with complete windows")

    model = RandomForestClassifier(
        n_estimators=64, max_depth=10, min_samples_leaf=3,
        class_weight="balanced_subsample", random_state=42,
    )
    model.fit(rows, labels)
    joblib.dump({
        "model": model, "model_version": version, "feature_names": FEATURE_NAMES, "data_source": data_source,
    }, output_path)
    print(f"Saved {version} to {output_path}")


def main() -> None:
    """Parse the input CSV and output model paths and run offline training."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("training.csv"))
    parser.add_argument("--output", type=Path, default=Path("model.joblib"))
    parser.add_argument("--version", default="ssd-rf-v2")
    parser.add_argument("--data-source", default="Labeled CSV", help="Describe the source of the training data in the saved artifact")
    args = parser.parse_args()
    train(args.input, args.output, args.version, args.data_source)


if __name__ == "__main__":
    main()
