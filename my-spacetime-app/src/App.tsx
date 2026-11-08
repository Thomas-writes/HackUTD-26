import { memo, useEffect, useMemo, useRef, useState, type MutableRefObject } from 'react';
import { useSpacetimeDB, useTable } from 'spacetimedb/react';
import { tables } from './module_bindings';
import type { SsdStatus } from './module_bindings/types';
import './app.css';

const statusInfo = {
  drive_fault: { label: 'Drive faults', priority: 0 },
  heavy_writes: { label: 'Heavy writes', priority: 1 },
  stale: { label: 'Stale', priority: 2 },
  warming_up: { label: 'Warming up', priority: 3 },
  unclassified: { label: 'Unclassified', priority: 4 },
  healthy: { label: 'Healthy', priority: 5 },
};
type DriveStatus = keyof typeof statusInfo;
type StatusFilter = DriveStatus | 'all';
type DriveEntry = { drive: SsdStatus; status: DriveStatus };
type NodeCounts = Record<DriveStatus, number>;
type HistoryPoint = { time: number; counts: NodeCounts };
type DriveSnapshot = { drives: readonly SsdStatus[]; live: boolean; connectionLabel: string };
const statuses = Object.keys(statusInfo) as DriveStatus[];
const chartStatuses = [...statuses].reverse();
const alertStatuses: DriveStatus[] = ['heavy_writes', 'drive_fault'];
type ModelHealth = {
  modelVersion: string;
  evaluation: {
    accuracy: number;
    balancedAccuracy?: number;
    windows: number;
    runs: number;
    dataSource: string;
    classes?: Partial<Record<DriveStatus, { precision: number; recall: number; support: number }>>;
  } | null;
};
type DemoFleet = { nodes: number; options: number[] };
const demoNode = /^sim-node-(\d{4})$/;
const healthUrl = import.meta.env.VITE_TELEMETRY_HEALTH_URL ?? (import.meta.env.DEV ? '/telemetry-health' : undefined);
const sampleInterval = 1_000;
const historyWindow = 120_000;
const pageSize = 12;
const mapPageSize = 1_000;
const number = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });
const initialSnapshot: DriveSnapshot = { drives: [], live: false, connectionLabel: 'Connecting' };

function driveStatus(drive: SsdStatus, now: number): DriveStatus {
  const age = now - Number(drive.sampleTimeMs);
  if (age > Math.max(5_000, drive.intervalMs * 3)) return 'stale';
  return classificationStatus(drive.classification);
}

function classificationStatus(classification: string): DriveStatus {
  return Object.prototype.hasOwnProperty.call(statusInfo, classification)
    ? classification as DriveStatus : 'unclassified';
}

function summarizeFleet(drives: readonly SsdStatus[], now: number) {
  const nodeStatuses = new Map<string, DriveStatus>();
  let latest = 0;
  const entries = drives.map(drive => {
    const status = driveStatus(drive, now);
    const previous = nodeStatuses.get(drive.nodeId);
    if (previous === undefined || statusInfo[status].priority < statusInfo[previous].priority) {
      nodeStatuses.set(drive.nodeId, status);
    }
    latest = Math.max(latest, Number(drive.sampleTimeMs));
    return { drive, status };
  });
  const counts: NodeCounts = { drive_fault: 0, heavy_writes: 0, stale: 0, warming_up: 0, healthy: 0, unclassified: 0 };
  for (const status of nodeStatuses.values()) counts[status]++;
  const nodes = [...nodeStatuses].sort(([left], [right]) => left.localeCompare(right));
  entries.sort((a, b) => statusInfo[a.status].priority - statusInfo[b.status].priority || a.drive.id.localeCompare(b.drive.id));
  return { entries, counts, nodes, latest };
}

// Keep transaction notifications in a small component so they don't rerender the full fleet.
const DriveSubscription = memo(function DriveSubscription({ snapshot }: { snapshot: MutableRefObject<DriveSnapshot> }) {
  const { isActive, connectionError } = useSpacetimeDB();
  const [drives, ready] = useTable(tables.ssdStatus);
  const live = isActive && ready;
  const connectionLabel = live ? 'Connected' : isActive ? 'Syncing' : connectionError ? 'Disconnected' : 'Connecting';

  useEffect(() => {
    snapshot.current = { drives, live, connectionLabel };
  }, [snapshot, drives, live, connectionLabel]);
  return null;
});

