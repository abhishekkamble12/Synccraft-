/**
 * Replicated Growable Array (RGA) CRDT - JavaScript Client Port.
 *
 * Implements identical algorithm and total ordering rules as the Python crdt/ package,
 * enabling full client-side offline editing and real-time convergence with zero library dependencies.
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
      if (!other) return false;
      return this.clock === other.clock && this.siteId === other.siteId;
    }

    greaterThan(other) {
      if (!other) return true;
      if (this.clock !== other.clock) {
        return this.clock > other.clock;
      }
      return this.siteId > other.siteId;
    }

    lessThan(other) {
      if (!other) return false;
      if (this.clock !== other.clock) {
        return this.clock < other.clock;
      }
      return this.siteId < other.siteId;
    }

    static fromString(str) {
      if (!str || typeof str !== 'string' || !str.includes('@')) {
        throw new Error(`Invalid CharId string: "${str}"`);
      }
      const parts = str.split('@');
      return new CharId(parseInt(parts[0], 10), parts[1]);
    }
  }

  const ROOT = new CharId(0, "");

  // ---------------------------------------------------------------------------
  // Op (Operation)
  // ---------------------------------------------------------------------------
  class Op {
    constructor({ opId, siteId, lamport, type, charId, parentId = null, char = null }) {
      this.opId = opId || `op_${siteId}_${lamport}_${Math.random().toString(36).substring(2, 9)}`;
      this.siteId = siteId;
      this.lamport = Number(lamport);
      this.type = type;
      this.charId = charId instanceof CharId ? charId : CharId.fromString(charId);
      this.parentId = parentId ? (parentId instanceof CharId ? parentId : CharId.fromString(parentId)) : null;
      this.char = char;
    }

    static createInsert({ siteId, lamport, charId, parentId, char, opId = null }) {
      return new Op({
        opId,
        siteId,
        lamport,
        type: 'insert',
        charId,
        parentId,
        char
      });
    }

    static createDelete({ siteId, lamport, charId, opId = null }) {
      return new Op({
        opId,
        siteId,
        lamport,
        type: 'delete',
        charId,
        parentId: null,
        char: null
      });
    }

    toDict() {
      return {
        op_id: this.opId,
        site_id: this.siteId,
        lamport: this.lamport,
        type: this.type,
        char_id: this.charId.toString(),
        parent_id: this.parentId ? this.parentId.toString() : null,
        char: this.char
      };
    }

    static fromDict(dict) {
      return new Op({
        opId: dict.op_id,
        siteId: dict.site_id,
        lamport: dict.lamport,
        type: dict.type,
        charId: dict.char_id,
        parentId: dict.parent_id,
        char: dict.char
      });
    }
  }

  // ---------------------------------------------------------------------------
  // Linked-List Node
  // ---------------------------------------------------------------------------
  class Node {
    constructor({ charId, char, deleted = false, parentId = null }) {
      this.charId = charId;
      this.char = char;
      this.deleted = deleted;
      this.parentId = parentId;
      this.next = null;
      this.prev = null;
    }
  }

  // ---------------------------------------------------------------------------
  // RGA Document
  // ---------------------------------------------------------------------------
  class RGA {
    constructor(siteId, clock = null) {
      this.siteId = siteId;
      this.clock = clock || new LamportClock();

      this._head = new Node({
        charId: ROOT,
        char: "",
        deleted: false,
        parentId: null
      });

      this._nodes = new Map();
      this._nodes.set(ROOT.toString(), this._head);

      this._appliedOpIds = new Set();
      this._pendingOps = [];
    }

    // --- Local Operations ---

    localInsert(pos, char, customOpId = null) {
      if (typeof char !== 'string' || char.length !== 1) {
        throw new Error(`localInsert expects single character, got: "${char}"`);
      }

      const parentNode = pos > 0 ? this._findVisibleNodeAtIndex(pos - 1) : this._head;
      const parentId = parentNode.charId;

      const currentClock = this.clock.tick();
      const newCharId = new CharId(currentClock, this.siteId);

      const op = Op.createInsert({
        siteId: this.siteId,
        lamport: currentClock,
        charId: newCharId,
        parentId: parentId,
        char: char,
        opId: customOpId
      });

      this._applyInsert(op);
      this._appliedOpIds.add(op.opId);
      return op;
    }

    localDelete(pos, customOpId = null) {
      const targetNode = this._findVisibleNodeAtIndex(pos);
      if (!targetNode || targetNode === this._head || targetNode.deleted) {
        throw new Error(`No visible character at index ${pos}`);
      }

      const currentClock = this.clock.tick();
      const op = Op.createDelete({
        siteId: this.siteId,
        lamport: currentClock,
        charId: targetNode.charId,
        opId: customOpId
      });

      this._applyDelete(op);
      this._appliedOpIds.add(op.opId);
      return op;
    }

    // --- Operation Application ---

    apply(op) {
      if (!(op instanceof Op)) {
        op = Op.fromDict(op);
      }

      if (this._appliedOpIds.has(op.opId)) {
        return false;
      }

      this.clock.update(op.lamport);

      if (op.type === 'insert') {
        const parentKey = op.parentId.toString();
        if (this._nodes.has(parentKey)) {
          this._applyInsert(op);
          this._appliedOpIds.add(op.opId);
          this._drainPendingOps();
          return true;
        } else {
          this._pendingOps.push(op);
          return false;
        }
      } else if (op.type === 'delete') {
        const targetKey = op.charId.toString();
        if (this._nodes.has(targetKey)) {
          this._applyDelete(op);
          this._appliedOpIds.add(op.opId);
          return true;
        } else {
          this._pendingOps.push(op);
          return false;
        }
      }

      return false;
    }

    _applyInsert(op) {
      const charKey = op.charId.toString();
      if (this._nodes.has(charKey)) {
        return;
      }

      const parentKey = op.parentId.toString();
      const parentNode = this._nodes.get(parentKey);

      const newNode = new Node({
        charId: op.charId,
        char: op.char,
        deleted: false,
        parentId: op.parentId
      });

      let curr = parentNode;
      while (curr.next !== null) {
        const nextNode = curr.next;
        if (nextNode.charId.greaterThan(op.charId)) {
          curr = nextNode;
        } else {
          break;
        }
      }

      newNode.next = curr.next;
      newNode.prev = curr;
      if (curr.next !== null) {
        curr.next.prev = newNode;
      }
      curr.next = newNode;

      this._nodes.set(charKey, newNode);
    }

    _applyDelete(op) {
      const charKey = op.charId.toString();
      if (this._nodes.has(charKey)) {
        const node = this._nodes.get(charKey);
        node.deleted = true;
      }
    }

    _drainPendingOps() {
      if (this._pendingOps.length === 0) return;

      let progress = true;
      while (progress) {
        progress = false;
        const remaining = [];
        for (const op of this._pendingOps) {
          if (this._appliedOpIds.has(op.opId)) continue;

          if (op.type === 'insert' && this._nodes.has(op.parentId.toString())) {
            this._applyInsert(op);
            this._appliedOpIds.add(op.opId);
            progress = true;
          } else if (op.type === 'delete' && this._nodes.has(op.charId.toString())) {
            this._applyDelete(op);
            this._appliedOpIds.add(op.opId);
            progress = true;
          } else {
            remaining.append ? remaining.append(op) : remaining.push(op);
          }
        }
        this._pendingOps = remaining;
      }
    }

    // --- Queries ---

    text() {
      const chars = [];
      let curr = this._head.next;
      while (curr !== null) {
        if (!curr.deleted) {
          chars.push(curr.char);
        }
        curr = curr.next;
      }
      return chars.join('');
    }

    visibleLen() {
      let count = 0;
      let curr = this._head.next;
      while (curr !== null) {
        if (!curr.deleted) {
          count++;
        }
        curr = curr.next;
      }
      return count;
    }

    charIdAt(pos) {
      const node = this._findVisibleNodeAtIndex(pos);
      if (!node || node === this._head) {
        throw new Error(`Index out of range: ${pos}`);
      }
      return node.charId;
    }

    posOfCharId(charId) {
      const key = charId instanceof CharId ? charId.toString() : charId;
      if (!this._nodes.has(key)) return null;
      const targetNode = this._nodes.get(key);
      if (targetNode.deleted || targetNode === this._head) return null;

      let pos = 0;
      let curr = this._head.next;
      while (curr !== null) {
        if (curr === targetNode) return pos;
        if (!curr.deleted) pos++;
        curr = curr.next;
      }
      return null;
    }

    _findVisibleNodeAtIndex(index) {
      if (index < -1) throw new Error(`Negative index ${index}`);
      if (index === -1) return this._head;

      let currentIdx = 0;
      let curr = this._head.next;
      while (curr !== null) {
        if (!curr.deleted) {
          if (currentIdx === index) return curr;
          currentIdx++;
        }
        curr = curr.next;
      }

      throw new Error(`Index ${index} out of bounds (visible length: ${currentIdx})`);
    }

    // --- Serialization ---

    toDict() {
      const nodesData = [];
      let curr = this._head.next;
      while (curr !== null) {
        nodesData.push({
          char_id: curr.charId.toString(),
          char: curr.char,
          deleted: curr.deleted,
          parent_id: curr.parentId ? curr.parentId.toString() : null
        });
        curr = curr.next;
      }

      return {
        site_id: this.siteId,
        clock: this.clock.value,
        applied_op_ids: Array.from(this._appliedOpIds),
        nodes: nodesData
      };
    }

    static fromDict(data, siteIdOverride = null) {
      const rga = new RGA(
        siteIdOverride || data.site_id,
        new LamportClock(data.clock)
      );
      rga._appliedOpIds = new Set(data.applied_op_ids || []);

      let curr = rga._head;
      for (const nodeDict of (data.nodes || [])) {
        const charId = CharId.fromString(nodeDict.char_id);
        const parentId = nodeDict.parent_id ? CharId.fromString(nodeDict.parent_id) : null;
        const newNode = new Node({
          charId: charId,
          char: nodeDict.char,
          deleted: Boolean(nodeDict.deleted),
          parentId: parentId
        });
        newNode.prev = curr;
        curr.next = newNode;
        rga._nodes.set(charId.toString(), newNode);
        curr = newNode;
      }

      return rga;
    }
  }

  return {
    LamportClock,
    CharId,
    ROOT,
    Op,
    Node,
    RGA
  };
}));
