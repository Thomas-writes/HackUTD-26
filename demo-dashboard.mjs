// Vite adds the demo UI to the served bundle while the checked-in app stays intact.
const styles = `
.demo-node-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(106px, 1fr)); gap: 8px; }
.demo-node { min-width: 0; border: 1px solid var(--border); border-radius: 10px; padding: 6px; text-align: center; }
.demo-node strong { display: block; font-size: 11px; }
.demo-node[data-rebalance] { border-color: var(--accent); }
.demo-rebalance-badge { display: block; margin-top: 3px; border-radius: 4px; padding: 2px 4px; background: color-mix(in srgb, var(--accent) 12%, var(--panel)); color: var(--text); font-size: 10px; font-weight: 700; }
.demo-drive-ring { position: relative; width: 86px; height: 86px; margin: 3px auto; border: 1px solid var(--border); border-radius: 50%; }
.demo-drive-ring > button { position: absolute; transform: translate(-50%, -50%); width: 18px; height: 18px; border: 1px solid var(--panel); border-radius: 50%; background: var(--state); cursor: pointer; }
.demo-drive-ring > button.healthy { background: color-mix(in srgb, var(--green) 55%, var(--base)); }
.demo-drive-ring > button[aria-pressed="true"] { outline: 2px solid var(--text); outline-offset: 1px; }
.demo-drive-ring > button.degraded { background: var(--red); border: 2px dashed white; }
.demo-actions { display: grid; gap: 8px; margin: 14px 0; }
.demo-actions button { border: 1px solid var(--border); border-radius: 6px; padding: 7px 10px; background: var(--panel); color: var(--text); font-weight: 650; }
.demo-actions button:not(:disabled) { border-color: var(--accent); cursor: pointer; }
.demo-actions button:disabled { opacity: 0.7; }
.demo-degraded { color: var(--red); font-weight: 700; }
`;

const actionCode = `
type DemoMode = 'manual' | 'automatic';

function DemoModeControl({ mode, onChange }: { mode: DemoMode; onChange: (settings: DemoFleet) => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  async function changeMode(next: DemoMode) {
    setBusy(true);
    setError(false);
    try {
      const response = await fetch('/demo/mode', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mode: next }),
      });
      if (!response.ok) throw new Error('Mode change rejected');
      onChange(await response.json());
    } catch {
      setError(true);
    } finally {
      setBusy(false);
    }
  }
  if (!import.meta.env.DEV) return <p className="muted">{mode === 'automatic' ? 'Automatic write rebalancing' : 'Monitoring'}</p>;
  return <>
    <label>
      Remediation mode
      <select value={mode} disabled={busy} onChange={event => { void changeMode(event.target.value as DemoMode); }}>
        <option value="manual">Monitor / manual actions</option>
        <option value="automatic">Automatic write rebalancing</option>
      </select>
    </label>
    {error && <p role="alert">Could not change remediation mode. Try again.</p>}
  </>;
}

type DemoActionKind = 'rebalance' | 'quarantine';
type DemoAction = { nodeId: string; driveId: string; kind: DemoActionKind; state: 'pending' | 'applied' | 'superseded' | 'completed' };
type DemoActions = Map<string, DemoAction>;
const demoActionKey = (node: string, drive: string, kind: DemoActionKind) => JSON.stringify([node, drive, kind]);
const demoOperations = [
  { kind: 'rebalance', label: 'Rebalance writes', status: 'heavy_writes' },
  { kind: 'quarantine', label: 'Quarantine drive', status: 'drive_fault' },
] as const;

function DemoDriveActions({ entry, actions, onAction, mode }: {
  entry: DriveEntry; actions: DemoActions; mode: DemoMode; onAction: (action: DemoAction) => void;
}) {
  const { drive, status } = entry;
  const [busy, setBusy] = useState<Partial<Record<DemoActionKind, boolean>>>({});
  const [errors, setErrors] = useState<Partial<Record<DemoActionKind, string>>>({});
  const actionFor = (kind: DemoActionKind) => actions.get(demoActionKey(drive.nodeId, drive.driveId, kind));
  const quarantined = actionFor('quarantine')?.state === 'applied';

  async function requestAction(kind: DemoActionKind) {
    setBusy(previous => ({ ...previous, [kind]: true }));
    setErrors(previous => ({ ...previous, [kind]: undefined }));
    try {
      const response = await fetch('/demo/actions', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ nodeId: drive.nodeId, driveId: drive.driveId, kind }),
      });
      if (!response.ok) {
        const result = await response.json().catch(() => null);
        throw new Error(result?.detail ?? 'Action could not be requested');
      }
      onAction(await response.json());
    } catch (error) {
      setErrors(previous => ({ ...previous, [kind]: error instanceof Error ? error.message : 'Action failed' }));
    } finally {
      setBusy(previous => ({ ...previous, [kind]: false }));
    }
  }

  if (!demoNode.test(drive.nodeId)) return null;
  if (!import.meta.env.DEV && !actionFor('rebalance') && !actionFor('quarantine')) return null;
  return <div className="demo-actions" aria-label="Drive remediation">
    {quarantined && <span className="demo-degraded">Degraded · new writes stopped</span>}
    {demoOperations.map(({ kind, label, status: required }) => {
      const state = actionFor(kind)?.state;
      const detail = state === 'applied' ? 'Applied' : state === 'pending' ? 'Applying' : state === 'superseded' ? 'Superseded' : state === 'completed' ? 'Recovered' : busy[kind] ? 'Requesting' : '';
      return <div key={kind}>
        {import.meta.env.DEV
          ? <button type="button" disabled={(mode === 'automatic' && kind === 'rebalance') || status !== required || (state !== undefined && state !== 'completed') || busy[kind] || quarantined}
              onClick={() => { void requestAction(kind); }}>{label}{detail && ' · ' + detail}</button>
          : detail && <p>{label} · {detail}</p>}
        {errors[kind] && <p role="alert">{errors[kind]}</p>}
      </div>;
    })}
  </div>;
}

function useDemoActions() {
  const [actions, setActions] = useState<DemoActions>(() => new Map());
  useEffect(() => {
    const controller = new AbortController();
    let timer: number | undefined;
    async function refresh() {
      try {
        const response = await fetch('/demo/action-status', { signal: controller.signal, cache: 'no-store' });
        if (response.ok) {
          const result: {actions: DemoAction[]} = await response.json();
          if (!controller.signal.aborted) setActions(new Map(result.actions.map(action => [
            demoActionKey(action.nodeId, action.driveId, action.kind), action,
          ])));
        }
      } catch { /* Retain the last known actions during a reconnect. */ }
      if (!controller.signal.aborted) timer = window.setTimeout(refresh, 2000);
    }
    void refresh();
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, []);
  return [actions, (action: DemoAction) => setActions(previous => new Map(previous).set(
    demoActionKey(action.nodeId, action.driveId, action.kind), action,
  ))] as const;
}
`;

