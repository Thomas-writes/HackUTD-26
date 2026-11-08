# SSD telemetry and classification

Each Linux collector sends per-drive metrics directly to FastAPI. The API summarizes the last 30 seconds for each SSD, runs the trained random forest, and sends the resulting JSON to a configurable SpacetimeDB reducer. The collector defaults to one second between samples, and the fleet simulator defaults to three seconds. Both accept intervals down to 100 ms.

## Run it locally

From the `telemetry` directory, create a Python 3.12 environment and train the demo model.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python simulate.py --training-csv training.csv
python train.py --data-source "Synthetic demo"
python -m uvicorn app:app --host 127.0.0.1 --port 8000 --workers 1
```

Use one API worker so each drive's rolling window lives in one process. The API loads the generated `model.joblib` at startup and groups concurrent windows into model calls over 10 ms. `TELEMETRY_MODEL_PATH` can point to another artifact created by `train.py`.

In a second terminal with the environment active, run the 1,000-node demo.

```sh
python simulate.py
```

Inspect a node in a separate terminal while the simulator runs.

```sh
curl -fsS http://127.0.0.1:8000/handoff/latest/sim-node-0000 | python -m json.tool
```

The default fleet has 1,000 nodes with one SSD each and runs until interrupted. The simulator staggers each node's clock across the three-second interval. Per three seconds of eligible healthy operation, each node has about a 0.27% chance of starting heavy writes and a 0.03% chance of starting a drive fault. The simulator adjusts the per-sample odds for other intervals. An incident injects metrics for either a heavy-write workload or a drive fault for 30–60 seconds, followed by a 30-second healthy period. The model uses the same rolling window as it does for collector data, so the displayed status can lag a change in the injected metrics.

Use `--nodes 1` for a walkthrough or `--nodes 10000` for a load test. The default interval increases with fleet size, reaching 12 seconds at 10,000 nodes. Use `--seconds 120` for a timed run, `--failure-chance 0` for a healthy baseline, or `--burst` to start all node clocks together. Use `--drives` and `--drives-per-node` to change the fleet size and number of SSDs per node. The simulator reports accepted readings, delivery failures, retries, missed intervals, and the recent 95th-percentile API round-trip time. Open `http://127.0.0.1:8000/docs` for the request and response schemas.

On a Linux node, the collector discovers whole NVMe devices through `/proc/diskstats`. For a specific device, use its kernel name as listed there. Repeat `--device` for multiple drives. A test device mapped by `dm-delay` can be selected by its `dm-N` name.

```sh
python agent.py --node-id node-01 --device nvme0n1 --mount / --interval 1
```

For remote collectors, serve the intake through a reachable HTTPS endpoint and set `--url` to it. The collector reads disk counters. When available, `nvme-cli` supplies SMART warning bits and the media error counter on a separate timer. `--smart-interval 0` disables those checks. `--mount` reports filesystem capacity separately from physical drive health. Set a `TELEMETRY_TOKEN` on each collector and set `TELEMETRY_NODE_TOKENS` on the API to a JSON map of node IDs to their tokens for authenticated intake and previews.

## SpacetimeDB handoff

- **`POST /telemetry`** accepts one node batch and returns the assessment document.
- **`GET /handoff/latest/{node_id}`** shows the latest generated assessment in camelCase. Add **`?wire=true`** to see the exact HTTP body sent to SpacetimeDB. See [example-output.json](example-output.json).
- **`GET /healthz`** shows the model version, forwarding status, and held-out accuracy with its dataset and sample size.

When `SPACETIME_REDUCER_URL` is unset, FastAPI runs in preview mode. To enable forwarding, point it at the team's reducer URL and provide the service credential as `SPACETIME_TOKEN` when the reducer requires authentication.

```sh
export SPACETIME_REDUCER_URL='http://127.0.0.1:3000/v1/database/ssd-monitor/call/ingest_ssd_batch'
python -m uvicorn app:app --host 127.0.0.1 --port 8000 --workers 1
```

The reducer takes **one typed batch object**. SpacetimeDB's HTTP API takes an array of positional arguments, so the body is an array containing the batch. The wire keys are snake_case. Present optional values use `{"some": value}` and missing values use `null`. [SpacetimeDB reducer HTTP API](https://spacetimedb.com/docs/http/database/)

```sh
curl -fsS 'http://127.0.0.1:8000/handoff/latest/sim-node-0000?wire=true' | python -m json.tool
```

| Field | Meaning |
|---|---|
| `schemaVersion` | Contract version, currently `1` |
| `nodeId`, `sampleTimeMs`, `intervalMs` | Collector identity, Unix timestamp in milliseconds, and the duration of the sample interval |
| `modelVersion` | Version of the loaded offline artifact |
| `drives[]` | Latest raw readings and the classification for each drive in the node batch |
| `volumes[]` | Optional filesystem capacity readings from explicitly configured mount paths |

`classification` is `healthy`, `heavy_writes`, `drive_fault` or `warming_up`. `warming_up` means fewer than three readings or under 29 seconds of observed coverage in the 30-second window. It has a null `modelScore`. Other scores are the model's class scores. `nextCheck` is a suggested first action. Read and write latency are null for intervals with zero completed operations of that type. SMART fields can also be null. The cumulative `mediaErrorsTotal` is a decimal string to preserve precision in JavaScript.

The checked-in reducer keeps the latest assessment per `(nodeId, driveId)` in `ssd_status` and ignores repeated or older samples. An exact FastAPI retry reuses the same assessment. FastAPI returns `409` for stale input and `502` when configured reducer delivery fails. The preview endpoint shows the generated JSON even when delivery fails. [Run the connected dashboard](../my-spacetime-app/README.md)

## Train with another dataset

`train.py --input path/to/labeled.csv` accepts the column names emitted by `simulate.py --training-csv`. Each row has `run_id`, `label`, `sample_time_ms`, `interval_ms`, and the snake_case fields in `DriveSample`. Use distinct run IDs for independent experiments. The trainer holds out entire runs for its report. The bundled generator supplies synthetic data for the demo, so the resulting scores measure performance on simulated runs. The dashboard displays held-out accuracy with the dataset name and number of windows. Expand it for balanced accuracy and per-class precision and recall. Use fault recall to track missed faults and fault precision to track false alerts. The bundled evaluation uses synthetic runs.

For field evaluation, collect healthy and write-heavy runs under known workloads. Capture a drive-fault run only when you have independent evidence such as a verified device diagnostic or incident ticket. Keep all captures from the same physical drive under one run ID so its windows stay together in the evaluation split.

```sh
python agent.py --device nvme0n1 --training-csv field.csv --label healthy --run-id lab-drive-a
python train.py --input field.csv --data-source "Labeled Linux SSD captures"
```

Record at least five independent groups per class with roughly a minute of usable telemetry per scenario. [Alibaba SSD SMART logs and failure tickets](https://github.com/shujiehan/ssdanalysis#readme) provide real fault evidence. [Microsoft Research block I/O traces](https://www.microsoft.com/en-us/research/people/dnarayan/more-information/) provide write-workload examples. These sources have different sampling intervals and measurement fields. The live classifier requires aligned I/O and SMART readings in its 30-second windows.

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
