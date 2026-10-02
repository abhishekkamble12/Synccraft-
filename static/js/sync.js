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
    this.onAIStatusReceived = arguments[0].onAIStatusReceived || (() => {});
    this.onMissedSummaryReceived = arguments[0].onMissedSummaryReceived || (() => {});
    this.onSuggestionReceived = arguments[0].onSuggestionReceived || (() => {});
    this.onSuggestionUpdated = arguments[0].onSuggestionUpdated || (() => {});

    this.socket = null;
    this.isConnected = false;
    this.reconnectAttempts = 0;
    this.maxReconnectAttempts = 50;
    this.baseDelayMs = 500;
    this.maxDelayMs = 10000;

    this.pendingAcks = new Map(); // opId -> sentTimestamp

    // Highest seq N such that every op 1..N has been applied locally. Seqs seen
    // above a hole wait in `seenAhead`; a hole that persists triggers a re-sync,
    // because the server's pub/sub layer may drop broadcasts under load.
    this.lastKnownSeq = this.storage.getLastSeq();
    this.seenAhead = new Set();
    this.gapTimer = null;
    this.gapRepairDelayMs = 1000;
  }

  _markSeq(seq) {
    if (!seq || seq <= this.lastKnownSeq) return;
    this.seenAhead.add(seq);
    while (this.seenAhead.has(this.lastKnownSeq + 1)) {
      this.seenAhead.delete(this.lastKnownSeq + 1);
      this.lastKnownSeq++;
    }
    this.storage.setLastSeq(this.lastKnownSeq);
    if (this.seenAhead.size > 0) this._scheduleGapRepair();
  }

  _resetSeq(seq) {
    this.lastKnownSeq = seq;
    this.seenAhead.clear();
    this.storage.setLastSeq(seq);
  }

  _scheduleGapRepair() {
    if (this.gapTimer) return;
    this.gapTimer = setTimeout(() => {
      this.gapTimer = null;
      if (this.seenAhead.size > 0) {
        this.sendSync(this.lastKnownSeq, this.storage.loadPendingOps(), 'gap');
      }
    }, this.gapRepairDelayMs);
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
      this.sendSync(this.lastKnownSeq, pendingOps, 'reconnect');
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
        this._resetSeq(msg.head_seq);
      }
      this.onInitReceived(msg);

      // Flush any stored pending ops
      const pending = this.storage.loadPendingOps();
      if (pending.length > 0) {
        this.sendSync(this.lastKnownSeq, pending, 'flush');
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
      this._markSeq(msg.seq);

      this.onAckReceived(msg, rtt);
    } else if (type === 'ops') {
      for (const item of msg.ops || []) {
        this.onOpReceived(item.op, item.seq);
        this._markSeq(item.seq);
      }
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
          this._markSeq(seq);
        }
      }

      this.onAckReceived(msg, null);
    } else if (type === 'presence' || type === 'presence_leave') {
      this.onPresenceReceived(msg);
    } else if (type === 'ai_status') {
      this.onAIStatusReceived(msg);
    } else if (type === 'missed_summary') {
      this.onMissedSummaryReceived(msg);
    } else if (type === 'new_suggestion') {
      this.onSuggestionReceived(msg);
    } else if (type === 'suggestion_update') {
      this.onSuggestionUpdated(msg);
    } else if (type === 'error') {
      console.warn('Server error:', msg);
    }
  }

  sendAIRequest(requestData) {
    if (this.isConnected && this.socket && this.socket.readyState === WebSocket.OPEN) {
      this.socket.send(
        JSON.stringify({
          type: 'ai_request',
          ...requestData
        })
      );
    }
  }

  sendAICancel(jobId) {
    if (this.isConnected && this.socket && this.socket.readyState === WebSocket.OPEN) {
      this.socket.send(
        JSON.stringify({
          type: 'ai_cancel',
          job_id: jobId
        })
      );
    }
  }

  sendSuggestionAccept(suggestionId) {
    if (this.isConnected && this.socket && this.socket.readyState === WebSocket.OPEN) {
      this.socket.send(
        JSON.stringify({
          type: 'suggestion_accept',
          suggestion_id: suggestionId
        })
      );
    }
  }

  sendSuggestionReject(suggestionId) {
    if (this.isConnected && this.socket && this.socket.readyState === WebSocket.OPEN) {
      this.socket.send(
        JSON.stringify({
          type: 'suggestion_reject',
          suggestion_id: suggestionId
        })
      );
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

  sendSync(lastSeq, pendingOps, reason = 'gap') {
    const payload = {
      type: 'sync',
      reason,
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
