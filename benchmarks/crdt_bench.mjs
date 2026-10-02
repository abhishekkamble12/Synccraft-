/**
 * Browser-side CRDT benchmark (Node): v1 rga.js vs current rga.js, and Yjs as an
 * industrial reference if it is installed (cd benchmarks && npm install).
 *
 *   node benchmarks/crdt_bench.mjs        -> docs/benchmarks/crdt_js.json
 *
 * Workloads match benchmarks/crdt_bench.py: per-keystroke edits at random
 * positions in a 100k-character document, and a 10k-character paste into it.
 */

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const here = path.dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);
const Current = require('../static/js/rga.js');
const Legacy = require('./legacy/rga.js');
let Y = null;
try {
  Y = require('yjs');
} catch {
  // optional
}

const N = 100_000;
const PASTE = 10_000;

function rng(seed) {
  let s = seed;
  return () => ((s = (s * 1103515245 + 12345) % 2147483648) / 2147483648);
}

function median(xs) {
  const s = [...xs].sort((a, b) => a - b);
  return s[Math.floor(s.length / 2)];
}

function timeEach(repeat, fn) {
  const samples = [];
  for (let i = 0; i < repeat; i++) {
    const t = performance.now();
    fn();
    samples.push((performance.now() - t) * 1000);
  }
  return Math.round(median(samples) * 100) / 100;
}

function buildCurrent(n) {
  return Current.RGA.fromDict({ site_id: 'seed', clock: n, runs: [['1@seed', 'x'.repeat(n), 0]] }, 'bench');
}

function buildLegacy(n) {
  const nodes = [];
  for (let i = 1; i <= n; i++) {
    nodes.push({ char_id: `${i}@seed`, char: 'x', deleted: false, parent_id: i > 1 ? `${i - 1}@seed` : '0@' });
  }
  return Legacy.RGA.fromDict({ site_id: 'seed', clock: n, nodes }, 'bench');
}

function buildYjs(n) {
  const doc = new Y.Doc();
  doc.getText('t').insert(0, 'x'.repeat(n));
  return doc;
}

const results = { machine: `${os.cpus()[0].model} / ${os.platform()}`, node: process.version, doc_chars: N };

// 1. Per-keystroke edits at random positions.
{
  const r = rng(1);
  const legacy = buildLegacy(N);
  const current = buildCurrent(N);
  results.keystroke_insert_us = {
    legacy: timeEach(100, () => legacy.localInsert(Math.floor(r() * legacy.visibleLen()), 'y')),
    current: timeEach(2000, () => current.localInsert(Math.floor(r() * current.visibleLen()), 'y')),
  };
  if (Y) {
    const text = buildYjs(N).getText('t');
    results.keystroke_insert_us.yjs = timeEach(2000, () => text.insert(Math.floor(r() * text.length), 'y'));
  }
}

// 2. Paste 10k characters into the middle.
{
  const pasted = 'abcdefgh '.repeat(Math.ceil(PASTE / 9)).slice(0, PASTE);
  const legacy = buildLegacy(N);
  let t = performance.now();
  for (let i = 0; i < pasted.length; i++) legacy.localInsert(N / 2 + i, pasted[i]);
  const legacyMs = performance.now() - t;

  const current = buildCurrent(N);
  t = performance.now();
  current.localInsert(N / 2, pasted);
  const currentMs = performance.now() - t;
  if (legacy.text() !== current.text()) throw new Error('paste results differ');

  results.paste_10k_ms = { legacy: Math.round(legacyMs), current: Math.round(currentMs * 100) / 100 };
  if (Y) {
    const text = buildYjs(N).getText('t');
    t = performance.now();
    text.insert(N / 2, pasted);
    results.paste_10k_ms.yjs = Math.round((performance.now() - t) * 100) / 100;
  }
}

// 3. Memory per character (rough: heap delta after building the document).
function heapPerChar(build) {
  global.gc?.();
  const before = process.memoryUsage().heapUsed;
  const keep = build(N);
  global.gc?.();
  const after = process.memoryUsage().heapUsed;
  if (!keep) throw new Error('unreachable');
  return Math.round((after - before) / N);
}
if (global.gc) {
  results.heap_bytes_per_char = { legacy: heapPerChar(buildLegacy), current: heapPerChar(buildCurrent) };
  if (Y) results.heap_bytes_per_char.yjs = heapPerChar(buildYjs);
}

const out = path.join(here, '..', 'docs', 'benchmarks', 'crdt_js.json');
fs.writeFileSync(out, JSON.stringify(results, null, 2));
console.log(JSON.stringify(results, null, 2));
