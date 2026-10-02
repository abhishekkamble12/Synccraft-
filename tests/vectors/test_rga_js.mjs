/**
 * Node.js tests for static/js/rga.js (node --test).
 * The cross-language vectors make the JavaScript port agree exactly with the Python reference.
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);
const { RGA, CharId, Op, OSTree, LamportClock, rebasePending } = require('../../static/js/rga.js');

const nodeOrder = (rga) => Array.from(rga.iterNodes(), (n) => [n.charId.toString(), n.char, n.deleted]);

test('runs, multi-char deletes and positions', () => {
  const doc = new RGA('site_js');
  doc.localInsert(0, 'Hello world');
  doc.localInsert(5, ',');
  assert.equal(doc.text(), 'Hello, world');
  const del = doc.localDelete(0, 7);
  assert.deepEqual(del.toDict().spans, [['1@site_js', 5], ['12@site_js', 1], ['6@site_js', 1]]);
  assert.equal(doc.text(), 'world');
  assert.equal(doc.posOfCharId(doc.charIdAt(3)), 3);
});

test('emoji are one code point, as in Python', () => {
  const doc = new RGA('s');
  const op = doc.localInsert(0, 'a😀b');
  assert.equal(doc.visibleLen(), 3);
  assert.equal(op.lamport, 3);
  doc.localDelete(1);
  assert.equal(doc.text(), 'ab');
});

test('concurrent insert tie-breaking converges', () => {
  const r1 = new RGA('site1');
  const r2 = new RGA('site2');
  for (const op of [r1.localInsert(0, 'AB')]) r2.apply(op);
  const x = r1.localInsert(1, 'X');
  const y = r2.localInsert(1, 'Y');
  r1.apply(y);
  r2.apply(x);
  assert.equal(r1.text(), r2.text());
  assert.deepEqual(nodeOrder(r1), nodeOrder(r2));
});

test('order-statistic treap matches a plain array under random edits', () => {
  const tree = new OSTree();
  const ref = [];
  let seed = 7;
  const rand = () => ((seed = (seed * 1103515245 + 12345) % 2147483648) / 2147483648);
  for (let label = 0; label < 3000; label++) {
    const roll = rand();
    if (ref.length && roll < 0.25) {
      const victim = ref[Math.floor(rand() * ref.length)];
      tree.setDeleted(victim, !victim.deleted);
    } else if (ref.length && roll < 0.27) {
      tree.build(ref);
    } else {
      const item = { deleted: false, label };
      const idx = Math.floor(rand() * (ref.length + 1));
      tree.insertAfter(idx > 0 ? ref[idx - 1] : null, item);
      ref.splice(idx, 0, item);
    }
  }
  const visible = ref.filter((item) => !item.deleted);
  assert.equal(tree.visibleCount, visible.length);
  visible.forEach((item, k) => {
    assert.equal(tree.kthVisible(k), item);
    assert.equal(tree.visibleRank(item), k);
  });
});

test('snapshot round trip is run-length encoded and loads the legacy format', () => {
  const doc = new RGA('s');
  doc.localInsert(0, 'hello world');
  doc.localDelete(5, 1);
  const state = doc.toDict();
  assert.deepEqual(state.runs, [['1@s', 'hello', 0], ['6@s', ' ', 1], ['7@s', 'world', 0]]);
  assert.deepEqual(nodeOrder(RGA.fromDict(state, 't')), nodeOrder(doc));

  const legacy = RGA.fromDict({
    site_id: 'server', clock: 3, applied_op_ids: ['a'],
    nodes: [
      { char_id: '1@a', char: 'h', deleted: false, parent_id: '0@' },
      { char_id: '2@a', char: 'i', deleted: true, parent_id: '1@a' },
      { char_id: '3@a', char: '!', deleted: false, parent_id: '2@a' },
    ],
  });
  assert.equal(legacy.text(), 'h!');
  assert.deepEqual(legacy.versionVector, { a: 3 });
});

const vectorFiles = fs.readdirSync(__dirname).filter((f) => f.endsWith('.json')).sort();

for (const file of vectorFiles) {
  test(`cross-language vector: ${file}`, () => {
    const vector = JSON.parse(fs.readFileSync(path.join(__dirname, file), 'utf8'));

    if (vector.kind === 'rebase') {
      const old = new RGA(vector.old_site);
      for (const raw of [...vector.shared, ...vector.pending]) old.apply(raw);
      old.clock = new LamportClock(vector.old_clock);
      const fresh = RGA.fromDict(vector.server_snapshot, vector.new_site);
      rebasePending(old, fresh, vector.pending.map((raw) => Op.fromDict(raw)));
      assert.equal(fresh.text(), vector.expected_text);
      assert.deepEqual(nodeOrder(fresh), vector.expected_order);
      return;
    }

    const replica = new RGA('js_verifier');
    for (const raw of vector.operations) replica.apply(raw);
    assert.equal(replica.text(), vector.expected_text, `${file}: text differs from Python`);
    assert.equal(replica.visibleLen(), vector.expected_len);
    if (vector.expected_order) {
      assert.deepEqual(nodeOrder(replica), vector.expected_order);
      assert.deepEqual(replica.toDict().runs, vector.expected_runs);
      assert.deepEqual(replica.versionVector, vector.expected_vv);
    }
  });
}

test('CharId parsing keeps @ in site ids after the first one', () => {
  const id = CharId.fromString('12@site@x');
  assert.equal(id.clock, 12);
  assert.equal(id.siteId, 'site@x');
});