function describeCounts(counts: NodeCounts, categories = statuses) {
  return categories.map(status => `${number.format(counts[status])} ${statusInfo[status].label.toLowerCase()}`).join(', ');
}

function NodeMap({ nodes, counts }: Pick<ReturnType<typeof summarizeFleet>, 'nodes' | 'counts'>) {
  const [page, setPage] = useState(0);
  const pages = Math.max(1, Math.ceil(nodes.length / mapPageSize));
  const activePage = Math.min(page, pages - 1);
  const start = activePage * mapPageSize;
  const visibleNodes = nodes.slice(start, start + mapPageSize);
  return (
    <section className="panel fleet-panel" aria-labelledby="node-map-title">
      <div className="fleet-panel-heading">
        <div>
          <h2 id="node-map-title">Node map</h2>
          <p className="muted">One square per node, colored by its highest-priority drive status.</p>
        </div>
        <span className="fleet-tag">{number.format(nodes.length)} {nodes.length === 1 ? 'node' : 'nodes'}</span>
      </div>
      {nodes.length ? (
        <div className="node-map" role="img" aria-label={`Fleet totals: ${describeCounts(counts)}`}>
          {visibleNodes.map(([id, status]) => (
            <span key={id} className={'node-cell ' + status} title={id + ' · ' + statusInfo[status].label} />
          ))}
        </div>
      ) : (
        <div className="fleet-placeholder">Waiting for node readings</div>
      )}
      {pages > 1 && (
        <div className="pagination" aria-label="Node map pages">
          <span>{number.format(start + 1)}–{number.format(start + visibleNodes.length)} of {number.format(nodes.length)} {nodes.length === 1 ? 'node' : 'nodes'}</span>
          <div>
            <button type="button" onClick={() => setPage(activePage - 1)} disabled={activePage === 0}>Previous nodes</button>
            <button type="button" onClick={() => setPage(activePage + 1)} disabled={activePage + 1 === pages}>Next nodes</button>
          </div>
        </div>
      )}
      <ul className="node-legend" aria-label="Current node counts">
        {chartStatuses.map(status => (
          <li key={status} className={status}>
            <i aria-hidden="true" />{statusInfo[status].label}<strong>{number.format(counts[status])}</strong>
          </li>
        ))}
      </ul>
    </section>
  );
}

