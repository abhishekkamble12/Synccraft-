// Tests for SyncClient sequence tracking, gap repair, GC watermarks and rebase
// triggering (static/js/sync.js).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const store = new Map();
globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
};
globalThis.WebSocket = { OPEN: 1 };

const require = createRequire(import.meta.url);
const { SyncClient } = require('../../static/js/sync.js');

function makeClient(options = {}) {
  store.clear();
  const sent = [];
  const applied = [];
  const client = new SyncClient({
    url: 'ws://test',
    docId: 'doc',
    siteId: 'site-a',
    onOpsReceived: (items) => applied.push(...items.map((i) => i.seq)),
    ...options,
  });
  client.gapRepairDelayMs = 20;
  client.isConnected = true;
  client.socket = { readyState: 1, send: (raw) => sent.push(JSON.parse(raw)) };
  return { client, sent, applied };
}

const op = (id) => ({ op_id: id });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const ofType = (sent, type) => sent.filter((m) => m.type === type);

test('init sends a sync handshake with the site id and pending ops', () => {
  const { client, sent } = makeClient();
  client.sendOp(op('offline'));
  client._handleMessage({ type: 'init', head_seq: 4, gc_seq: 0 });
  const [sync] = ofType(sent, 'sync');
  assert.equal(sync.site_id, 'site-a');
  assert.equal(sync.last_seq, 4);
  assert.deepEqual(sync.pending.map((e) => e.op.op_id), ['offline']);
});

test('contiguous ops advance lastKnownSeq without re-sync', async () => {
  const { client, sent } = makeClient();
  client._handleMessage({ type: 'init', head_seq: 0 });
  client._handleMessage({ type: 'ops', ops: [{ seq: 1, op: op('a') }, { seq: 2, op: op('b') }] });
  client._handleMessage({ type: 'ack', op_id: 'mine', seq: 3 });
  await sleep(40);
  assert.equal(client.lastKnownSeq, 3);
  assert.equal(ofType(sent, 'sync').filter((m) => m.reason === 'gap').length, 0);
});

test('out-of-order delivery that fills itself does not re-sync', async () => {
  const { client, sent } = makeClient();
  client._handleMessage({ type: 'init', head_seq: 0 });
  client._handleMessage({ type: 'ack', op_id: 'mine', seq: 2 });
  assert.equal(client.lastKnownSeq, 0);
  client._handleMessage({ type: 'ops', ops: [{ seq: 1, op: op('a') }] });
  assert.equal(client.lastKnownSeq, 2);
  await sleep(40);
  assert.equal(ofType(sent, 'sync').filter((m) => m.reason === 'gap').length, 0);
});

test('a dropped broadcast triggers a sync from the last contiguous seq', async () => {
  const { client, sent, applied } = makeClient();
  client._handleMessage({ type: 'init', head_seq: 5 });
  client._handleMessage({ type: 'ops', ops: [{ seq: 6, op: op('a') }, { seq: 8, op: op('c') }] });
  assert.equal(client.lastKnownSeq, 6);

  await sleep(40);
  const gaps = ofType(sent, 'sync').filter((m) => m.reason === 'gap');
  assert.equal(gaps.length, 1);
  assert.equal(gaps[0].last_seq, 6);

  client._handleMessage({
    type: 'sync_ack',
    missed: [{ seq: 7, op: op('b') }, { seq: 8, op: op('c') }],
    acked: [],
    rejected: [],
    head_seq: 8,
  });
  assert.equal(client.lastKnownSeq, 8);
  assert.deepEqual(applied, [6, 8, 7, 8]); // replays are fine: RGA apply is idempotent
});

test('ops carry the seq they were based on, and the stable watermark respects them', () => {
  const { client, sent } = makeClient();
  client._handleMessage({ type: 'init', head_seq: 10 });
  client.sendOp(op('x'));
  assert.equal(ofType(sent, 'op')[0].base_seq, 10);

  client._handleMessage({ type: 'ops', ops: [{ seq: 11, op: op('y') }, { seq: 12, op: op('z') }] });
  assert.equal(client.stableSeq(), 10, 'unacked op based on 10 holds the watermark back');
  client._handleMessage({ type: 'ack', op_id: 'x', seq: 13 });
  assert.equal(client.stableSeq(), 13);
  client._reportStable();
  assert.deepEqual(ofType(sent, 'stable').at(-1), { type: 'stable', seq: 13 });
});

test('a rejection asks for a resync and the next init rebases under a new site', () => {
  let rebaseFlag = null;
  const { client, sent } = makeClient({
    onInitReceived: (msg, pending, mustRebase) => {
      rebaseFlag = mustRebase;
      return { siteId: 'site-b', pending: pending.map((e) => ({ op: { op_id: `re-${e.op.op_id}` }, base_seq: msg.head_seq })) };
    },
  });
  client._handleMessage({ type: 'init', head_seq: 1 });
  client.sendOp(op('bad'));
  client._handleMessage({ type: 'error', code: 'op_rejected', op_id: 'bad', reason: 'stale' });
  assert.equal(ofType(sent, 'resync').length, 1);

  client.sendOp(op('typed-meanwhile'));
  assert.equal(ofType(sent, 'op').length, 1, 'ops typed while a rebase is pending wait for it');

  client._handleMessage({ type: 'init', head_seq: 7, gc_seq: 5 });
  assert.equal(rebaseFlag, true);
  const sync = ofType(sent, 'sync').at(-1);
  assert.equal(sync.site_id, 'site-b');
  assert.deepEqual(sync.pending.map((e) => e.op.op_id), ['re-bad', 're-typed-meanwhile']);
  assert.ok(sync.pending.every((e) => e.base_seq === 7));
});

test('pending ops older than the GC point force a rebase even without a rejection', () => {
  let rebaseFlag = null;
  const { client } = makeClient({
    onInitReceived: (msg, pending, mustRebase) => {
      rebaseFlag = mustRebase;
    },
  });
  store.set('collab_sync_pending_doc', JSON.stringify([{ op: op('old'), base_seq: 3 }]));
  client._handleMessage({ type: 'init', head_seq: 50, gc_seq: 40 });
  assert.equal(rebaseFlag, true);
});

test('a heartbeat head beyond our view schedules a gap repair', async () => {
  const { client, sent } = makeClient();
  client._handleMessage({ type: 'init', head_seq: 3 });
  client._handleMessage({ type: 'head', seq: 9 });
  await sleep(40);
  const gaps = ofType(sent, 'sync').filter((m) => m.reason === 'gap');
  assert.equal(gaps.length, 1);
  assert.equal(gaps[0].last_seq, 3);
});
