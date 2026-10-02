/**
 * Replicated Growable Array (RGA) CRDT - JavaScript port of the Python `crdt` package.
 *
 * Same algorithm, ordering rule, wire format and snapshot format as crdt/rga.py;
 * shared JSON test vectors (tests/vectors) hold the two implementations together.
 *
 * - Ops are run-length encoded: an insert carries a run of text, a delete carries
 *   (firstId, count) spans.
 * - An order-statistic treap indexes the document order, so position <-> node
 *   lookups are O(log n).
 * - The unit is the Unicode code point (not the UTF-16 code unit), matching Python.
 */

(function (root, factory) {
  if (typeof define === 'function' && define.amd) {
    define([], factory);
  } else if (typeof module === 'object' && module.exports) {
    module.exports = factory();
  } else {
    root.RGAEngine = factory();
  }
}(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const codePoints = (text) => Array.from(text);

  // ---------------------------------------------------------------------------
  // Lamport Logical Clock
  // ---------------------------------------------------------------------------
  class LamportClock {
    constructor(initialValue = 0) {
      this.value = initialValue;
    }

    tick() {
      this.value += 1;
      return this.value;
    }

    /** Reserve `count` consecutive timestamps; returns the first. */
    reserve(count) {
      const first = this.value + 1;
      this.value += count;
      return first;
    }

    update(remoteTimestamp) {
      this.value = Math.max(this.value, remoteTimestamp) + 1;
      return this.value;
    }

    peek() {
      return this.value;
    }
  }

  // ---------------------------------------------------------------------------
  // CharId Identifier
  // ---------------------------------------------------------------------------
  class CharId {
    constructor(clock, siteId) {
      this.clock = Number(clock);
      this.siteId = String(siteId);
    }

    toString() {
      return `${this.clock}@${this.siteId}`;
    }

    equals(other) {
      return !!other && this.clock === other.clock && this.siteId === other.siteId;
    }

    greaterThan(other) {
      if (!other) return true;
      if (this.clock !== other.clock) return this.clock > other.clock;
      return this.siteId > other.siteId;
    }

    static fromString(str) {
      if (typeof str !== 'string') throw new Error(`Invalid CharId: ${str}`);
      const at = str.indexOf('@');
      if (at < 0) throw new Error(`Invalid CharId string: "${str}"`);
      return new CharId(parseInt(str.slice(0, at), 10), str.slice(at + 1));
    }

    static from(value) {
      return value instanceof CharId ? value : CharId.fromString(value);
    }
  }

  const ROOT = new CharId(0, '');
  const ROOT_KEY = ROOT.toString();

  // ---------------------------------------------------------------------------
  // Op (run-length encoded)
  // ---------------------------------------------------------------------------
  const newOpId = () => {
    if (typeof crypto !== 'undefined' && crypto.randomUUID) {
      return crypto.randomUUID().replace(/-/g, '');
    }
    let id = '';
    for (let i = 0; i < 32; i++) id += Math.floor(Math.random() * 16).toString(16);
    return id;
  };

  class Op {
    constructor({ opId, siteId, lamport, type, charId = null, parentId = null, text = null, spans = [] }) {
      this.opId = opId || newOpId();
      this.siteId = siteId;
      this.lamport = Number(lamport);
      this.type = type;
      this.charId = charId ? CharId.from(charId) : null;
      this.parentId = parentId ? CharId.from(parentId) : null;
      this.text = text;
      this.spans = spans.map(([first, count]) => [CharId.from(first), Number(count)]);
    }

    static createInsert({ siteId, charId, parentId, text, opId = null }) {
      return new Op({
        opId,
        siteId,
        lamport: charId.clock + codePoints(text).length - 1,
        type: 'insert',
        charId,
        parentId,
        text,
      });
    }

    static createDelete({ siteId, lamport, spans, opId = null }) {
      return new Op({ opId, siteId, lamport, type: 'delete', spans });
    }

    get length() {
      if (this.type === 'insert') return codePoints(this.text).length;
      return this.spans.reduce((sum, [, count]) => sum + count, 0);
    }

    *insertedIds() {
      if (this.type !== 'insert') return;
      const n = codePoints(this.text).length;
      for (let i = 0; i < n; i++) yield new CharId(this.charId.clock + i, this.charId.siteId);
    }

    *targetIds() {
      for (const [first, count] of this.spans) {
        for (let i = 0; i < count; i++) yield new CharId(first.clock + i, first.siteId);
      }
    }

    toDict() {
      const base = { op_id: this.opId, site_id: this.siteId, lamport: this.lamport, type: this.type };
      if (this.type === 'insert') {
        base.char_id = this.charId.toString();
        base.parent_id = this.parentId.toString();
        base.text = this.text;
      } else {
        base.spans = this.spans.map(([first, count]) => [first.toString(), count]);
      }
      return base;
    }

    static fromDict(dict) {
      if (dict instanceof Op) return dict;
      if (dict.type === 'insert') {
        return new Op({
          opId: dict.op_id,
          siteId: dict.site_id,
          lamport: dict.lamport,
          type: 'insert',
          charId: dict.char_id,
          parentId: dict.parent_id,
          text: dict.text !== undefined ? dict.text : dict.char,
        });
      }
      const spans = dict.spans ? dict.spans : [[dict.char_id, 1]];
      return new Op({ opId: dict.op_id, siteId: dict.site_id, lamport: dict.lamport, type: 'delete', spans });
    }
  }

  function spansFromIds(ids) {
    const spans = [];
    for (const id of ids) {
      const last = spans[spans.length - 1];
      if (last && last[0].siteId === id.siteId && last[0].clock + last[1] === id.clock) {
        last[1] += 1;
      } else {
        spans.push([id, 1]);
      }
    }
    return spans;
  }

  // ---------------------------------------------------------------------------
  // Order-statistic treap (see crdt/ostree.py for the full explanation)
  // ---------------------------------------------------------------------------
  const size = (n) => (n ? n.size : 0);
  const vis = (n) => (n ? n.vis : 0);
  const pull = (n) => {
    n.size = 1 + size(n.left) + size(n.right);
    n.vis = (n.deleted ? 0 : 1) + vis(n.left) + vis(n.right);
  };

  class OSTree {
    constructor() {
      this.root = null;
    }

    get length() {
      return size(this.root);
    }

    get visibleCount() {
      return vis(this.root);
    }

    insertAfter(anchor, node) {
      node.left = node.right = node.up = null;
      node.prio = Math.random();
      node.size = 1;
      node.vis = node.deleted ? 0 : 1;
      if (this.root === null) {
        this.root = node;
        return;
      }
      let parent;
      if (anchor === null) {
        parent = this.root;
        while (parent.left) parent = parent.left;
        parent.left = node;
      } else if (anchor.right === null) {
        parent = anchor;
        parent.right = node;
      } else {
        parent = anchor.right;
        while (parent.left) parent = parent.left;
        parent.left = node;
      }
      node.up = parent;
      const weight = node.vis;
      for (let walk = parent; walk; walk = walk.up) {
        walk.size += 1;
        walk.vis += weight;
      }
      while (node.up && node.up.prio < node.prio) this._rotateUp(node);
    }

    setDeleted(node, deleted = true) {
      if (node.deleted === deleted) return;
      node.deleted = deleted;
      const delta = deleted ? -1 : 1;
      for (let walk = node; walk; walk = walk.up) walk.vis += delta;
    }

    build(nodes) {
      this.root = null;
      const count = nodes.length;
      if (count === 0) return;
      const prios = Array.from({ length: count }, () => Math.random()).sort((a, b) => b - a);
      let p = 0;
      const rootIdx = Math.floor((count - 1) / 2);
      this.root = nodes[rootIdx];
      const queue = [[0, count - 1, rootIdx]];
      const order = [];
      for (let head = 0; head < queue.length; head++) {
        const [lo, hi, mid] = queue[head];
        const node = nodes[mid];
        node.prio = prios[p++];
        node.left = node.right = null;
        order.push(node);
        if (lo <= mid - 1) {
          const c = Math.floor((lo + mid - 1) / 2);
          node.left = nodes[c];
          nodes[c].up = node;
          queue.push([lo, mid - 1, c]);
        }
        if (mid + 1 <= hi) {
          const c = Math.floor((mid + 1 + hi) / 2);
          node.right = nodes[c];
          nodes[c].up = node;
          queue.push([mid + 1, hi, c]);
        }
      }
      this.root.up = null;
      for (let i = order.length - 1; i >= 0; i--) pull(order[i]);
    }

    kthVisible(k) {
      if (k < 0 || k >= this.visibleCount) {
        throw new Error(`Position ${k} out of range (visible length: ${this.visibleCount})`);
      }
      let node = this.root;
      while (node) {
        const leftVis = vis(node.left);
        if (k < leftVis) {
          node = node.left;
          continue;
        }
        k -= leftVis;
        if (!node.deleted) {
          if (k === 0) return node;
          k -= 1;
        }
        node = node.right;
      }
      throw new Error('subtree visible counts are inconsistent');
    }

    visibleRank(node) {
      let rank = vis(node.left);
      for (let cur = node; cur.up; cur = cur.up) {
        const parent = cur.up;
        if (parent.right === cur) rank += vis(parent.left) + (parent.deleted ? 0 : 1);
      }
      return rank;
    }

    _rotateUp(node) {
      const parent = node.up;
      const grand = parent.up;
      let moved;
      if (parent.left === node) {
        moved = node.right;
        parent.left = moved;
        node.right = parent;
      } else {
        moved = node.left;
        parent.right = moved;
        node.left = parent;
      }
      if (moved) moved.up = parent;
      parent.up = node;
      node.up = grand;
      if (!grand) this.root = node;
      else if (grand.left === parent) grand.left = node;
      else grand.right = node;
      pull(parent);
      pull(node);
    }
  }

  // ---------------------------------------------------------------------------
  // Document node: one character in document order + treap bookkeeping
  // ---------------------------------------------------------------------------
  class Node {
    constructor(charId, char, deleted = false) {
      this.charId = charId;
      this.char = char;
      this.deleted = deleted;
      this.next = null;
      this.prev = null;
      this.left = null;
      this.right = null;
      this.up = null;
      this.prio = 0;
      this.size = 1;
      this.vis = deleted ? 0 : 1;
    }
  }

  // ---------------------------------------------------------------------------
  // RGA Document
  // ---------------------------------------------------------------------------
  class PendingOverflowError extends Error {}

  class RGA {
    constructor(siteId, clock = null, maxPending = 10000) {
      this.siteId = siteId;
      this.clock = clock || new LamportClock();
      this.maxPending = maxPending;
      this._head = new Node(ROOT, '');
      this._tree = new OSTree();
      this._nodes = new Map([[ROOT_KEY, this._head]]);
      this._vv = new Map();
      this._pending = new Map();
    }

    // --- Local operations ---

    localInsert(pos, text, opId = null) {
      if (!text) throw new Error('localInsert needs at least one character');
      if (pos < 0 || pos > this.visibleLen()) {
        throw new Error(`Insert position ${pos} out of range (0..${this.visibleLen()})`);
      }
      const parent = pos === 0 ? this._head : this._tree.kthVisible(pos - 1);
      return this.localInsertAfter(parent.charId, text, opId);
    }

    localInsertAfter(parentId, text, opId = null) {
      if (!text) throw new Error('localInsertAfter needs at least one character');
      parentId = CharId.from(parentId);
      if (!this._nodes.has(parentId.toString())) throw new Error(`Unknown parent ${parentId}`);
      const first = this.clock.reserve(codePoints(text).length);
      const op = Op.createInsert({
        siteId: this.siteId,
        charId: new CharId(first, this.siteId),
        parentId,
        text,
        opId,
      });
      this._integrate(op);
      return op;
    }

    localDelete(pos, length = 1, opId = null) {
      if (length < 1 || pos < 0 || pos + length > this.visibleLen()) {
        throw new Error(`No visible range [${pos}, ${pos + length}) (visible length: ${this.visibleLen()})`);
      }
      let node = this._tree.kthVisible(pos);
      const ids = [];
      while (ids.length < length) {
        if (!node.deleted) ids.push(node.charId);
        node = node.next;
      }
      const op = Op.createDelete({
        siteId: this.siteId,
        lamport: this.clock.tick(),
        spans: spansFromIds(ids),
        opId,
      });
      this._applyDelete(op);
      return op;
    }

    localDeleteIds(ids, opId = null) {
      const present = ids.filter((id) => !id.equals(ROOT) && this._nodes.has(id.toString()));
      if (present.length === 0) return null;
      const op = Op.createDelete({
        siteId: this.siteId,
        lamport: this.clock.tick(),
        spans: spansFromIds(present),
        opId,
      });
      this._applyDelete(op);
      return op;
    }

    // --- Remote operations ---

    apply(op) {
      op = Op.fromDict(op);
      this.clock.update(op.lamport);
      if (op.type === 'insert') {
        if (this._nodes.has(op.charId.toString())) return false;
        if (!this._nodes.has(op.parentId.toString())) {
          this._buffer(op);
          return false;
        }
        this._integrate(op);
        this._drainPending();
        return true;
      }
      for (const id of op.targetIds()) {
        if (!this._nodes.has(id.toString())) {
          this._buffer(op);
          return false;
        }
      }
      return this._applyDelete(op);
    }

    _integrate(op) {
      const first = op.charId;
      let anchor = this._nodes.get(op.parentId.toString());
      let following = anchor.next;
      while (following !== null && following.charId.greaterThan(first)) {
        anchor = following;
        following = anchor.next;
      }
      const chars = codePoints(op.text);
      for (let i = 0; i < chars.length; i++) {
        const node = new Node(new CharId(first.clock + i, first.siteId), chars[i]);
        node.prev = anchor;
        node.next = anchor.next;
        if (anchor.next) anchor.next.prev = node;
        anchor.next = node;
        this._tree.insertAfter(anchor === this._head ? null : anchor, node);
        this._nodes.set(node.charId.toString(), node);
        anchor = node;
      }
      const lastClock = first.clock + chars.length - 1;
      if (lastClock > (this._vv.get(first.siteId) || 0)) this._vv.set(first.siteId, lastClock);
    }

    _applyDelete(op) {
      let changed = false;
      for (const id of op.targetIds()) {
        const node = this._nodes.get(id.toString());
        if (node && node !== this._head && !node.deleted) {
          this._tree.setDeleted(node);
          changed = true;
        }
      }
      return changed;
    }

    _buffer(op) {
      if (this._pending.has(op.opId)) return;
      if (this._pending.size >= this.maxPending) {
        throw new PendingOverflowError(`${this._pending.size} ops waiting on missing dependencies`);
      }
      this._pending.set(op.opId, op);
    }

    _drainPending() {
      let progress = true;
      while (progress && this._pending.size > 0) {
        progress = false;
        for (const [opId, op] of Array.from(this._pending)) {
          if (op.type === 'insert') {
            if (this._nodes.has(op.charId.toString())) {
              this._pending.delete(opId);
            } else if (this._nodes.has(op.parentId.toString())) {
              this._pending.delete(opId);
              this._integrate(op);
              progress = true;
            }
          } else if (Array.from(op.targetIds()).every((id) => this._nodes.has(id.toString()))) {
            this._pending.delete(opId);
            this._applyDelete(op);
          }
        }
      }
    }

    // --- Queries ---

    *iterNodes() {
      for (let node = this._head.next; node !== null; node = node.next) yield node;
    }

    text() {
      const chars = [];
      for (let node = this._head.next; node !== null; node = node.next) {
        if (!node.deleted) chars.push(node.char);
      }
      return chars.join('');
    }

    visibleLen() {
      return this._tree.visibleCount;
    }

    totalLen() {
      return this._tree.length;
    }

    has(charId) {
      return this._nodes.has(charId.toString());
    }

    charIdAt(pos) {
      return this._tree.kthVisible(pos).charId;
    }

    posOfCharId(charId) {
      const node = this._nodes.get(charId.toString());
      if (!node || node === this._head || node.deleted) return null;
      return this._tree.visibleRank(node);
    }

    get versionVector() {
      return Object.fromEntries(this._vv);
    }

    get pendingCount() {
      return this._pending.size;
    }

    // --- Snapshots ---

    toDict() {
      const runs = [];
      let chars = [];
      let site = '';
      let nextClock = -1;
      let deleted = false;
      for (let node = this._head.next; node !== null; node = node.next) {
        const id = node.charId;
        if (chars.length && id.siteId === site && id.clock === nextClock && node.deleted === deleted) {
          chars.push(node.char);
          nextClock += 1;
          continue;
        }
        if (chars.length) runs[runs.length - 1][1] = chars.join('');
        runs.push([id.toString(), '', node.deleted ? 1 : 0]);
        chars = [node.char];
        site = id.siteId;
        nextClock = id.clock + 1;
        deleted = node.deleted;
      }
      if (chars.length) runs[runs.length - 1][1] = chars.join('');
      return { v: 2, site_id: this.siteId, clock: this.clock.value, vv: this.versionVector, runs };
    }

    static fromDict(data, siteIdOverride = null) {
      const rga = new RGA(siteIdOverride || data.site_id || '', new LamportClock(Number(data.clock || 0)));
      const nodes = [];
      if (data.runs) {
        for (const [firstRaw, text, deleted] of data.runs) {
          const first = CharId.fromString(firstRaw);
          const chars = codePoints(text);
          for (let i = 0; i < chars.length; i++) {
            nodes.push(new Node(new CharId(first.clock + i, first.siteId), chars[i], Boolean(deleted)));
          }
        }
      } else {
        for (const item of data.nodes || []) {
          nodes.push(new Node(CharId.fromString(item.char_id), item.char, Boolean(item.deleted)));
        }
      }
      rga._relink(nodes);
      for (const [site, clock] of Object.entries(data.vv || {})) rga._vv.set(site, Number(clock));
      for (const node of nodes) {
        const id = node.charId;
        if (id.clock > (rga._vv.get(id.siteId) || 0)) rga._vv.set(id.siteId, id.clock);
      }
      return rga;
    }

    _relink(nodes) {
      let previous = this._head;
      this._nodes = new Map([[ROOT_KEY, this._head]]);
      for (const node of nodes) {
        node.prev = previous;
        previous.next = node;
        this._nodes.set(node.charId.toString(), node);
        previous = node;
      }
      previous.next = null;
      this._tree.build(nodes);
    }
  }

  // ---------------------------------------------------------------------------
  // Rebase (see crdt/rebase.py): replay unacknowledged ops onto a fresh state
  // ---------------------------------------------------------------------------
  function rebasePending(old, fresh, pending) {
    const remap = new Map();
    const out = [];
    const committed = fresh.versionVector;
    if (old) fresh.clock.update(old.clock.value);

    const resolve = (id) => {
      const mapped = remap.get(id.toString()) || id;
      return fresh.has(mapped) ? mapped : null;
    };

    for (const raw of pending) {
      const op = Op.fromDict(raw);
      if (op.type === 'insert') {
        if (op.charId.clock <= (committed[op.charId.siteId] || 0)) continue;
        let parent = resolve(op.parentId);
        if (parent === null) parent = survivingLeftNeighbour(old, fresh, remap, op.parentId);
        const newOp = fresh.localInsertAfter(parent, op.text);
        const oldIds = Array.from(op.insertedIds());
        const newIds = Array.from(newOp.insertedIds());
        oldIds.forEach((id, i) => remap.set(id.toString(), newIds[i]));
        out.push(newOp);
      } else {
        const alive = Array.from(op.targetIds()).map(resolve).filter((id) => id !== null);
        const newDelete = fresh.localDeleteIds(alive);
        if (newDelete) out.push(newDelete);
      }
    }
    return out;
  }

  function survivingLeftNeighbour(old, fresh, remap, missing) {
    if (old && old.has(missing)) {
      let node = old._nodes.get(missing.toString());
      while (node !== old._head) {
        const id = remap.get(node.charId.toString()) || node.charId;
        if (fresh.has(id)) return id;
        node = node.prev;
      }
      return ROOT;
    }
    const length = fresh.visibleLen();
    return length ? fresh.charIdAt(length - 1) : ROOT;
  }

  return {
    LamportClock,
    CharId,
    ROOT,
    Op,
    Node,
    OSTree,
    RGA,
    PendingOverflowError,
    spansFromIds,
    rebasePending,
    codePoints,
  };
}));