const circleMap = `
        <div className="demo-node-grid" aria-label="SSD node map">
          <style>{DEMO_MAP_STYLES}</style>
          {visibleNodes.map(([nodeId]) => {
            const drives = byNode.get(nodeId) ?? [];
            const states = drives.map(({ drive }) => actions.get(demoActionKey(nodeId, drive.driveId, 'rebalance'))?.state);
            const rebalance = states.includes('applied') ? 'applied' : states.includes('pending') ? 'pending' : undefined;
            return <div key={nodeId} className="demo-node" data-rebalance={rebalance}>
              <strong>{nodeId}</strong>
              {rebalance && <span className="demo-rebalance-badge">
                {rebalance === 'applied' ? 'Rebalancing writes' : 'Rebalance queued'}
              </span>}
              <div className="demo-drive-ring" aria-label={nodeId + ' SSDs'}>
                {drives.map(({ drive, status }, index) => {
                  const angle = (index / drives.length - 0.25) * Math.PI * 2;
                  const degraded = actions.get(demoActionKey(nodeId, drive.driveId, 'quarantine'))?.state === 'applied';
                  const description = drive.driveId + ' · ' + (degraded ? 'Degraded, quarantined' : statusInfo[status].label);
                  return <button key={drive.id} type="button" className={status + (degraded ? ' degraded' : '')}
                    aria-label={nodeId + ' ' + description} title={description} aria-pressed={selectedId === drive.id}
                    style={{ left: (50 + 39 * Math.cos(angle)) + '%', top: (50 + 39 * Math.sin(angle)) + '%' }}
                    onClick={() => onDrive(drive.id)} />;
                })}
              </div>
            </div>;
          })}
        </div>
`;

function replace(code, expected, replacement) {
  if (!code.includes(expected)) throw new Error('Unexpected stock dashboard structure: ' + expected.slice(0, 60));
  return code.replace(expected, () => replacement);
}

function replaceSection(code, start, end, replacement = '') {
  const first = code.indexOf(start);
  const last = code.indexOf(end, first + start.length);
  if (first < 0 || last < 0) throw new Error('Unexpected stock dashboard section: ' + start);
  return code.slice(0, first) + replacement + code.slice(last);
}

