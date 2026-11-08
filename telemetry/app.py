"""Receive live metrics, run the classifier and expose the SpacetimeDB JSON."""

import asyncio
import hmac
import json
import logging
import os
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import httpx
import joblib
import numpy as np
from fastapi import FastAPI, Header, HTTPException, Request
from sklearn.ensemble import RandomForestClassifier

from features import FEATURE_NAMES, TimedSample, append_sample
from schemas import MODEL_LABELS, AssessmentBatch, DriveAssessment, TelemetryBatch

LOGGER = logging.getLogger("uvicorn.error")
DEFAULT_MODEL = Path(__file__).with_name("model.joblib")
NEXT_CHECK = {
    "normal": "Continue monitoring.",
    "likely_workload_contention": "Check the write-heavy job. Cap it and see if latency recovers.",
    "unexpected_slowdown": "Inspect drive and host logs. Compare other drives on the node.",
    "warming_up": "Wait for enough recent samples to classify this drive.",
}


def load_model(path: Path) -> tuple[RandomForestClassifier, str]:
    """Read a trusted artifact and return its fitted classifier and version after schema checks."""
    if not path.is_file():
        raise RuntimeError(f"Model missing at {path}. Generate training.csv and run train.py first.")
    artifact = joblib.load(path)
    estimator = artifact["model"]
    if (
        tuple(artifact["feature_names"]) != FEATURE_NAMES
        or not isinstance(estimator, RandomForestClassifier)
        or set(estimator.classes_) != set(MODEL_LABELS)
    ):
        raise RuntimeError("Model features or classes differ from the live contract. Retrain the model.")
    return estimator, artifact["model_version"]


class DriveClassifier:
    """Own each node's rolling windows and latest generated batch in one process."""

    def __init__(self, model: RandomForestClassifier, version: str) -> None:
        """Keep the fitted model and initialize empty per-node windows and response caches."""
        self.model = model
        self.version = version
        self.windows: dict[str, dict[str, deque[TimedSample]]] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.last_input: dict[str, TelemetryBatch] = {}
        self.latest: dict[str, AssessmentBatch] = {}
        self.pending_predictions: list[tuple[list[tuple[float, ...]], asyncio.Future[np.ndarray]]] = []
        self.flush_task: asyncio.Task[None] | None = None

    async def predict(self, rows: list[tuple[float, ...]]) -> np.ndarray:
        """Return class scores for ready windows by batching concurrent nodes into one model call."""
        future = asyncio.get_running_loop().create_future()
        self.pending_predictions.append((rows, future))
        if self.flush_task is None:
            self.flush_task = asyncio.create_task(self.flush_predictions())
        return await future

    async def flush_predictions(self) -> None:
        """Score windows queued over ten milliseconds and return each request's slice to its caller."""
        await asyncio.sleep(0.01)
        pending, self.pending_predictions = self.pending_predictions, []
        self.flush_task = None
        try:
            probabilities = self.model.predict_proba([row for rows, _ in pending for row in rows])
        except Exception as exc:
            for _, future in pending:
                if not future.done():
                    future.set_exception(exc)
            return

        start = 0
        for rows, future in pending:
            end = start + len(rows)
            if not future.done():
                future.set_result(probabilities[start:end])
            start = end

    async def assess(self, batch: TelemetryBatch) -> AssessmentBatch:
        """Apply one node's batch in order and return its computed or cached assessment."""
        async with self.locks.setdefault(batch.node_id, asyncio.Lock()):
            return await self.assess_node(batch)

    async def assess_node(self, batch: TelemetryBatch) -> AssessmentBatch:
        """Return current predictions for a batch, reuse exact retries and reject older samples."""
        node_id = batch.node_id
        previous = self.last_input.get(node_id)
        if previous is not None and batch.sample_time_ms <= previous.sample_time_ms:
            if batch == previous:
                return self.latest[node_id]
            raise HTTPException(409, "The node has already reported this timestamp or a newer one")

        previous_windows = self.windows.get(node_id, {})
        histories = {}
        windows = []
        for drive in batch.drives:
            history = deque(previous_windows.get(drive.drive_id, ()), maxlen=320)
            histories[drive.drive_id] = history
            windows.append(append_sample(history, TimedSample(batch.sample_time_ms, batch.interval_ms, drive)))

        ready = [window.values for window in windows if window.ready]
        predictions = iter(await self.predict(ready) if ready else [])
        assessments = []
        for drive, window in zip(batch.drives, windows, strict=True):
            label = "warming_up"
            score = None
            if window.ready:
                probabilities = next(predictions)
                best = int(probabilities.argmax())
                label = str(self.model.classes_[best])
                score = float(probabilities[best])
            assessments.append(
                DriveAssessment(
                    **drive.model_dump(), classification=label, model_score=score,
                    window_samples=window.samples, window_covered_ms=window.covered_ms,
                    next_check=NEXT_CHECK[label],
                )
            )

        result = AssessmentBatch(
            node_id=node_id, sample_time_ms=batch.sample_time_ms,
            interval_ms=batch.interval_ms, model_version=self.version,
            drives=assessments, volumes=batch.volumes,
        )
        self.windows[node_id] = histories
        self.last_input[node_id] = batch
        self.latest[node_id] = result
        return result


