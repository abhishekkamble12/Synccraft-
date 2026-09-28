/**
 * Persistent Offline Synchronization Engine with IndexedDB / localStorage storage,
 * Exponential Backoff with Jitter, and Idempotent Catch-up Protocol.
 */

class OfflineStorage {
  constructor(docId) {
    this.storageKey = `collab_sync_pending_${docId}`;
    this.seqKey = `collab_sync_last_seq_${docId}`;
  }

  loadPendingOps() {
    try {
      const raw = localStorage.getItem(this.storageKey);
      return raw ? JSON.parse(raw) : [];
    } catch (e) {
      console.warn('Failed to load pending ops from storage:', e);
      return [];
    }
  }

  savePendingOps(ops) {
    try {
      localStorage.setItem(this.storageKey, JSON.stringify(ops));
    } catch (e) {
      console.warn('Failed to save pending ops to storage:', e);
    }
  }

  addPendingOp(op) {
    const ops = this.loadPendingOps();
    const opDict = typeof op.toDict === 'function' ? op.toDict() : op;
    ops.push(opDict);
    this.savePendingOps(ops);
  }

  removeAckedOps(ackedIds) {
    const ackSet = new Set(ackedIds);
    const ops = this.loadPendingOps().filter(op => !ackSet.has(op.op_id || op.opId));
    this.savePendingOps(ops);
    return ops;
  }

  getLastSeq() {
    try {
      return parseInt(localStorage.getItem(this.seqKey) || '0', 10);
    } catch (e) {
      return 0;
    }
  }

  setLastSeq(seq) {
    try {
      localStorage.setItem(this.seqKey, String(seq));
    } catch (e) {
      console.warn('Failed to persist last_seq:', e);
    }
  }
}

class SyncClient {
  constructor({
    url,
    docId,
    onOpReceived,
    onAckReceived,
    onInitReceived,
    onPresenceReceived,
    onStatusChange
  }) {
    this.url = url;
    this.docId = docId || 'default';
    this.storage = new OfflineStorage(this.docId);

    this.onOpReceived = onOpReceived || (() => {});
    this.onAckReceived = onAckReceived || (() => {});
    this.onInitReceived = onInitReceived || (() => {});
    this.onPresenceReceived = onPresenceReceived || (() => {});
    this.onStatusChange = onStatusChange || (() => {});

    this.socket = null;
    this.isConnected = false;
    this.reconnectAttempts = 0;
    this.maxReconnectAttempts = 50;
    this.baseDelayMs = 500;
    this.maxDelayMs = 10000;

    this.pendingAcks = new Map(); // opId -> sentTimestamp
    this.lastKnownSeq = this.storage.getLastSeq();
  }

  connect() {
    this.onStatusChange('connecting');
    try {
      this.socket = new WebSocket(this.url);
    } catch (err) {
      this._handleDisconnect();
      return;
    }

    this.socket.onopen = () => {
      this.isConnected = true;
      this.reconnectAttempts = 0;
      this.onStatusChange('connected');

      // Send synchronization handshake on connection / reconnection
      const pendingOps = this.storage.loadPendingOps();
      this.sendSync(this.lastKnownSeq, pendingOps);
    };

    this.socket.onclose = () => {
      this._handleDisconnect();
    };

    this.socket.onerror = () => {
      this._handleDisconnect();
    };

    this.socket.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        this._handleMessage(msg);
      } catch (err) {
        console.error('Failed to parse WebSocket message:', err);
      }
    };
  }

  _handleDisconnect() {
    if (this.isConnected) {
      this.isConnected = false;
      this.onStatusChange('disconnected');
    }

    if (this.reconnectAttempts < this.maxReconnectAttempts) {
      this.reconnectAttempts++;
      // Exponential backoff with full jitter
      const backoff = Math.min(
        this.baseDelayMs * Math.pow(1.5, this.reconnectAttempts),
        this.maxDelayMs
      );
      const jitter = backoff * (0.75 + Math.random() * 0.5);
      setTimeout(() => this.connect(), jitter);
    }
  }

  _handleMessage(msg) {
    const type = msg.type;

    if (type === 'init') {
      if (msg.head_seq !== undefined) {
        this.lastKnownSeq = msg.head_seq;
        this.storage.setLastSeq(this.lastKnownSeq);
      }
      this.onInitReceived(msg);

      // Flush any stored pending ops
      const pending = this.storage.loadPendingOps();
      if (pending.length > 0) {
        this.sendSync(this.lastKnownSeq, pending);
      }
    } else if (type === 'ack') {
      const opId = msg.op_id;
      const sendTime = this.pendingAcks.get(opId);
      let rtt = null;
      if (sendTime) {
        rtt = Math.round(performance.now() - sendTime);
        this.pendingAcks.delete(opId);
      }

      this.storage.removeAckedOps([opId]);
      if (msg.seq) {
        this.lastKnownSeq = Math.max(this.lastKnownSeq, msg.seq);
        this.storage.setLastSeq(this.lastKnownSeq);
      }

      this.onAckReceived(msg, rtt);
    } else if (type === 'op') {
      if (msg.seq) {
        this.lastKnownSeq = Math.max(this.lastKnownSeq, msg.seq);
        this.storage.setLastSeq(this.lastKnownSeq);
      }
      this.onOpReceived(msg.op, msg.seq);
    } else if (type === 'sync_ack') {
      // 1. Clear acknowledged operations from offline storage
      if (msg.acked && Array.isArray(msg.acked)) {
        this.storage.removeAckedOps(msg.acked);
        for (const ackedId of msg.acked) {
          this.pendingAcks.delete(ackedId);
        }
      }

      // 2. Apply missed operations received while offline
      if (msg.missed && Array.isArray(msg.missed)) {
        for (const missedItem of msg.missed) {
          const op = missedItem.op || missedItem;
          const seq = missedItem.seq || null;
          this.onOpReceived(op, seq);
        }
      }

      if (msg.head_seq) {
        this.lastKnownSeq = Math.max(this.lastKnownSeq, msg.head_seq);
        this.storage.setLastSeq(this.lastKnownSeq);
      }

      this.onAckReceived(msg, null);
    } else if (type === 'presence' || type === 'presence_leave') {
      this.onPresenceReceived(msg);
    } else if (type === 'error') {
      console.warn('Server error:', msg);
    }
  }

  sendOp(op) {
    const opDict = typeof op.toDict === 'function' ? op.toDict() : op;
    const opId = opDict.op_id || opDict.opId;

    this.pendingAcks.set(opId, performance.now());
    this.storage.addPendingOp(opDict);

    if (this.isConnected && this.socket && this.socket.readyState === WebSocket.OPEN) {
      this.socket.send(
        JSON.stringify({
          type: 'op',
          op: opDict
        })
      );
    }
  }

  sendSync(lastSeq, pendingOps) {
    const payload = {
      type: 'sync',
      last_seq: lastSeq,
      pending: pendingOps.map(op => (typeof op.toDict === 'function' ? op.toDict() : op))
    };

    if (this.isConnected && this.socket && this.socket.readyState === WebSocket.OPEN) {
      this.socket.send(JSON.stringify(payload));
    }
  }

  sendPresence(presenceData) {
    if (this.isConnected && this.socket && this.socket.readyState === WebSocket.OPEN) {
      this.socket.send(
        JSON.stringify({
          type: 'presence',
          ...presenceData
        })
      );
    }
  }
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { SyncClient, OfflineStorage };
}
