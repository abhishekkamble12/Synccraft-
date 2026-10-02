// Tests for SyncClient sequence tracking and gap repair (static/js/sync.js).
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

function makeClient() {
  store.clear();
  const sent = [];
  const applied = [];
  const client = new SyncClient({
    url: 'ws://test',
    docId: 'doc',
    onOpReceived: (op, seq) => applied.push(seq),
  });
  client.gapRepairDelayMs = 20;
  client.isConnected = true;
  client.socket = { readyState: 1, send: (raw) => sent.push(JSON.parse(raw)) };
  return { client, sent, applied };
}

const op = (id) => ({ op_id: id });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

test('contiguous ops advance lastKnownSeq without re-sync', async () => {
  const { client, sent } = makeClient();
  client._handleMessage({ type: 'init', head_seq: 0 });
  client._handleMessage({ type: 'ops', ops: [{ seq: 1, op: op('a') }, { seq: 2, op: op('b') }] });
  client._handleMessage({ type: 'ack', op_id: 'mine', seq: 3 });
  await sleep(40);
  assert.equal(client.lastKnownSeq, 3);
  assert.equal(sent.filter((m) => m.type === 'sync').length, 0);
});

test('out-of-order delivery that fills itself does not re-sync', async () => {
  const { client, sent } = makeClient();
  client._handleMessage({ type: 'init', head_seq: 0 });
  client._handleMessage({ type: 'ack', op_id: 'mine', seq: 2 });
  assert.equal(client.lastKnownSeq, 0);
  client._handleMessage({ type: 'ops', ops: [{ seq: 1, op: op('a') }] });
  assert.equal(client.lastKnownSeq, 2);
  await sleep(40);
  assert.equal(sent.filter((m) => m.type === 'sync').length, 0);
});

test('a dropped broadcast triggers a sync from the last contiguous seq', async () => {
  const { client, sent, applied } = makeClient();
  client._handleMessage({ type: 'init', head_seq: 5 });
  client._handleMessage({ type: 'ops', ops: [{ seq: 6, op: op('a') }, { seq: 8, op: op('c') }] });
  assert.equal(client.lastKnownSeq, 6);

  await sleep(40);
  const syncs = sent.filter((m) => m.type === 'sync');
  assert.equal(syncs.length, 1);
  assert.equal(syncs[0].last_seq, 6);
  assert.equal(syncs[0].reason, 'gap');

  client._handleMessage({
    type: 'sync_ack',
    missed: [{ seq: 7, op: op('b') }, { seq: 8, op: op('c') }],
    acked: [],
    head_seq: 8,
  });
  assert.equal(client.lastKnownSeq, 8);
  assert.deepEqual(applied, [6, 8, 7, 8]); // replays are fine: RGA apply is idempotent
});
