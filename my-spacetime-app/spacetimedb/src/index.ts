import { schema, SenderError, table, t } from 'spacetimedb/server';

const driveFields = {
  driveId: t.string(),
  device: t.string(),
  readIops: t.f64(),
  writeIops: t.f64(),
  readMibPerSec: t.f64(),
  writeMibPerSec: t.f64(),
  readLatencyMs: t.option(t.f64()),
  writeLatencyMs: t.option(t.f64()),
  queueDepth: t.f64(),
  inFlight: t.u64(),
  smartCriticalWarning: t.option(t.u8()),
  mediaErrorsTotal: t.option(t.string()),
  classification: t.string(),
  modelScore: t.option(t.f64()),
  windowSamples: t.u32(),
  windowCoveredMs: t.u32(),
  nextCheck: t.string(),
};

const batchFields = {
  nodeId: t.string(),
  sampleTimeMs: t.u64(),
  intervalMs: t.u32(),
  modelVersion: t.string(),
};

const SsdBatch = t.object('SsdBatch', {
  schemaVersion: t.u8(),
  ...batchFields,
  drives: t.array(t.object('SsdAssessment', driveFields)),
  volumes: t.array(t.object('VolumeCapacity', {
    mountPath: t.string(),
    availableBytes: t.u64(),
    totalBytes: t.u64(),
  })),
});

const ssdStatus = table({ name: 'ssd_status', public: true }, {
  id: t.string().primaryKey(),
  ...batchFields,
  ...driveFields,
});

const spacetimedb = schema({
  person: table({ public: true }, { name: t.string() }),
  ssdStatus,
}, { env: { SSD_WRITER_IDENTITY: t.option(t.string()) } });
export default spacetimedb;

const classifications = new Set([
  'normal', 'likely_workload_contention', 'unexpected_slowdown', 'warming_up',
]);

/** Apply one typed FastAPI batch and keep the newest assessment per node and drive. */
export const ingestSsdBatch = spacetimedb.reducer({ batch: SsdBatch }, (ctx, { batch }) => {
  const writer = ctx.env.SSD_WRITER_IDENTITY;
  if (writer !== undefined && ctx.sender.toHexString() !== writer.toLowerCase().replace(/^0x/, '')) {
    throw new SenderError('This identity cannot write SSD telemetry');
  }

  const { nodeId, sampleTimeMs, intervalMs, modelVersion, drives } = batch;
  if (batch.schemaVersion !== 1 || !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(nodeId) ||
      sampleTimeMs === 0n || sampleTimeMs > BigInt(Number.MAX_SAFE_INTEGER) ||
      intervalMs < 100 || intervalMs > 60_000 || drives.length < 1 || drives.length > 2048) {
    throw new SenderError('Invalid SSD batch');
  }

  const seen = new Set<string>();
  for (const drive of drives) {
    if (drive.driveId.length === 0 || drive.driveId.length > 200 || seen.has(drive.driveId) ||
        !classifications.has(drive.classification)) {
      throw new SenderError('Invalid SSD assessment');
    }
    seen.add(drive.driveId);

    const id = `${nodeId}/${drive.driveId}`;
    const row = { id, nodeId, sampleTimeMs, intervalMs, modelVersion, ...drive };
    const previous = ctx.db.ssdStatus.id.find(id);
    if (previous === null) {
      ctx.db.ssdStatus.insert(row);
    } else if (previous.sampleTimeMs < sampleTimeMs) {
      ctx.db.ssdStatus.id.update(row);
    }
  }
});

export const init = spacetimedb.init(_ctx => {
  // Called when the module is initially published
});

export const onConnect = spacetimedb.clientConnected(_ctx => {
  // Called every time a new client connects
});

export const onDisconnect = spacetimedb.clientDisconnected(_ctx => {
  // Called every time a client disconnects
});

export const add = spacetimedb.reducer(
  { name: t.string() },
  (ctx, { name }) => {
    ctx.db.person.insert({ name });
  }
);

export const sayHello = spacetimedb.reducer(ctx => {
  for (const person of ctx.db.person.iter()) {
    console.info(`Hello, ${person.name}!`);
  }
  console.info('Hello, World!');
});
