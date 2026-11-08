
# DriveWatch / HackUTD-26 — Run Locally

DriveWatch is an SSD telemetry monitoring application built for HackUTD 2026.

The system runs:

1. **SSD Simulator** — generates telemetry for up to 10,000 simulated SSD nodes.
2. **FastAPI Backend** — receives telemetry and uses a trained random forest to classify SSD behavior.
3. **SpacetimeDB** — stores the latest SSD assessments and sends updates to connected clients.
4. **React Dashboard** — displays live SSD health, warnings, and performance metrics.

Data flow:

```text
SSD Simulator
      |
      v
FastAPI + Machine Learning
      |
      v
SpacetimeDB
      |
      v
React Dashboard
```

The bundled training and evaluation data are synthetic. High accuracy on synthetic data does not establish accuracy on real SSD failures.

---

## 1. Prerequisites

Supported systems:

- macOS
- Linux
- Windows through WSL2 Ubuntu

Native Windows is not supported by the automated launcher.

### Required software

Install the following:

- Git
- Python 3.12
- Node.js
- npm
- SpacetimeDB CLI 2.14

Supported Node.js versions:

- Node 22.23.4 or newer within major version 22
- Node 24.22.1 or newer within major version 24
- Node 26.12.1 or newer

A supported Node 24 release is recommended.

### Install SpacetimeDB

On macOS or Linux:

```bash
curl -sSf https://install.spacetimedb.com | sh
```

Official documentation:

https://spacetimedb.com/install

If the `spacetime` command cannot be found, add its installation directory to your PATH:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

