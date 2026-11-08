# DriveWatch

DriveWatch shows the latest SSD assessment for every node. The fleet view puts unexpected slowdowns first, separates likely write contention from stale telemetry, and gives the engineer a first check and the underlying measurements for each drive.

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
python train.py
export SPACETIME_REDUCER_URL=http://127.0.0.1:3000/v1/database/ssd-monitor/call/ingest_ssd_batch
python -m uvicorn app:app --host 127.0.0.1 --port 8000 --workers 1
```

**4. Send live readings**

```sh
cd telemetry
source .venv/bin/activate
python simulate.py --drives 60 --drives-per-node 5 --interval 1 --seconds 120
```

The initial readings show **Warming up**. Classification starts after roughly 30 seconds of coverage. Use `--drives 1000 --drives-per-node 10` for the fleet load test. Readings become **Stale** after three expected intervals, with a five-second minimum.

## Data flow

The collector sends one batch per node to FastAPI. FastAPI sends each assessed batch as the typed argument to `ingest_ssd_batch`. The reducer keeps one row per node and drive in `ssd_status`, and only a newer sample replaces that row. React reads those rows through its SpacetimeDB subscription.

For a remote demo, configure `VITE_SPACETIMEDB_HOST`, `VITE_SPACETIMEDB_DB_NAME`, and `SPACETIME_REDUCER_URL` for the same database. The dashboard uses public read access to `ssd_status`. To restrict ingestion, set `SSD_WRITER_IDENTITY` to the writer's hex identity in the module's publishing environment and set the matching `SPACETIME_TOKEN` in FastAPI. The token stays in the API environment.

Run `npm run spacetime:generate` after changing the module schema and `npm run build` to check the frontend. [Collector and wire contract](../telemetry/README.md)
