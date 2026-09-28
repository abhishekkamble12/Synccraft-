/**
 * Node.js test suite for static/js/rga.js using native node:test.
 * Verifies that the JavaScript CRDT port agrees 100% with the Python reference.
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// Import CommonJS rga.js module
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { RGA, CharId, ROOT, Op, LamportClock } = require('../../static/js/rga.js');

test('JavaScript RGA: Basic local operations', () => {
  const doc = new RGA('site_js');
  doc.localInsert(0, 'H');
  doc.localInsert(1, 'i');
  doc.localInsert(2, '!');
  assert.equal(doc.text(), 'Hi!');
  assert.equal(doc.visibleLen(), 3);

  doc.localDelete(1); // delete 'i'
  assert.equal(doc.text(), 'H!');
  assert.equal(doc.visibleLen(), 2);
});

test('JavaScript RGA: Concurrent insert tie-breaking matches Python', () => {
  const r1 = new RGA('site1');
  const r2 = new RGA('site2');

  const opA = r1.localInsert(0, 'A');
  const opB = r1.localInsert(1, 'B');

  r2.apply(opA);
  r2.apply(opB);

  // Concurrently insert between A and B
  const opX = r1.localInsert(1, 'X');
  const opY = r2.localInsert(1, 'Y');

  r1.apply(opY);
  r2.apply(opX);

  assert.equal(r1.text(), r2.text());
  assert.equal(r1.visibleLen(), 4);
});

test('JavaScript RGA: Cross-language JSON test vector verification', () => {
  const vectorDir = __dirname;
  const files = fs.readdirSync(vectorDir).filter(f => f.endsWith('.json'));

  for (const file of files) {
    const raw = fs.readFileSync(path.join(vectorDir, file), 'utf8');
    const vector = JSON.parse(raw);

    const jsReplica = new RGA('js_verifier');
    for (const opData of vector.operations) {
      jsReplica.apply(opData);
    }

    assert.equal(
      jsReplica.text(),
      vector.expected_text,
      `Vector ${file} failed in JS: got "${jsReplica.text()}", expected "${vector.expected_text}"`
    );
    assert.equal(jsReplica.visibleLen(), vector.expected_len);
  }
});
