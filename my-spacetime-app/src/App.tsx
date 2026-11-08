import { useDeferredValue, useEffect, useMemo, useState } from 'react';
import { useSpacetimeDB, useTable } from 'spacetimedb/react';
import { tables } from './module_bindings';
import type { SsdStatus } from './module_bindings/types';
import './app.css';

const statusInfo = {
  unexpected_slowdown: { label: 'Unexpected slowdown', priority: 0 },
  likely_workload_contention: { label: 'Likely contention', priority: 1 },
  stale: { label: 'Stale', priority: 2 },
  warming_up: { label: 'Warming up', priority: 3 },
  normal: { label: 'Normal', priority: 4 },
};
type DriveStatus = keyof typeof statusInfo;
type StatusFilter = DriveStatus | 'all';
type DriveEntry = { drive: SsdStatus; status: DriveStatus };
const statuses = Object.keys(statusInfo) as DriveStatus[];
const pageSize = 12;
const number = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });

function driveStatus(drive: SsdStatus, now: number): DriveStatus {
  const age = now - Number(drive.sampleTimeMs);
  return age > Math.max(5_000, drive.intervalMs * 3) ? 'stale' : drive.classification as DriveStatus;
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
        {status === 'stale' && <small>Last assessment · {statusInfo[drive.classification as DriveStatus].label}</small>}
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

export default function App() {
  const { isActive, connectionError } = useSpacetimeDB();
  const [rows, ready] = useTable(tables.ssdStatus);
  const drives = useDeferredValue(rows);
  const [now, setNow] = useState(Date.now);
  const [search, setSearch] = useState('');
  const [filter, setFilter] = useState<StatusFilter>('all');
  const [page, setPage] = useState(0);
  const [selectedId, setSelectedId] = useState('');

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, []);

  const fleet = useMemo(() => {
    const nodes = new Set<string>();
    const counts = { unexpected_slowdown: 0, likely_workload_contention: 0, stale: 0, warming_up: 0, normal: 0 };
    let latest = 0;
    const entries = drives.map(drive => {
      const status = driveStatus(drive, now);
      counts[status]++;
      nodes.add(drive.nodeId);
      latest = Math.max(latest, Number(drive.sampleTimeMs));
      return { drive, status };
    });
    entries.sort((a, b) => statusInfo[a.status].priority - statusInfo[b.status].priority || a.drive.id.localeCompare(b.drive.id));
    return { entries, counts, nodes: nodes.size, latest };
  }, [drives, now]);

  const query = search.trim().toLowerCase();
  const visible = fleet.entries.filter(({ drive, status }) =>
    (filter === 'all' || status === filter) &&
    (!query || [drive.nodeId, drive.driveId, drive.device].some(value => value.toLowerCase().includes(query)))
  );
  const pageCount = Math.max(1, Math.ceil(visible.length / pageSize));
  const activePage = Math.min(page, pageCount - 1);
  const pageStart = activePage * pageSize;
  const pageRows = visible.slice(pageStart, pageStart + pageSize);
  const selected = visible.find(({ drive }) => drive.id === selectedId) ?? pageRows[0];
  const live = isActive && ready;
  const connectionLabel = live ? 'Connected' : isActive ? 'Syncing' : connectionError ? 'Disconnected' : 'Connecting';
  const tiles = [
    { label: 'Drives monitored', value: drives.length, note: number.format(fleet.nodes) + ' nodes', color: 'accent' },
    { label: 'Unexpected slowdowns', value: fleet.counts.unexpected_slowdown, note: 'Check drive and host logs', color: 'unexpected_slowdown' },
    { label: 'Likely contention', value: fleet.counts.likely_workload_contention, note: 'Check active write jobs', color: 'likely_workload_contention' },
    { label: 'Telemetry gaps', value: fleet.counts.stale + fleet.counts.warming_up, note: fleet.counts.stale + ' stale · ' + fleet.counts.warming_up + ' warming up', color: 'stale' },
  ];

  return (
    <>
      <header className="topbar">
        <div className="topbar-inner">
          <div className="brand">
            <span className="brand-mark" aria-hidden="true">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6">
                <rect x="4" y="5" width="16" height="14" rx="3" />
                <path d="M4 14h16m-12 2.5h2m3 0h3" strokeLinecap="round" />
              </svg>
            </span>
            <span>DriveWatch <small>Storage operations</small></span>
          </div>
          <div className="connection" role="status">
            <span className={'connection-dot' + (live ? ' is-live' : '')} />
            {connectionLabel}
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
          {fleet.latest > 0 && <p className="last-sample">Latest sample · {sampleAge(fleet.latest, now)}</p>}
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

        {!live && drives.length > 0 && <p className="connection-note">Showing last received readings while SpacetimeDB reconnects.</p>}

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