def node_tokens(raw: str | None) -> dict[str, str]:
    """Parse an optional JSON mapping of node IDs to bearer tokens for intake and preview."""
    if not raw:
        return {}
    tokens = json.loads(raw)
    if not isinstance(tokens, dict) or not tokens or any(
        not isinstance(token, str) or not token for token in tokens.values()
    ):
        raise ValueError("TELEMETRY_NODE_TOKENS must map node IDs to nonempty strings")
    return tokens


def authorize_node(node_id: str, authorization: str | None, tokens: dict[str, str]) -> None:
    """Allow the requested node in demo mode or require its configured bearer token."""
    if tokens:
        scheme, _, received = (authorization or "").partition(" ")
        expected = tokens.get(node_id)
        if expected is None or scheme.lower() != "bearer" or not hmac.compare_digest(
            received.encode(), expected.encode()
        ):
            raise HTTPException(401, "A valid token for this node is required")


def reducer_args(batch: AssessmentBatch) -> list[str]:
    """Wrap the displayed JSON document as the single string argument to the reducer."""
    return [batch.model_dump_json(by_alias=True)]


async def send_to_spacetime(
    client: httpx.AsyncClient, url: str, token: str | None, batch: AssessmentBatch
) -> None:
    """Post one assessment batch to the configured reducer and raise if delivery fails."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    response = await client.post(url, headers=headers, json=reducer_args(batch))
    response.raise_for_status()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load the trained model and configuration once, then manage a reusable HTTP client."""
    model_path = Path(os.environ.get("TELEMETRY_MODEL_PATH", DEFAULT_MODEL))
    app.state.classifier = DriveClassifier(*load_model(model_path))
    app.state.node_tokens = node_tokens(os.environ.get("TELEMETRY_NODE_TOKENS"))
    app.state.reducer_url = os.environ.get("SPACETIME_REDUCER_URL")
    app.state.reducer_token = os.environ.get("SPACETIME_TOKEN")
    async with httpx.AsyncClient(timeout=5) as client:
        app.state.http = client
        yield


app = FastAPI(title="SSD telemetry and triage", version="1.0", lifespan=lifespan)
BearerHeader = Annotated[str | None, Header()]


@app.get("/healthz")
async def health(request: Request) -> dict:
    """Return the loaded model version and whether reducer forwarding is configured."""
    return {
        "status": "ok", "modelVersion": request.app.state.classifier.version,
        "spacetimeForwarding": bool(request.app.state.reducer_url),
    }


@app.post("/telemetry", response_model=AssessmentBatch)
async def ingest(
    batch: TelemetryBatch, request: Request, authorization: BearerHeader = None
) -> AssessmentBatch:
    """Validate one node batch and return the JSON document handed to SpacetimeDB."""
    state = request.app.state
    authorize_node(batch.node_id, authorization, state.node_tokens)
    result = await state.classifier.assess(batch)
    if state.reducer_url:
        try:
            await send_to_spacetime(state.http, state.reducer_url, state.reducer_token, result)
        except httpx.HTTPError as exc:
            status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else "network"
            LOGGER.warning("SpacetimeDB rejected or missed batch for %s (%s)", batch.node_id, status)
            raise HTTPException(502, "SpacetimeDB delivery failed. Retry the same batch.") from exc
    return result


@app.get("/handoff/latest/{node_id}", response_model=AssessmentBatch)
async def preview(node_id: str, request: Request, authorization: BearerHeader = None) -> AssessmentBatch:
    """Return the latest generated document for this node so teammates can inspect the contract."""
    state = request.app.state
    authorize_node(node_id, authorization, state.node_tokens)
    result = state.classifier.latest.get(node_id)
    if result is None:
        raise HTTPException(404, "This node has not sent telemetry yet")
    return result
