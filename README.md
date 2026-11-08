# WriteOrWrong

WriteOrWrong helps operators understand what is happening across a fleet of SSDs. It collects live measurements from Linux drives or a simulator, then uses a trained random forest to distinguish healthy operation, heavy writes, and suspected drive faults. SpacetimeDB streams those assessments to a React dashboard, where operators can follow fleet trends, inspect individual drives, and see what to check first. The simulator can replay up to 10,000 nodes for demos and load testing.

```text
Synthetic simulator or Linux collector → FastAPI + random forest → SpacetimeDB → React dashboard
```

## Tech stack

| Layer | What it does |
| --- | --- |
| Python 3.12, aiohttp, and httpx | Generate simulated telemetry, collect Linux device measurements, and deliver HTTP batches. |
| FastAPI, Uvicorn, and Pydantic | Validate input, maintain per-drive windows, run inference, and expose assessments and model evaluation. |
| scikit-learn, NumPy, and joblib | Train and evaluate the random forest and save a versioned artifact for inference. |
| SpacetimeDB 2.14 and TypeScript | Accept assessed batches through a reducer and stream the latest state of each drive to subscribers. |
| React, TypeScript, and Vite | Render the fleet map, rolling chart, drive inventory, simulator controls, and evaluation details. |
| unittest, Vitest, and jsdom | Test telemetry calculations and API behavior, plus the browser-side JSON helpers. |

## Run locally