function NodeHistory({ history, now }: { history: HistoryPoint[]; now: number }) {
  const [alertsOnly, setAlertsOnly] = useState(false);
  const categories = alertsOnly ? alertStatuses : chartStatuses;
  const peak = Math.max(1, ...history.map(point => categories.reduce((sum, status) => sum + point.counts[status], 0)));
  const axisStep = alertsOnly ? 20 : peak > 100 ? 100 : 4;
  const ceiling = Math.ceil(peak / axisStep) * axisStep;
  const span = history.length ? now - history[0].time + sampleInterval : 0;
  const rangeSeconds = span > 60_000 ? 120 : span > 30_000 ? 60 : 30;
  const left = 44;
  const top = 14;
  const plotWidth = 468;
  const plotHeight = 160;
  const x = (time: number) => left + plotWidth * (1 + (time - now) / (rangeSeconds * 1_000));
  const y = (count: number) => top + plotHeight * (1 - count / ceiling);
  const latest = history[history.length - 1];

  return (
    <section className="panel fleet-panel" aria-labelledby="node-history-title">
      <div className="fleet-panel-heading">
        <div>
          <h2 id="node-history-title">Nodes over time</h2>
          <p className="muted">Up to two minutes of history from this session</p>
        </div>
        <div className="chart-toggle" role="group" aria-label="Chart series">
          <button type="button" aria-pressed={!alertsOnly} onClick={() => setAlertsOnly(false)}>All</button>
          <button type="button" aria-pressed={alertsOnly} onClick={() => setAlertsOnly(true)}>Alerts</button>
        </div>
      </div>
      {latest ? (
        <svg className="node-chart" viewBox="0 0 524 208" role="img"
          aria-label={`Node counts over time. Last plotted counts were ${describeCounts(latest.counts, categories)}.`}>
          {[0, 1, 2, 3, 4].map(step => {
            const count = ceiling * step / 4;
            return (
              <g key={step}>
                <line className="chart-gridline" x1={left} x2={left + plotWidth} y1={y(count)} y2={y(count)} />
                <text x={left - 8} y={y(count)} textAnchor="end" dominantBaseline="middle">{number.format(count)}</text>
              </g>
            );
          })}
          {history.map(point => {
            let stacked = 0;
            const barLeft = Math.max(left, x(point.time - sampleInterval));
            const barWidth = x(point.time) - barLeft;
            return (
              <g className="chart-point" key={point.time}>
                <title>{new Date(point.time).toLocaleTimeString()} · {describeCounts(point.counts, categories)}</title>
                {categories.map(status => {
                  const bottom = y(stacked);
                  stacked += point.counts[status];
                  return (
                    <rect key={status} className={status} x={barLeft} y={y(stacked)}
                      width={barWidth} height={bottom - y(stacked)} />
                  );
                })}
                <rect className="chart-hover" x={barLeft} y={top} width={barWidth} height={plotHeight} />
              </g>
            );
          })}
          <text x={left} y={198}>−{rangeSeconds} s</text>
          <text x={left + plotWidth / 2} y={198} textAnchor="middle">−{rangeSeconds / 2} s</text>
          <text x={left + plotWidth} y={198} textAnchor="end">Now</text>
        </svg>
      ) : (
        <div className="fleet-placeholder">History appears when node readings arrive</div>
      )}
    </section>
  );
}

function quantity(value: number | undefined, unit = '') {
  return value == null ? '—' : number.format(value) + unit;
}

function sampleAge(timestamp: number, now: number) {
  const seconds = Math.max(0, Math.floor((now - timestamp) / 1_000));
  if (seconds < 60) return seconds + 's ago';
  if (seconds < 3_600) return Math.floor(seconds / 60) + 'm ago';
  return Math.floor(seconds / 3_600) + 'h ago';
}

function StatusBadge({ status }: { status: DriveStatus }) {
  return <span className={'status-badge ' + status}><i />{statusInfo[status].label}</span>;
}

function DriveDetail({ entry }: { entry: DriveEntry | undefined }) {
  if (!entry) {
    return (
      <aside className="panel detail">
        <p className="eyebrow">Drive detail</p>
        <h2>No drive selected</h2>
        <p className="muted">Choose a drive to see its readings and the first check to make.</p>
      </aside>
    );
  }

  const { drive, status } = entry;
  const warning = drive.smartCriticalWarning;
  const metrics = [
    ['Read latency', quantity(drive.readLatencyMs, ' ms')],
    ['Write latency', quantity(drive.writeLatencyMs, ' ms')],
    ['Read throughput', quantity(drive.readMibPerSec, ' MiB/s')],
    ['Write throughput', quantity(drive.writeMibPerSec, ' MiB/s')],
    ['Read IOPS', quantity(drive.readIops)],
    ['Write IOPS', quantity(drive.writeIops)],
    ['Queue depth', quantity(drive.queueDepth)],
    ['In flight', drive.inFlight.toLocaleString()],
    ['SMART warning', warning == null ? '—' : '0x' + warning.toString(16).padStart(2, '0')],
    ['Media errors', drive.mediaErrorsTotal ?? '—'],
  ];

  return (
    <aside className="panel detail" aria-label="Selected drive details">
      <p className="eyebrow">Drive detail</p>
      <h2>{drive.driveId}</h2>
      <p className="drive-location">{drive.nodeId} / {drive.device}</p>
      <StatusBadge status={status} />
      <div className={'next-check ' + status}>
        <span className="eyebrow">Check first</span>
        <p>{status === 'stale' ? 'Check the node collector and its connection to the telemetry API.' : drive.nextCheck}</p>
        {status === 'stale' && <small>Last assessment · {statusInfo[classificationStatus(drive.classification)].label}</small>}
      </div>
      <dl className="metric-grid">
        {metrics.map(([label, value]) => (
          <div key={label}><dt>{label}</dt><dd>{value}</dd></div>
        ))}
      </dl>
      <dl className="assessment-meta">
        <div><dt>Sample time</dt><dd>{new Date(Number(drive.sampleTimeMs)).toLocaleTimeString()}</dd></div>
        <div><dt>Model</dt><dd>{drive.modelVersion}</dd></div>
        <div><dt>Model score</dt><dd>{quantity(drive.modelScore == null ? undefined : drive.modelScore * 100, '%')}</dd></div>
        <div><dt>Window coverage</dt><dd>{quantity(drive.windowCoveredMs / 1_000, 's')} · {drive.windowSamples} readings</dd></div>
      </dl>
    </aside>
  );
}

