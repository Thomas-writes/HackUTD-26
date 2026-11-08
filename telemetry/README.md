# SSD telemetry and classification

Each Linux collector sends per-drive metrics directly to FastAPI. The API summarizes the last 30 seconds for each SSD, runs the trained random forest, and sends the resulting JSON to a configurable SpacetimeDB reducer. The default interval is **one second**. The collector and simulator accept intervals down to 100 ms.

## Run it locally

From the `telemetry` directory, create a Python 3.12 environment and train the demo model.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python simulate.py --training-csv training.csv
python train.py
python -m uvicorn app:app --host 127.0.0.1 --port 8000 --workers 1
```

Use one API worker so each drive's rolling window lives in one process. The API loads the generated `model.joblib` at startup and groups concurrent windows into model calls over 10 ms. `TELEMETRY_MODEL_PATH` can point to another artifact created by `train.py`.

In a second terminal with the environment active, run the 1,000-drive demo and inspect a node's live output.

```sh
python simulate.py --drives 1000 --drives-per-node 10 --interval 1 --seconds 40
curl -fsS http://127.0.0.1:8000/handoff/latest/sim-node-0000 | python -m json.tool
```

That replays 100 logical nodes with ten SSDs each, targeting 1,000 readings per second. The simulator spreads their sampling clocks across each second. Add `--burst` to start all node clocks together, or set `--drives-per-node 1` for 1,000 individual senders and a higher request rate. The simulator skips a batch if its send time is already one interval late and reports that as a missed interval. Its report also includes accepted readings, failed and retried batches, and the 95th-percentile API round-trip time. Open `http://127.0.0.1:8000/docs` for the request and response schemas.

On a Linux node, the collector discovers whole NVMe devices through `/proc/diskstats`. For a specific device, use its kernel name as listed there. Repeat `--device` for multiple drives. A test device mapped by `dm-delay` can be selected by its `dm-N` name.

```sh
python agent.py --node-id node-01 --device nvme0n1 --mount / --interval 1
```

For remote collectors, serve the intake through a reachable HTTPS endpoint and set `--url` to it. The collector reads disk counters. When available, `nvme-cli` supplies SMART warning bits and the media error counter on a separate timer. `--smart-interval 0` disables those checks. `--mount` reports filesystem capacity separately from physical drive health. Set a `TELEMETRY_TOKEN` on each collector and set `TELEMETRY_NODE_TOKENS` on the API to a JSON map of node IDs to their tokens for authenticated intake and previews.

## SpacetimeDB handoff

- **`POST /telemetry`** accepts one node batch and returns the assessment document.
- **`GET /handoff/latest/{node_id}`** shows the latest generated document for that node. It matches the JSON inside the reducer argument. See [example-output.json](example-output.json).
- **`GET /healthz`** shows the loaded model version and whether forwarding is configured.

When `SPACETIME_REDUCER_URL` is unset, FastAPI runs in preview mode. To enable forwarding, point it at the team's reducer URL and provide the service credential as `SPACETIME_TOKEN` when the reducer requires authentication.

```sh
export SPACETIME_REDUCER_URL='https://maincloud.spacetimedb.com/v1/database/YOUR_DATABASE/call/ingest_ssd_batch'
python -m uvicorn app:app --host 127.0.0.1 --port 8000 --workers 1
```

The reducer takes **one string argument**, for example `payload` with type `t.string()`. SpacetimeDB's HTTP API takes an array of positional arguments. FastAPI sends one request per node batch with this wire shape in JavaScript terms. [SpacetimeDB reducer HTTP API](https://spacetimedb.com/docs/http/database/)

```js
const requestBody = JSON.stringify([JSON.stringify(document)]);
// Inside the reducer, after SpacetimeDB decodes the positional arguments.
const batch = JSON.parse(payload);
```

| Field | Meaning |
|---|---|
| `schemaVersion` | Contract version, currently `1` |
| `nodeId`, `sampleTimeMs`, `intervalMs` | Collector identity, Unix timestamp in milliseconds, and the duration of the sample interval |
| `modelVersion` | Version of the loaded offline artifact |
| `drives[]` | Latest raw readings and the classification for each drive in the node batch |
| `volumes[]` | Optional filesystem capacity readings from explicitly configured mount paths |

`classification` is `normal`, `likely_workload_contention`, `unexpected_slowdown` or `warming_up`. `warming_up` means fewer than three readings or under 29 seconds of observed coverage in the 30-second window. It has a null `modelScore`. Other scores are the model's class scores. `nextCheck` is a suggested first action. Read and write latency are null for intervals with zero completed operations of that type. SMART fields can also be null. The cumulative `mediaErrorsTotal` is a decimal string to preserve precision in JavaScript.

Use `(nodeId, driveId)` for current status and `(nodeId, driveId, sampleTimeMs)` to deduplicate history. An exact retry reuses the same assessment. The reducer should only replace the latest status when the incoming timestamp is newer, since retries and delayed delivery can repeat an observation. FastAPI returns `409` for stale input and `502` when configured reducer delivery fails. The preview endpoint shows the generated JSON even when delivery fails.

## Train with another dataset

`train.py --input path/to/labeled.csv` accepts the column names emitted by `simulate.py --training-csv`. Each row has `run_id`, `label`, `sample_time_ms`, `interval_ms`, and the snake_case fields in `DriveSample`. Use distinct run IDs for independent experiments. The trainer holds out entire runs for its report. The bundled generator supplies synthetic data for the demo, so the resulting scores measure performance on simulated runs. Use controlled storage experiments and labeled field data to evaluate hardware performance.

| File | Responsibility |
|---|---|
| `agent.py` | Linux counters, interval calculations and live delivery |
| `simulate.py` | Labeled CSV generation and concurrent HTTP replay |
| `features.py` | Shared training and inference calculations |
| `train.py` | Offline training, held-out evaluation and artifact generation |
| `schemas.py` | Request and response fields, types and JSON naming |
| `app.py` | Live inference, JSON preview and HTTP reducer delivery |
| `test_pipeline.py` | Counter, timing, API and wire-contract checks |

Run the checks with `python -m unittest -v` from this directory. [Linux disk statistics reference](https://docs.kernel.org/admin-guide/iostats.html)