Install Python 3.12, npm, a Node.js version allowed by [`my-spacetime-app/package.json`](my-spacetime-app/package.json), and the [SpacetimeDB CLI 2.14](https://spacetimedb.com/install). The simulator works on macOS and Linux. The real collector requires Linux and access to `/proc/diskstats`. Start each numbered section in a separate terminal from the repository root.

### 1. Start the local database

```sh
spacetime start --listen-addr 127.0.0.1:3000
```

### 2. Publish locally and start the web dashboard

```sh
cd my-spacetime-app
npm ci
npm ci --prefix spacetimedb
spacetime publish ssd-monitor --module-path ./spacetimedb --server http://127.0.0.1:3000 --no-config
VITE_SPACETIMEDB_HOST=ws://127.0.0.1:3000 VITE_SPACETIMEDB_DB_NAME=ssd-monitor \
  npm run dev -- --host 127.0.0.1 --port 5173 --strictPort
```

The explicit server and `--no-config` select the local database. The repository's [`spacetime.json`](my-spacetime-app/spacetime.json) targets Maincloud. The remote publish script also points to a `server` directory that is absent in this checkout.

### 3. Train the model and start FastAPI

```sh
cd telemetry
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python train.py --input training.csv --data-source "Synthetic demo"

SPACETIME_REDUCER_URL=http://127.0.0.1:3000/v1/database/ssd-monitor/call/ingest_ssd_batch \
  TELEMETRY_DEMO_NODES=1000 \
  python -m uvicorn app:app --host 127.0.0.1 --port 8000 --workers 1 --no-access-log
```

The repository already contains `telemetry/training.csv`. To generate another synthetic dataset, run `python simulate.py --training-csv /tmp/writeorwrong-training.csv` and pass that path to `train.py --input`. The trainer writes `model.joblib` locally. Set `TELEMETRY_MODEL_PATH` before starting FastAPI to load a different trusted artifact. Keep one API worker because the recent samples and exact-retry cache live in process memory. Vite proxies the demo controls and evaluation to port 8000. Reuse the artifact on later starts unless the model inputs, labels, or dataset change.

### 4. Start the simulator

```sh
cd telemetry
. .venv/bin/activate
python simulate.py --dashboard-control
```

Once Vite starts, copy its local URL from the terminal into your browser. Choose 1, 10, 100, 500, 1,000, 2,500, 5,000, or 10,000 in **Simulated nodes**. That selection changes how many nodes send measurements. The chart starts a new history after a resize. Each simulated node has one SSD in this mode. Reports arrive about every 3–12 seconds depending on fleet size, and the classifier usually needs about 30 seconds of telemetry.

Use `python simulate.py --nodes 1` for fixed-size replay, `--seconds 120` for a timed run, or `--failure-chance 0` for an incident-free workload. Fixed-size mode also accepts `--drives` and `--drives-per-node`. The default simulator starts heavy writes with about 0.27% probability and a fault with about 0.03% probability per eligible healthy three seconds. Those are incident start probabilities. Incidents last roughly 30–60 seconds, so a snapshot can show a larger share of affected drives. Press Ctrl+C in each terminal to stop its process.

### Preview and diagnose

For a Python-only run, train as above and start Uvicorn without `SPACETIME_REDUCER_URL`. Run the simulator with `--nodes 1`. FastAPI serves interactive docs at `/docs`. Use these commands to inspect a simulated node.

```sh
curl -fsS http://127.0.0.1:8000/healthz | python3 -m json.tool
curl -fsS http://127.0.0.1:8000/handoff/latest/sim-node-0000 | python3 -m json.tool
```

- **Empty dashboard** — Check that the simulator is sending, the API reports `spacetimeForwarding: true`, and React and the reducer URL point to the same database.
- **HTTP 502 from intake** — FastAPI assessed the sample but could not forward it. Check the database listener, module, reducer URL, and writer permissions.
- **Model startup failure** — Check the label definition, artifact path, and model version. Retrain with the current features and labels after resolving a contract mismatch.
- **Long warm-up** — Check sample delivery and coverage. Dropped intervals can leave gaps even when the first and latest samples are far enough apart.
- **Port conflict** — Check which process is listening before stopping anything. The current web proxy expects FastAPI on port 8000.

For a secured deployment, the collector can send a `TELEMETRY_TOKEN` and the API can validate it with `TELEMETRY_NODE_TOKENS`, a JSON map of node IDs to bearer tokens. Use HTTPS for remote collection. If the database requires a designated writer, publish with `SSD_WRITER_IDENTITY` and set its matching `SPACETIME_TOKEN` in the API environment. The dashboard reads the public `ssd_status` table. Keep writer credentials on the server.

## Share the demo on a phone

Install [cloudflared](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/), install the Node dependencies, and prepare the Python environment and model as shown above. Then run this from the repository root.

```sh
python3.12 dev.py
```

The script starts SpacetimeDB, FastAPI, and the simulator with 1,000 nodes. Open the local dashboard URL printed by the launcher to change the fleet size using **Simulated nodes**. The single `trycloudflare.com` link opens a viewing dashboard with no fleet controls. Both dashboards receive the same telemetry. Press Ctrl+C to stop the run.

## How the model learns and classifies

### Training data versus live data

The bundled [training CSV](telemetry/training.csv) has **4,680 synthetic readings** from 72 independent runs. There are 24 runs of 65 one-second readings for each of the three classes. `simulate.py` generates the readings and supplies the ground-truth label for each run.

| Training label | How the simulator produces it |
| --- | --- |
| `healthy` | Baseline reads and writes at varying load, with baseline latency and queue depth. |
| `heavy_writes` | Higher write throughput and heavier queue and latency pressure. |
| `drive_fault` | Higher latency, queue pressure, and injected SMART warnings or increases in media errors. Some fault samples also have high write throughput. |

The online simulator and Linux collector send **measurements only** to `POST /telemetry`. The scenario label stays in the generator. The Linux collector derives rates and latency from changes in kernel disk counters and can read NVMe health data using `nvme smart-log`. The checked-in [`field.csv`](telemetry/field.csv) contains a header and no measurements. The bundled model evaluation uses synthetic data.

### What goes into a prediction

Training and inference call the same feature builder in [`features.py`](telemetry/features.py). For each drive it uses the most recent 30 seconds, counts observed intervals without filling gaps, and requires at least three readings and 29 seconds of observed coverage. Before that, the API returns `warming_up` with no model score.

| Features | How they are calculated |
| --- | --- |
| Read IOPS, write IOPS, read MiB/s, write MiB/s, queue depth | Five averages weighted by the duration each reading represents. |
| Read latency, write latency | Two averages weighted by completed operations of the corresponding type. |
| Write throughput change | Latest write MiB/s minus earliest write MiB/s in the window. |
| Write share | Average write throughput divided by total read and write throughput. |
| SMART media warning | Whether the window includes a low-spare, degraded-reliability, or read-only-media warning bit. |
| Media error increase | `log1p` of the nonnegative difference between the latest and earliest available cumulative error counters. |

That makes **11 numerical features**. Node and drive names, mount capacity, and the simulator's ground-truth labels are absent from the input vector. Missing SMART data currently contributes no observed warning or counter increase. A green result with missing SMART therefore has less evidence behind it than a green result with a recent clean SMART reading.

### How it chooses a class

[`train.py`](telemetry/train.py) fits a supervised **random forest of 64 decision trees**. Each tree learns numeric splits from labeled examples, up to depth 10, with at least three training samples per leaf. The fit reweights classes within each tree's training sample. The learned splits can combine signals. A write-heavy window with stable media counters can follow different paths through the trees than a similarly busy window with new media errors.

At runtime, each tree follows its learned splits for the new feature vector and contributes class probabilities. The forest averages these estimates. [`app.py`](telemetry/app.py) selects the largest class score and returns it with the label, model version, window coverage, and a suggested first check. The score ranks classes for that window. Converting it into an estimate of physical failure risk would require calibration against independently verified field outcomes.

The API batches ready windows for prediction over about 10 ms. Training starts a new window when the ground-truth label changes. Live inference continuously slides the window over real readings, so old and new behavior can coexist for a while during onset and recovery. The dashboard also marks a reading stale after three expected sample intervals, with a minimum of five seconds.

### How to measure accuracy

The trainer uses the first split of a five-way stratified group splitter, holding out whole `run_id` groups. Adjacent windows from the same run stay on the same side of the split. This avoids testing on windows that overlap the training windows from that run.

The bundled CSV reproduced **100% accuracy and balanced accuracy on 555 held-out windows from 15 synthetic runs**, with 185 windows per class. This measures performance on new runs from the same simulator. The 555 overlapping windows represent 15 independent runs. Expand **Model accuracy** on the dashboard for the dataset, class precision, and recall.

- **Overall accuracy** is the fraction of labeled windows classified correctly. A fleet with 99% healthy windows could yield 99% accuracy from a model that always predicts Healthy.
- **Balanced accuracy** gives each class equal weight by averaging its recall.
- **Fault recall** measures how many labeled faults were caught. **Fault precision** measures how often a fault prediction was correct.

For field evaluation, label controlled healthy and heavy-write sessions from the workload that actually ran. Use verified diagnostics, incident tickets, or a controlled test as ground truth for fault sessions. Group all captures from a physical drive under one `run_id` so that drive stays on one side of the split. Collect at least five independent groups with complete windows per class. Test across drives and workloads, including heavy writes with faults, missing SMART readings, and onset and recovery periods. Report a confusion matrix, false alerts per drive-day, and incident detection delay in addition to window-level scores.

```sh
# From telemetry/ on a Linux node with the environment active.
python agent.py --node-id node-01 --device nvme0n1 --interval 1 \
  --training-csv captures.csv --label healthy --run-id physical-drive-a
python train.py --input captures.csv --data-source "Labeled Linux SSD captures"
```

For ordinary monitoring, omit the CSV, label, and run ID options. Repeat `--device` for multiple SSDs, use `--mount /` to report filesystem capacity, and use `--smart-interval 0` to turn off optional SMART polling. The [Alibaba SSD dataset](https://github.com/shujiehan/ssdanalysis#readme) provides SMART logs and failure tickets. [Microsoft Research block I/O traces](https://www.microsoft.com/en-us/research/people/dnarayan/more-information/) provide workload examples. Those sources can inform experimental design, but an evaluation of this model needs labeled I/O and SMART readings aligned to its feature windows.

## Data and file guide

### Telemetry and development

| Path | Role |
| --- | --- |
| [`telemetry/agent.py`](telemetry/agent.py) | Linux collector. Reads `/proc/diskstats`, optionally samples NVMe SMART and volume capacity, sends batches, and can append labeled captures to CSV. |
| [`telemetry/simulate.py`](telemetry/simulate.py) | Generates labeled training data or replays changing synthetic measurements across a configurable number of nodes. |
| [`telemetry/schemas.py`](telemetry/schemas.py) | Pydantic types, validation limits, class names, and JSON field names shared by the Python pipeline. |
| [`telemetry/features.py`](telemetry/features.py) | Builds the same rolling-window features for offline training and live inference. |
| [`telemetry/train.py`](telemetry/train.py) | Loads labeled runs, holds out groups, trains and evaluates the model, and writes the joblib artifact. |
| [`telemetry/app.py`](telemetry/app.py) | Runs the FastAPI service, maintains per-drive history, assesses batches, controls the local demo fleet, and forwards classifications. |
| [`telemetry/test_pipeline.py`](telemetry/test_pipeline.py) | Tests counter conversion, coverage gaps, inference ordering, retries, and the database handoff. |
| [`telemetry/requirements.txt`](telemetry/requirements.txt) | Python runtime and model dependencies. |
| [`telemetry/training.csv`](telemetry/training.csv), [`field.csv`](telemetry/field.csv) | Bundled synthetic samples and a header-only capture template, respectively. |
| [`telemetry/example-output.json`](telemetry/example-output.json) | Example assessment shape from an older model. Use the live preview for current labels. |
| `telemetry/model.joblib`, `telemetry/.venv/` | Local trained model and Python environment. |

### Web and database

| Path | Role |
| --- | --- |
| [`my-spacetime-app/index.html`](my-spacetime-app/index.html), [`src/main.tsx`](my-spacetime-app/src/main.tsx) | Browser entry point and React bootstrap. The bootstrap connects to the configured SpacetimeDB host and database. |
| [`src/App.tsx`](my-spacetime-app/src/App.tsx), [`src/app.css`](my-spacetime-app/src/app.css) | Dashboard subscription, node map, history chart, fleet-size selector, drive details, evaluation display, and Catppuccin Latte styling. |
| [`src/module_bindings/`](my-spacetime-app/src/module_bindings/) | Generated client connection and types for database tables, reducers, and procedures. |
| [`spacetimedb/src/index.ts`](my-spacetime-app/spacetimedb/src/index.ts) | TypeScript database module. Defines `ssd_status`, validates incoming assessments, keeps the latest row per node and drive, and contains the example `person` table and reducers. |
| [`src/jsonFile.ts`](my-spacetime-app/src/jsonFile.ts), [`src/spacetime.ts`](my-spacetime-app/src/spacetime.ts), [`src/sync.ts`](my-spacetime-app/src/sync.ts) | Observable helpers for reading and downloading browser JSON, subscribing to `person` rows, and importing or exporting those rows. These helpers are separate from SSD intake. |
| [`src/jsonIO.md`](my-spacetime-app/src/jsonIO.md), `src/*.test.ts` | Detailed helper notes and the corresponding Vitest tests. |
| `package.json`, `package-lock.json`, `tsconfig.json` in the web and module directories | Node dependencies and lockfiles, command definitions, and TypeScript compiler settings. |
| [`vite.config.ts`](my-spacetime-app/vite.config.ts), [`spacetime.json`](my-spacetime-app/spacetime.json), `.env.local` | Web dev server, local API proxies, and test settings. Database project settings and local browser connection overrides. |
| `AGENTS.md`, `CLAUDE.md`, `.cursor/rules/`, `.github/copilot-instructions.md`, `.windsurfrules` | Repository guidance for development tools. `LICENSE` contains licensing terms, and `.gitignore` files exclude local artifacts. |

### Assessment contract

`POST /telemetry` accepts a node ID, Unix timestamp in milliseconds, sample interval, drive readings, and optional volume measurements. The response contains each drive's classification, model score, window coverage, and first suggested check. Read and write latency may be null when no corresponding operations completed. The cumulative media error counter is serialized as a decimal string to preserve integer precision.

`GET /handoff/latest/{node_id}` returns the latest assessment. Add `?wire=true` to see the reducer argument. FastAPI sends a one-element positional argument array with snake_case fields. Present optional values are wrapped in `{"some": value}` and absent values use `null`. The API reuses an assessment on an exact retry and returns 409 for a conflicting or older sample. SpacetimeDB keeps current rows, while the web chart samples the live node counts for up to two minutes in the open page.

## SSD auto-correction roadmap

Today the system **detects and suggests**. It records no remediation actions and never changes the device or the workload. A useful first implementation would automate recovery from verified workload contention and track hardware faults as operator incidents.

| Situation | Proposed action | How to recognize recovery |
| --- | --- | --- |
| Sustained heavy writes tied to a known background job | Show the proposed action in dry-run mode. With explicit opt-in, temporarily limit an allowlisted, noncritical job and record its prior setting. | Compare fresh windows before and after the action. Look for lower queue depth and latency, with write throughput consistent with the new limit. Verify improvement across consecutive windows and roll back if it fails. |
| Suspected drive fault | Corroborate the result with fresh SMART, new media errors, or independent device diagnostics. Raise an incident for backup or redundancy verification and operator-led replacement. | Record the repair or replacement outcome and device identity. Verify fresh healthy telemetry and no new errors over an observation period before clearing the incident. |
| Stale or missing telemetry | Restore collection or connectivity and identify which signals are missing. | Require resumed coverage and, when applicable, fresh SMART readings before taking any action. |

Track the evidence, action, timestamp, affected device, pre/post measurements, and outcome. Persistence thresholds, cooldowns, an allowlist, and rollback prevent repeated changes during noisy telemetry. For a demo, record a synthetic heavy-write event and visibly step through **detected → mitigation proposed → mitigated → verified**. Use observed recovery as the label for learning a recovery pattern later, separate from the model's initial diagnosis.

## Verification

Run the telemetry suite with `cd telemetry && .venv/bin/python -m unittest -v`. From `my-spacetime-app`, run `npm test -- --run` for web tests and `npm run build` for TypeScript and production bundling.