export default function demoDashboard(appSource, fleetEffect) {
  return {
    name: 'writeorwrong-demo-dashboard',
    enforce: 'pre',
    transform(source, id) {
      if (id.split('?')[0] !== appSource) return;
      const start = source.indexOf('    if (!import.meta.env.DEV) return;');
      const end = source.indexOf('  async function resizeFleet', start);
      if (start < 0 || end < 0) this.error('Unexpected stock fleet controls');
      let code = source.slice(0, start) + fleetEffect + source.slice(end);
      code = replaceSection(code, 'type ModelHealth = {', 'type DemoFleet = {');
      code = replaceSection(code, 'const healthUrl =', 'const sampleInterval =');
      code = replaceSection(code, 'function ModelEvaluation(', 'export default function App()');
      code = replace(code, '  const [modelHealth, setModelHealth] = useState<ModelHealth>();\n', '');
      code = replaceSection(code, '  const modelVersion = drives[0]?.modelVersion;', '  useEffect(() => {\n    let history');
      code = replace(code, '{evaluation && <ModelEvaluation evaluation={evaluation} />}', '');
      code = replace(code, 'type DemoFleet = { nodes: number; options: number[] };',
        'type DemoFleet = { nodes: number; options: number[]; mode: DemoMode };');
      code = replace(code, 'const targetNodes = demoFleet?.nodes;', 'const targetNodes = demoFleet?.nodes ?? 0;');
      code = replace(code, 'return match === null || Number(match[1]) < targetNodes;', `
        const driveId = /^ssd-([0-9]{4})$/.exec(drive.driveId);
        if (!match || !driveId) return false;
        const node = Number(match[1]);
        return node < targetNodes && Math.floor(Number(driveId[1]) / 10) === node;
      `);
      code = replace(code, '<div className="fleet-controls">',
        '<div className="fleet-controls">{demoFleet && <DemoModeControl mode={demoFleet.mode} onChange={setDemoFleet} />}');
      code = replace(code, '{demoFleet && (', '{import.meta.env.DEV && demoFleet && (');
      code = replace(code, '<select aria-label="Filter by status"', '{import.meta.env.DEV && <select aria-label="Filter by status"');
      code = replace(code, '                </select>\n              </div>', '                </select>}\n              </div>');
      code = replace(code, 'if (current.live && fleet.nodes.length > 0) history.push({ time: now, counts: fleet.counts });', `
      if (current.live && fleet.entries.length > 0) {
        const driveCounts = { drive_fault: 0, heavy_writes: 0, stale: 0, warming_up: 0, healthy: 0, unclassified: 0 };
        for (const { status } of fleet.entries) driveCounts[status]++;
        history.push({ time: now, counts: driveCounts });
      }`);
      code = replace(code, 'Nodes over time', 'Drives over time');
      code = replace(code, 'Node counts over time.', 'Drive counts over time.');
      code = replace(code, 'History appears when node readings arrive', 'History appears when drive readings arrive');
      code = replace(code, 'const mapPageSize = 1_000;', 'const mapPageSize = 30;');
      code = replace(code, 'function NodeMap({ nodes, counts }: Pick<ReturnType<typeof summarizeFleet>, \'nodes\' | \'counts\'>) {', `
function NodeMap({ nodes, counts, entries, actions, selectedId, onDrive }: ReturnType<typeof summarizeFleet> & {
  actions: DemoActions; selectedId: string; onDrive: (id: string) => void;
}) {
`);
      code = replace(code, '  const visibleNodes = nodes.slice(start, start + mapPageSize);', `
  const visibleNodes = nodes.slice(start, start + mapPageSize);
  const byNode = useMemo(() => {
    const visibleIds = new Set(nodes.slice(start, start + mapPageSize).map(([id]) => id));
    const groups = new Map<string, DriveEntry[]>();
    for (const entry of entries) {
      if (!visibleIds.has(entry.drive.nodeId)) continue;
      const group = groups.get(entry.drive.nodeId);
      if (group) group.push(entry);
      else groups.set(entry.drive.nodeId, [entry]);
    }
    for (const group of groups.values()) group.sort((a, b) => a.drive.id.localeCompare(b.drive.id));
    return groups;
  }, [entries, nodes, start]);
`);
      code = replace(code, 'One square per node, colored by its highest-priority drive status.', 'Each ring is one node. Select an SSD to inspect its readings.');
      const mapStart = code.indexOf('        <div className="node-map"');
      const mapEnd = code.indexOf('      ) : (', mapStart);
      if (mapStart < 0 || mapEnd < 0) this.error('Unexpected stock node map');
      code = code.slice(0, mapStart) + circleMap.replace('DEMO_MAP_STYLES', JSON.stringify(styles)) + code.slice(mapEnd);
      code = replace(code, 'function DriveDetail({ entry }: { entry: DriveEntry | undefined }) {',
        actionCode + 'function DriveDetail({ entry, actions, onAction, mode }: { entry: DriveEntry | undefined; actions: DemoActions; mode: DemoMode; onAction: (action: DemoAction) => void }) {');
      code = replace(code, '      <dl className="metric-grid">', '      <DemoDriveActions key={drive.id} entry={entry} actions={actions} onAction={onAction} mode={mode} />\n      <dl className="metric-grid">');
      code = replace(code, "  const [selectedId, setSelectedId] = useState('');", "  const [selectedId, setSelectedId] = useState('');\n  const [demoActions, rememberAction] = useDemoActions();");
      code = replace(code, '<NodeMap nodes={fleet.nodes} counts={fleet.counts} />', `<NodeMap {...fleet} actions={demoActions}
            selectedId={selected?.drive.id ?? ''} onDrive={id => {
              setFilter('all'); setSearch(''); setPage(0); setSelectedId(id);
            }} />`);
      code = replace(code, '<DriveDetail entry={selected} />', `<DriveDetail entry={selected} actions={demoActions} mode={demoFleet?.mode ?? 'manual'} onAction={action => {
            rememberAction(action);
            if (selected) {
              setSelectedId(selected.drive.id);
              setFilter('all');
            }
          }} />`);
      return code;
    },
  };
}