function ModelEvaluation({ evaluation }: { evaluation: NonNullable<ModelHealth['evaluation']> }) {
  const percent = (value: number) => number.format(value * 100) + '%';
  return (
    <details className="panel model-evaluation">
      <summary>
        <strong>Model accuracy {percent(evaluation.accuracy)}</strong>
        {' · '}{evaluation.dataSource}{' · '}
        {number.format(evaluation.windows)} windows from {evaluation.runs} held-out runs
      </summary>
      {evaluation.balancedAccuracy !== undefined && (
        <p>Balanced accuracy {percent(evaluation.balancedAccuracy)} gives each class equal weight.</p>
      )}
      {evaluation.classes && (
        <table aria-label="Held-out accuracy by classification">
          <thead><tr><th>Class</th><th>Precision</th><th>Recall</th><th>Test windows</th></tr></thead>
          <tbody>
            {statuses.map(status => {
              const scores = evaluation.classes?.[status];
              if (!scores) return null;
              return (
                <tr key={status}>
                  <td>{statusInfo[status].label}</td><td>{percent(scores.precision)}</td>
                  <td>{percent(scores.recall)}</td><td>{number.format(scores.support)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      <p>Fault recall measures how many labeled faults the model caught. Precision measures how often a fault alert was correct.</p>
    </details>
  );
}

export default function App() {
  const snapshot = useRef(initialSnapshot);
  const [{ drives, live, connectionLabel, fleet, history, now }, setDashboard] = useState(() => ({
    ...initialSnapshot,
    fleet: summarizeFleet([], Date.now()),
    history: [] as HistoryPoint[],
    now: Date.now(),
  }));
  const [search, setSearch] = useState('');
  const [filter, setFilter] = useState<StatusFilter>('all');
  const [page, setPage] = useState(0);
  const [selectedId, setSelectedId] = useState('');
  const [modelHealth, setModelHealth] = useState<ModelHealth>();
  const [demoFleet, setDemoFleet] = useState<DemoFleet>();
  const [resizing, setResizing] = useState(false);
  const [resizeError, setResizeError] = useState(false);
  const [correctiveAlgorithm, setCorrectiveAlgorithm] = useState(false);
  const targetNodes = demoFleet?.nodes;

  useEffect(() => {
    if (!import.meta.env.DEV) return;
    const controller = new AbortController();
    void fetch('/demo/fleet', { signal: controller.signal })
      .then(response => response.ok ? response.json() : null)
      .then(settings => { if (!controller.signal.aborted && settings) setDemoFleet(settings); })
      .catch(() => {});
    return () => controller.abort();
  }, []);

  async function resizeFleet(nodes: number) {
    setResizing(true);
    setResizeError(false);
    try {
      const response = await fetch('/demo/fleet', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ nodes }),
      });
      if (!response.ok) throw new Error('Fleet size was rejected');
      setDemoFleet(await response.json());
      setPage(0);
      setSelectedId('');
    } catch {
      setResizeError(true);
    } finally {
      setResizing(false);
    }
  }

  const modelVersion = drives[0]?.modelVersion;
  useEffect(() => {
    if (!healthUrl || !modelVersion) return;
    const controller = new AbortController();
    void fetch(healthUrl, { signal: controller.signal })
      .then(response => response.ok ? response.json() : null)
      .then(health => { if (!controller.signal.aborted && health) setModelHealth(health); })
      .catch(() => {});
    return () => controller.abort();
  }, [modelVersion, live]);
  const evaluation = modelHealth?.modelVersion === modelVersion ? modelHealth?.evaluation : undefined;

  useEffect(() => {
    let history: HistoryPoint[] = [];
    const timer = window.setInterval(() => {
      const current = snapshot.current;
      const now = Date.now();
      const drives = targetNodes === undefined ? current.drives : current.drives.filter(drive => {
        const match = demoNode.exec(drive.nodeId);
        return match === null || Number(match[1]) < targetNodes;
      });
      const fleet = summarizeFleet(drives, now);
      history = history.filter(point => point.time > now - historyWindow);
      if (current.live && fleet.nodes.length > 0) history.push({ time: now, counts: fleet.counts });
      setDashboard({ ...current, drives, fleet, history, now });
    }, sampleInterval);
    return () => window.clearInterval(timer);
  }, [targetNodes]);

  const query = search.trim().toLowerCase();
  const visible = useMemo(() => fleet.entries.filter(({ drive, status }) =>
    (filter === 'all' || status === filter) &&
    (!query || [drive.nodeId, drive.driveId, drive.device].some(value => value.toLowerCase().includes(query)))
  ), [fleet.entries, filter, query]);
  const pageCount = Math.max(1, Math.ceil(visible.length / pageSize));
  const activePage = Math.min(page, pageCount - 1);
  const pageStart = activePage * pageSize;
  const pageRows = visible.slice(pageStart, pageStart + pageSize);
  const selected = visible.find(({ drive }) => drive.id === selectedId) ?? pageRows[0];
  const tiles = [
    { label: 'Nodes monitored', value: fleet.nodes.length, note: number.format(drives.length) + ' drives reporting', color: 'accent' },
    { label: 'Drive faults', value: fleet.counts.drive_fault, note: 'Suspected drive faults', color: 'drive_fault' },
    { label: 'Heavy writes', value: fleet.counts.heavy_writes, note: 'Write-heavy workload', color: 'heavy_writes' },
    { label: 'Telemetry gaps', value: fleet.counts.stale + fleet.counts.warming_up + fleet.counts.unclassified, note: fleet.counts.stale + ' stale · ' + fleet.counts.warming_up + ' warming up' + (fleet.counts.unclassified ? ' · ' + fleet.counts.unclassified + ' unclassified' : ''), color: 'stale' },
  ];

  return (
    <>
      <DriveSubscription snapshot={snapshot} />
      <header className="topbar">
        <div className="topbar-inner">
          <div className="brand">
            <span className="brand-mark" aria-hidden="true">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6">
                <rect x="4" y="5" width="16" height="14" rx="3" />
                <path d="M4 14h16m-12 2.5h2m3 0h3" strokeLinecap="round" />
              </svg>
            </span>
            <span>WriteOrWrong <small>Storage operations</small></span>
          </div>

    <div className="topbar-actions">
      <button type="button" className="connection topbar-button"
        aria-pressed={correctiveAlgorithm} onClick={() => setCorrectiveAlgorithm(on => !on)}>
        Corrective Algorithm {correctiveAlgorithm ? 'on' : 'off'}
      </button>
      <div className="connection" role="status">
        <span className={'connection-dot' + (live ? ' is-live' : '')} />
        {connectionLabel}
      </div>
    </div>

        </div>
      </header>

      <main className="layout">
        <div className="heading">
          <div>
            <p className="eyebrow">Fleet telemetry</p>
            <h1>SSD fleet</h1>
            <p className="muted">See which drives need attention and what to check first.</p>
          </div>
          <div className="fleet-controls">
            {demoFleet && (
              <label>
                Simulated nodes
                <select value={demoFleet.nodes} disabled={resizing}
                  onChange={event => { void resizeFleet(Number(event.target.value)); }}>
                  {demoFleet.options.map(nodes => <option key={nodes} value={nodes}>{number.format(nodes)}</option>)}
                </select>
              </label>
            )}
            {resizeError && <p role="alert">Could not change the fleet size. Try again.</p>}
            {fleet.latest > 0 && <p className="last-sample">Latest sample · {sampleAge(fleet.latest, now)}</p>}
          </div>
        </div>

        <section className="overview" aria-label="Fleet summary">
          {tiles.map(tile => (
            <div key={tile.label} className={'summary-card ' + tile.color}>
              <p>{tile.label}</p>
              <strong>{number.format(tile.value)}</strong>
              <small>{tile.note}</small>
            </div>
          ))}
        </section>

        {evaluation && <ModelEvaluation evaluation={evaluation} />}
        {!live && drives.length > 0 && <p className="connection-note">Showing last received readings while SpacetimeDB reconnects.</p>}

        <div className="fleet-visuals">
          <NodeMap nodes={fleet.nodes} counts={fleet.counts} />
          <NodeHistory history={history} now={now} />
        </div>

        <div className="workspace">
          <section className="panel inventory" aria-labelledby="inventory-title">
            <div className="inventory-head">
              <div>
                <h2 id="inventory-title">Drive assessments</h2>
                <p className="muted">{number.format(visible.length)} of {number.format(drives.length)} drives</p>
              </div>
              <div className="toolbar">
                <label className="search">
                  <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true">
                    <circle cx="8.5" cy="8.5" r="4.8" /><path d="m12.2 12.2 4 4" strokeLinecap="round" />
                  </svg>
                  <input type="search" aria-label="Search drives" placeholder="Find a node or drive" value={search}
                    onChange={event => { setSearch(event.target.value); setPage(0); }} />
                </label>
                <select aria-label="Filter by status" value={filter}
                  onChange={event => { setFilter(event.target.value as StatusFilter); setPage(0); }}>
                  <option value="all">All statuses</option>
                  {statuses.map(status => <option key={status} value={status}>{statusInfo[status].label}</option>)}
                </select>
              </div>
            </div>
            {visible.length > 0 ? (
              <>
                <div className="table-scroll">
                  <table>
                    <thead>
                      <tr><th>Drive</th><th>Node</th><th>Status</th><th>Read latency</th><th>Write rate</th><th>Updated</th></tr>
                    </thead>
                    <tbody>
                      {pageRows.map(({ drive, status }) => (
                        <tr key={drive.id} className={selected?.drive.id === drive.id ? 'selected' : ''}>
                          <td>
                            <button type="button" className="drive-button" aria-pressed={selected?.drive.id === drive.id}
                              onClick={() => setSelectedId(drive.id)}>{drive.driveId}</button>
                            <span className="device">{drive.device}</span>
                          </td>
                          <td className="mono">{drive.nodeId}</td>
                          <td><StatusBadge status={status} /></td>
                          <td className="mono">{quantity(drive.readLatencyMs, ' ms')}</td>
                          <td className="mono">{quantity(drive.writeMibPerSec, ' MiB/s')}</td>
                          <td className="muted mono">{sampleAge(Number(drive.sampleTimeMs), now)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <div className="pagination">
                  <span>{number.format(pageStart + 1)}–{number.format(pageStart + pageRows.length)} of {number.format(visible.length)}</span>
                  <div>
                    <button type="button" onClick={() => setPage(activePage - 1)} disabled={activePage === 0}>Previous</button>
                    <button type="button" onClick={() => setPage(activePage + 1)} disabled={activePage + 1 === pageCount}>Next</button>
                  </div>
                </div>
              </>
            ) : (
              <div className="empty">
                <strong>{drives.length ? 'No matching drives' : live ? 'Waiting for telemetry' : 'Waiting for SpacetimeDB'}</strong>
                <p className="muted">
                  {drives.length ? 'Try a different node, drive, or status.'
                    : live ? 'Start a Linux collector or run the simulator to populate the fleet.'
                    : 'Check that the database is running and the SSD module has been published.'}
                </p>
              </div>
            )}
          </section>
          <DriveDetail entry={selected} />
        </div>
      </main>
    </>
  );
}