For zsh, make that permanent:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
```

SpacetimeDB is installed separately from Python. Do not use `pip install spacetime` to install the CLI.

### Verify your tools

```bash
git --version
python3 --version
node --version
npm --version
spacetime --version
```

The launcher can also find `spacetime` at `~/.local/bin/spacetime` even when it is not on your PATH.

---

## 2. Download the repository

Open Terminal and run:

```bash
git clone https://github.com/Thomas-writes/HackUTD-26.git
cd HackUTD-26
```

All remaining launcher commands should be executed from the main `HackUTD-26` directory.

The repository must contain `scripts/dev.py` and the port-aware Vite configuration for the automated setup to work.

---

## 3. Check your computer

Run:

```bash
python3 scripts/dev.py doctor
```

This checks your development environment, including the required software and supported versions.

Fix any reported missing dependencies before continuing.

---

## 4. Install and configure everything

Run:

```bash
python3 scripts/dev.py setup
```

This automatically:

1. Creates `telemetry/.venv`.
2. Installs the Python dependencies.
3. Generates `telemetry/training.csv`.
4. Trains the random forest model.
5. Saves `telemetry/model.joblib`.
6. Installs the React dependencies.
7. Installs the SpacetimeDB module dependencies.

The training CSV and trained model are generated locally and are intentionally excluded from Git.

Re-running `setup` refreshes dependencies. The model is retrained when its artifact is missing.

---

## 5. Start the application

Run:

```bash
python3 scripts/dev.py up
```

The launcher starts:

- Local SpacetimeDB on port 3000
- FastAPI on port 8001
- React/Vite on port 5173
- A simulator with 1,000 SSD nodes

It also publishes the SpacetimeDB module to the `ssd-monitor` database.

The launcher manages the processes and writes logs into `.dev-run/`.

### Open the dashboard

React dashboard:

http://127.0.0.1:5173

FastAPI documentation:

http://127.0.0.1:8001/docs

Backend health:

http://127.0.0.1:8001/healthz

The dashboard displays live telemetry after the simulator begins sending samples.

The ML classifier needs approximately 30 seconds of readings before moving beyond `warming_up`.

---

## 6. Running different fleet sizes

### Default: 1,000 nodes

```bash
python3 scripts/dev.py up
```

### Small test: 100 nodes

```bash
python3 scripts/dev.py up --nodes 100
```

### Stress test: 10,000 nodes

```bash
python3 scripts/dev.py up --nodes 10000
```

For a full application run, the dashboard can also control the simulated fleet size.

Stop any existing launcher session before starting another configuration.

---

## 7. Stopping everything

### Stop all services started by the launcher

From the main repository directory:

```bash
python3 scripts/dev.py down
```

This stops the processes tracked by the launcher:

- SSD simulator
- FastAPI backend
- React/Vite frontend
- SpacetimeDB, if the launcher started it

This does not delete the trained model or database.

### Check which services are still running

```bash
python3 scripts/dev.py status
```

The command reports active services, relevant ports, and log locations.

**Important:** The launcher cannot stop servers that were started manually before it ran.

### Stop manually started services

If you launched a service manually, return to its Terminal window and press:

```text
Ctrl+C
```

Do this for any manually started instances of:

- `spacetime start`
- `uvicorn`
- `npm run dev`
- `python simulate.py`

### Find leftover processes

If you closed or lost the Terminal windows, inspect the common development ports:

```bash
lsof -nP -iTCP:3000 -iTCP:8000 -iTCP:8001 -iTCP:5173 -sTCP:LISTEN
```

Find related processes, including simulators that do not listen on a port:

```bash
pgrep -fl 'simulate.py|uvicorn|vite|spacetime' || true
```

Examine the results and identify which processes belong to your HackUTD application.

Stop each verified process with:

```bash
kill <PID>
```

Replace `<PID>` with its actual process ID.

For example, if the process ID is `54206`:

```bash
kill 54206
```

If a process refuses to exit, try:

```bash
kill -TERM <PID>
```

Use `kill -9 <PID>` only as a last resort.

Do not use broad commands such as `killall python` or `killall node`, because those may terminate unrelated applications.

After stopping the processes, run the inspection commands again to confirm they are gone.

---

## 8. Restarting the application

After a normal shutdown:

```bash
python3 scripts/dev.py up
```

You do not need to retrain the model or reinstall dependencies every time.

Use `setup` again if dependencies change or generated files are missing.

---

## 9. Backend-only mode

If SpacetimeDB or React is unavailable, use preview mode.

```bash
python3 scripts/dev.py up --preview --nodes 100
```

This starts only:

- FastAPI
- The trained ML model
- The SSD simulator

It does not start SpacetimeDB or React.

### View a simulated SSD assessment

```bash
curl -fsS http://127.0.0.1:8001/handoff/latest/sim-node-0000 | python3 -m json.tool
```

The output includes telemetry measurements, classifications, model scores, and suggested actions.

Stop preview mode before returning to the full application:

```bash
python3 scripts/dev.py down
```

---

## 10. Changing ports

If port 8001 is occupied:

```bash
python3 scripts/dev.py up --api-port 8002
```

If port 5173 is occupied:

```bash
python3 scripts/dev.py up --web-port 5174
```

The launcher checks for port conflicts rather than terminating unrelated processes.

When using a different port, update the URL you open accordingly.

---

## 11. Inspecting logs

Check application status:

```bash
python3 scripts/dev.py status
```

Service logs are stored in:

```text
.dev-run/
```

Common log files include:

```text
.dev-run/api.log
.dev-run/simulator.log
.dev-run/react.log
.dev-run/spacetime.log
```

For example:

```bash
tail -n 50 .dev-run/api.log
```

To follow the simulator's output:

```bash
tail -f .dev-run/simulator.log
```

Press `Ctrl+C` to stop following a log. This does not terminate the application.

---

## 12. Troubleshooting

### `spacetime: command not found`

SpacetimeDB is not installed correctly or its executable directory is missing from your PATH.

Check:

```bash
ls ~/.local/bin/spacetime
```

If the executable exists:

```bash
export PATH="$HOME/.local/bin:$PATH"
spacetime --version
```

The CLI is independent of the Python virtual environment.

### `Project path does not exist: server`

An older npm publish script points to the wrong directory.

The actual SpacetimeDB module lives at:

```text
my-spacetime-app/spacetimedb/
```

The automated launcher publishes that directory using the `ssd-monitor` database name.

Do not use the outdated publish script.

### `EJSONPARSE` in package.json

This means `package.json` contains invalid JSON.

Common causes include:

- Missing commas
- Extra commas
- Missing quotation marks
- Incorrectly edited scripts

Fix the JSON formatting or restore a known-good version before running setup again.

### `EBADENGINE`

Your Node.js version does not match the versions required in `package.json`.

Install a supported version listed in the prerequisites.

For example, Node 26.8.1 does not meet the project's Node 26 minimum of 26.12.1.

### Blocked npm install scripts

Some npm versions restrict package installation scripts.

Review:

```bash
npm install-scripts ls
```

If a build fails because a required package script was blocked, review and approve that package intentionally or use a compatible npm version.

### `Address already in use`

Another process is using the requested port.

Check:

```bash
lsof -nP -iTCP:8001 -sTCP:LISTEN
```

Stop the verified process or configure another API port.

### FastAPI returns `502 Bad Gateway`

This commonly means FastAPI could not deliver the assessment to the configured SpacetimeDB reducer.

Check that:

1. SpacetimeDB is running on port 3000.
2. The `ssd-monitor` database exists.
3. The module has been published.
4. The reducer URL points to the correct server and database.

Restart the launcher if necessary:

```bash
python3 scripts/dev.py down
python3 scripts/dev.py up
```

Review the service logs if the error continues.

### `/proc/diskstats` missing

The real SSD collector in `agent.py` requires Linux.

On macOS, run the synthetic simulator instead.

Real SSD data collection requires a Linux node with access to `/proc/diskstats`.

### `training.csv` missing

Generate it from the `telemetry` directory:

```bash
cd telemetry
source .venv/bin/activate
python simulate.py --training-csv training.csv
```

Or run the automated setup from the repository root:

```bash
python3 scripts/dev.py setup
```

### React dashboard is empty

Verify that:

1. SpacetimeDB is running.
2. The `ssd-monitor` module is published.
3. FastAPI is receiving telemetry.
4. The simulator is sending data.
5. The browser connects to the correct database.

Inspect `.dev-run/` for connection and delivery errors.

### Model accuracy shows 100%

The included model is trained and evaluated on synthetic SSD scenarios.

A perfect score on held-out synthetic runs does not guarantee performance on real SSD failures.

Real-world validation requires independently labeled SSD data.

### `scripts/dev.py` not found

Check that you are in the repository root:

```bash
pwd
ls scripts/dev.py
```

If the file does not exist, your clone does not contain the automated launcher yet.

---

## 13. Command reference

| Command | Purpose |
| --- | --- |
| `python3 scripts/dev.py doctor` | Check prerequisites |
| `python3 scripts/dev.py setup` | Install dependencies and train model |
| `python3 scripts/dev.py up` | Start the complete application |
| `python3 scripts/dev.py status` | Inspect service status |
| `python3 scripts/dev.py down` | Stop launcher-managed services |
| `python3 scripts/dev.py up --nodes 100` | Run 100 simulated nodes |
| `python3 scripts/dev.py up --nodes 10000` | Run 10,000 simulated nodes |
| `python3 scripts/dev.py up --preview` | Run backend-only mode |
| `python3 scripts/dev.py up --api-port 8002` | Change FastAPI port |
| `python3 scripts/dev.py up --web-port 5174` | Change React port |

---

## 14. Project structure

```text
HackUTD-26/
|
|-- README.md
|-- scripts/
|   |-- dev.py
|
|-- telemetry/
|   |-- agent.py
|   |-- app.py
|   |-- features.py
|   |-- schemas.py
|   |-- simulate.py
|   |-- train.py
|   |-- requirements.txt
|   |-- test_pipeline.py
|   |-- training.csv         # generated
|   |-- model.joblib         # generated
|   |-- .venv/               # generated
|
|-- my-spacetime-app/
|   |-- package.json
|   |-- vite.config.ts
|   |-- spacetime.json
|   |-- spacetimedb/
|   |   |-- src/
|   |-- src/
|
|-- .dev-run/                # generated logs and process state
```

---

## 15. Quick start summary

For a new machine:

```bash
git clone https://github.com/Thomas-writes/HackUTD-26.git
cd HackUTD-26

python3 scripts/dev.py doctor
python3 scripts/dev.py setup
python3 scripts/dev.py up
```

Open:

http://127.0.0.1:5173

To stop:

```bash
python3 scripts/dev.py down
```

To start again:

```bash
python3 scripts/dev.py up
```

**Note:** This guide assumes the automated launcher and updated Vite configuration have been added to the repository. The complete startup workflow has not yet been validated on every supported operating system.

