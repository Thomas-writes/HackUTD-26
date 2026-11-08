"""Check the Linux conversion, rolling coverage and database handoff contract."""

import asyncio
import json
import os
import unittest
from collections import deque
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

import httpx
import joblib
import numpy as np
from fastapi.testclient import TestClient
from sklearn.ensemble import RandomForestClassifier

from agent import Counters, measure
from app import DriveClassifier, app, reducer_args, send_to_spacetime
from features import FEATURE_NAMES, TimedSample, append_sample
from schemas import MODEL_LABELS, AssessmentBatch, DriveSample, TelemetryBatch


def batch_at(timestamp: int) -> TelemetryBatch:
    """Build a repeatable one-second sample for a named test node and drive."""
    return TelemetryBatch(
        node_id="test-node", sample_time_ms=timestamp, interval_ms=1_000,
        drives=[DriveSample(
            drive_id="ssd-01", device="nvme0n1", read_iops=100, write_iops=500,
            read_mib_per_sec=5, write_mib_per_sec=75, read_latency_ms=12,
            write_latency_ms=1.8, queue_depth=8, smart_critical_warning=0,
            media_errors_total="9007199254740993",
        )],
    )


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        """Create a tiny fitted artifact for API tests, without relying on generated demo files."""
        directory = TemporaryDirectory()
        cls.addClassCleanup(directory.cleanup)
        cls.model_path = Path(directory.name) / "test.joblib"
        model = RandomForestClassifier(n_estimators=5, random_state=42)
        model.fit([[value] * len(FEATURE_NAMES) for value in (0, 10, 100)], MODEL_LABELS)
        joblib.dump({"model": model, "model_version": "test", "feature_names": FEATURE_NAMES}, cls.model_path)

    def test_collector_converts_counters(self) -> None:
        """Check units, idle latency and a negative counter delta from known snapshots."""
        before = Counters(20, 10_000, 70, 0, 0, 0, 1, 50)
        after = Counters(120, 12_048, 270, 0, 0, 0, 3, 2_550)
        result = measure("nvme0n1", "ssd-01", before, after, 1_000)
        self.assertEqual(result.read_iops, 100)
        self.assertEqual(result.read_mib_per_sec, 1)
        self.assertEqual(result.read_latency_ms, 2)
        self.assertEqual(result.queue_depth, 2.5)
        self.assertIsNone(result.write_latency_ms)
        self.assertIsNone(measure("nvme0n1", "ssd-01", after, before, 1_000))

    def test_gaps_do_not_count_as_observed_time(self) -> None:
        """Verify a spread-out set of readings still leaves the classifier warming up."""
        history = deque()
        for timestamp in (1_000, 11_000, 21_000, 31_000):
            window = append_sample(history, TimedSample(timestamp, 1_000, batch_at(timestamp).drives[0]))
        self.assertEqual(window.covered_ms, 3_000)
        self.assertFalse(window.ready)

    def test_live_response_and_retry(self) -> None:
        """Verify classification, repeat delivery and stale timestamp rejection through the API."""
        environment = {
            "TELEMETRY_MODEL_PATH": str(self.model_path), "SPACETIME_REDUCER_URL": "",
            "TELEMETRY_NODE_TOKENS": '{"test-node":"test-node-token"}',
        }
        headers = {"Authorization": "Bearer test-node-token"}
        with patch.dict(os.environ, environment), TestClient(app) as client:
            first = batch_at(1_000).model_dump(by_alias=True)
            self.assertEqual(client.post("/telemetry", json=first).status_code, 401)
            response = client.post("/telemetry", json=first, headers=headers)
            self.assertEqual(response.json()["drives"][0]["classification"], "warming_up")
            for second in range(2, 31):
                body = batch_at(second * 1_000).model_dump(by_alias=True)
                response = client.post("/telemetry", json=body, headers=headers)
                self.assertEqual(response.status_code, 200)
            document = response.json()
            self.assertIn(document["drives"][0]["classification"], MODEL_LABELS)
            self.assertEqual(document["drives"][0]["mediaErrorsTotal"], "9007199254740993")
            self.assertEqual(client.get("/handoff/latest/test-node", headers=headers).json(), document)
            self.assertEqual(client.post("/telemetry", json=body, headers=headers).json(), document)
            self.assertEqual(client.post("/telemetry", json=first, headers=headers).status_code, 409)
            self.assertEqual(json.loads(reducer_args(AssessmentBatch.model_validate(document))[0]), document)

    def test_failed_delivery_can_be_retried(self) -> None:
        """Verify a downstream outage returns 502 and a retry sends the same prepared document."""
        environment = {
            "TELEMETRY_MODEL_PATH": str(self.model_path), "TELEMETRY_NODE_TOKENS": "",
            "SPACETIME_REDUCER_URL": "https://example.test/v1/database/test/call/ingest_ssd_batch",
        }
        sender = AsyncMock(side_effect=[httpx.ConnectError("offline"), None])
        with patch.dict(os.environ, environment), patch("app.send_to_spacetime", sender), TestClient(app) as client:
            body = batch_at(1_000).model_dump(by_alias=True)
            self.assertEqual(client.post("/telemetry", json=body).status_code, 502)
            self.assertEqual(client.post("/telemetry", json=body).status_code, 200)
            self.assertEqual(sender.await_args_list[0].args[-1], sender.await_args_list[1].args[-1])


class ReducerTests(unittest.IsolatedAsyncioTestCase):
    async def test_inference_batch_preserves_request_order(self) -> None:
        """Verify concurrent requests receive the same scores as separate model calls."""
        rows = [[value] * len(FEATURE_NAMES) for value in (0, 10, 100)]
        model = RandomForestClassifier(n_estimators=5, random_state=42).fit(rows, MODEL_LABELS)
        classifier = DriveClassifier(model, "test")
        left, right = await asyncio.gather(classifier.predict(rows[:2]), classifier.predict(rows[2:]))
        np.testing.assert_allclose(left, model.predict_proba(rows[:2]))
        np.testing.assert_allclose(right, model.predict_proba(rows[2:]))

    async def test_http_uses_one_json_string_argument(self) -> None:
        """Verify the reducer receives a single serialized batch and the expected auth header."""
        batch = batch_at(1_000)
        output = AssessmentBatch(
            node_id=batch.node_id, sample_time_ms=batch.sample_time_ms, interval_ms=batch.interval_ms,
            model_version="test", drives=[], volumes=[],
        )

        def reducer(request: httpx.Request) -> httpx.Response:
            """Check the request wire shape and return a simulated successful delivery."""
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.headers["authorization"], "Bearer test-secret")
            args = json.loads(request.content)
            self.assertEqual(len(args), 1)
            self.assertEqual(json.loads(args[0]), output.model_dump(by_alias=True))
            return httpx.Response(200)

        async with httpx.AsyncClient(transport=httpx.MockTransport(reducer)) as client:
            await send_to_spacetime(client, "https://example.test/call/test", "test-secret", output)


if __name__ == "__main__":
    unittest.main()
