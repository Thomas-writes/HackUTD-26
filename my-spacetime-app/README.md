# DriveWatch

DriveWatch shows the latest SSD assessment for every node. The Catppuccin Latte dashboard shows a fleet map and plots node counts by severity over time. Green marks healthy drives, yellow marks heavy writes, and red marks suspected drive faults. Use the drive inventory to inspect each assessment and see what to check first.

## Run the demo locally

Use Python 3.12, a Node version supported by `package.json`, and the [SpacetimeDB CLI](https://spacetimedb.com/install) 2.14. Run each block in a separate terminal from the repository root.

**1. Start SpacetimeDB**

```sh
spacetime start --listen-addr 127.0.0.1:3000
```

**2. Publish the module and start React**

```sh
cd my-spacetime-app
npm ci
npm ci --prefix spacetimedb
npm run spacetime:publish:local
VITE_SPACETIMEDB_HOST=ws://127.0.0.1:3000 VITE_SPACETIMEDB_DB_NAME=ssd-monitor npm run dev
```

Open [localhost:5173](http://localhost:5173). The dashboard subscribes to `ssd_status` and will show rows as they arrive.

**3. Train the demo model and start FastAPI**

```sh
cd telemetry
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python simulate.py --training-csv training.csv
python train.py --data-source "Synthetic demo"
export SPACETIME_REDUCER_URL=http://127.0.0.1:3000/v1/database/ssd-monitor/call/ingest_ssd_batch
TELEMETRY_DEMO_NODES=1000 python -m uvicorn app:app --host 127.0.0.1 --port 8000 --workers 1 --no-access-log
```

**4. Send live readings**

```sh
cd telemetry
source .venv/bin/activate
python simulate.py --dashboard-control
```

Use **Simulated nodes** in the dashboard to choose a fleet size from 1 to 10,000. The simulator starts with one SSD per node and reports every 3–12 seconds depending on fleet size. Heavy writes start on about 0.27% of eligible healthy three-second intervals, and drive faults on about 0.03%. The simulator changes the readings, and the model classifies them after roughly 30 seconds of coverage. The graph starts a new history when the fleet size changes.

Expand **Model accuracy** to see the dataset, balanced accuracy, and precision and recall per class. The bundled score comes from held-out synthetic runs. For field validation, hold out physical drives and use independent diagnoses as ground truth.

Press Ctrl+C in the simulator terminal to stop it. Use `python simulate.py --nodes 1000 --seconds 120` for a fixed-size timed run.

## Data flow

The collector sends one batch per node to FastAPI. FastAPI sends each assessed batch as the typed argument to `ingest_ssd_batch`. The reducer keeps one row per node and drive in `ssd_status`, and only a newer sample replaces that row. React reads those rows through its SpacetimeDB subscription.

For a remote demo, configure `VITE_SPACETIMEDB_HOST`, `VITE_SPACETIMEDB_DB_NAME`, and `SPACETIME_REDUCER_URL` for the same database. The dashboard uses public read access to `ssd_status`. To restrict ingestion, set `SSD_WRITER_IDENTITY` to the writer's hex identity in the module's publishing environment and set the matching `SPACETIME_TOKEN` in FastAPI. The token stays in the API environment.

Run `npm run spacetime:generate` after changing the module schema and `npm run build` to check the frontend. [Collector and wire contract](../telemetry/README.md)
