/**
 * WebSocket sync client: offline persistence, exponential backoff with jitter,
 * sequence-gap repair, GC watermark reporting, and rebase on rejection.
 *
 * Protocol (see docs/DESIGN.md):
 *   server -> init {head_seq, gc_seq, snapshot}     on every (re)connect and on resync
 *   client -> sync {site_id, last_seq, pending:[{op, base_seq}]}
 *   client -> op {op, base_seq}                     base_seq = state the op was made from
 *   server -> ack {op_id, seq} | error {code:"op_rejected", op_id, reason}
 *   server -> ops {ops:[{seq, op}]}                 batched broadcast
 *   client -> stable {seq}                          no future op will be based on < seq
 *   server -> head {seq}                            heartbeat reply: current head of the log
 *   client -> resync                                ask for a fresh init (after a rejection)
 */

class OfflineStorage {
  constructor(docId) {
    this.storageKey = `collab_sync_pending_${docId}`;
    this.seqKey = `collab_sync_last_seq_${docId}`;
  }

  /** Pending entries are {op, base_seq}; bare ops from older versions get base_seq 0. */
  loadPending() {
    try {
      const raw = localStorage.getItem(this.storageKey);
      const items = raw ? JSON.parse(raw) : [];
      return items.map((item) => (item && item.op ? item : { op: item, base_seq: 0 }));
    } catch (e) {
      console.warn('Failed to load pending ops from storage:', e);
      return [];
    }
  }

  savePending(entries) {
    try {
      localStorage.setItem(this.storageKey, JSON.stringify(entries));
    } catch (e) {
      console.warn('Failed to save pending ops to storage:', e);
    }
  }

  addPending(entry) {
    const entries = this.loadPending();
    entries.push(entry);
    this.savePending(entries);
  }

  removeAcked(ackedIds) {
    const ackSet = new Set(ackedIds);
    const entries = this.loadPending().filter((e) => !ackSet.has(e.op.op_id));
    this.savePending(entries);
    return entries;
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
  constructor(options) {
    this.url = options.url;
    this.docId = options.docId || 'default';
    this.siteId = options.siteId || null;
    this.storage = new OfflineStorage(this.docId);

    const noop = () => {};
    // onOpsReceived(items) gets each batch [{op, seq}] at once so the editor renders once.
    this.onOpsReceived = options.onOpsReceived || null;
    this.onOpReceived = options.onOpReceived || noop;
    this.onAckReceived = options.onAckReceived || noop;
    // onInitReceived(msg, pendingEntries, mustRebase) -> {pending, siteId} | undefined
    this.onInitReceived = options.onInitReceived || noop;
    this.onPresenceReceived = options.onPresenceReceived || noop;
    this.onStatusChange = options.onStatusChange || noop;
    this.onRejected = options.onRejected || noop;
    this.onAIStatusReceived = options.onAIStatusReceived || noop;
    this.onMissedSummaryReceived = options.onMissedSummaryReceived || noop;
    this.onSuggestionReceived = options.onSuggestionReceived || noop;
    this.onSuggestionUpdated = options.onSuggestionUpdated || noop;

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

    // Set when the server rejected one of our ops: the next init rebases everything.
    this.rebaseRequired = false;
    this.resyncRequested = false;

    this.stableIntervalMs = 5000;
    this.lastStableSent = -1;
    this.stableTimer = null;
  }

  // --- Sequence tracking ---

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
        this.sendSync(this.lastKnownSeq, this.storage.loadPending(), 'gap');
      }
    }, this.gapRepairDelayMs);
  }

  /** Oldest state any not-yet-acknowledged op of ours is based on (the GC watermark). */
  stableSeq() {
    let floor = this.lastKnownSeq;
    for (const entry of this.storage.loadPending()) floor = Math.min(floor, entry.base_seq || 0);
    return floor;
  }

  _reportStable() {
    const seq = this.stableSeq();
    if (seq !== this.lastStableSent && this._send({ type: 'stable', seq })) {
      this.lastStableSent = seq;
    }
  }

  // --- Connection lifecycle ---

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
      // The server sends `init` right after accepting; the handshake continues there.
      if (!this.stableTimer && typeof setInterval !== 'undefined') {
        this.stableTimer = setInterval(() => this._reportStable(), this.stableIntervalMs);
      }
    };
    this.socket.onclose = () => this._handleDisconnect();
    this.socket.onerror = () => this._handleDisconnect();
    this.socket.onmessage = (event) => {
      try {
        this._handleMessage(JSON.parse(event.data));
      } catch (err) {
        console.error('Failed to handle WebSocket message:', err);
      }
    };
  }

  _handleDisconnect() {
    if (this.isConnected) {
      this.isConnected = false;
      this.onStatusChange('disconnected');
    }
    this.resyncRequested = false;
    if (this.reconnectAttempts < this.maxReconnectAttempts) {
      this.reconnectAttempts++;
      const backoff = Math.min(this.baseDelayMs * Math.pow(1.5, this.reconnectAttempts), this.maxDelayMs);
      const jitter = backoff * (0.75 + Math.random() * 0.5);
      setTimeout(() => this.connect(), jitter);
    }
  }

  // --- Incoming messages ---

  _deliver(items) {
    if (items.length === 0) return;
    if (this.onOpsReceived) this.onOpsReceived(items);
    else for (const item of items) this.onOpReceived(item.op, item.seq);
    for (const item of items) this._markSeq(item.seq);
  }

  _requestResync() {
    this.rebaseRequired = true;
    if (!this.resyncRequested && this._send({ type: 'resync' })) this.resyncRequested = true;
  }

  _handleMessage(msg) {
    const type = msg.type;

    if (type === 'init') {
      this.resyncRequested = false;
      this._resetSeq(msg.head_seq || 0);
      let pending = this.storage.loadPending();
      const mustRebase =
        pending.length > 0 &&
        (this.rebaseRequired || pending.some((e) => (e.base_seq || 0) < (msg.gc_seq || 0)));
      const outcome = this.onInitReceived(msg, pending, mustRebase);
      if (outcome) {
        pending = outcome.pending;
        if (outcome.siteId) this.siteId = outcome.siteId;
        this.storage.savePending(pending);
      }
      this.rebaseRequired = false;
      this.lastStableSent = -1;
      this.sendSync(this.lastKnownSeq, pending, 'reconnect');
    } else if (type === 'ack') {
      const sendTime = this.pendingAcks.get(msg.op_id);
      let rtt = null;
      if (sendTime) {
        rtt = Math.round(performance.now() - sendTime);
        this.pendingAcks.delete(msg.op_id);
      }
      this.storage.removeAcked([msg.op_id]);
      this._markSeq(msg.seq);
      this.onAckReceived(msg, rtt);
    } else if (type === 'ops') {
      this._deliver(msg.ops || []);
    } else if (type === 'head') {
      // Heartbeat reply. If the log is ahead of us, broadcasts were lost (e.g. the
      // pub/sub layer restarted); recording the head as "seen ahead" schedules a re-sync.
      this._markSeq(msg.seq);
    } else if (type === 'sync_ack') {
      if (Array.isArray(msg.acked)) {
        this.storage.removeAcked(msg.acked);
        for (const id of msg.acked) this.pendingAcks.delete(id);
      }
      this._deliver((msg.missed || []).map((m) => ({ op: m.op || m, seq: m.seq || null })));
      this.onAckReceived(msg, null);
      if (Array.isArray(msg.rejected) && msg.rejected.length > 0) {
        this.onRejected(msg.rejected);
        this._requestResync();
      }
    } else if (type === 'error' && msg.code === 'op_rejected') {
      this.onRejected([{ op_id: msg.op_id, reason: msg.reason }]);
      this._requestResync();
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

  // --- Outgoing messages ---

  _send(payload) {
    if (this.isConnected && this.socket && this.socket.readyState === WebSocket.OPEN) {
      this.socket.send(JSON.stringify(payload));
      return true;
    }
    return false;
  }

  sendOp(op) {
    const entry = { op: typeof op.toDict === 'function' ? op.toDict() : op, base_seq: this.lastKnownSeq };
    this.pendingAcks.set(entry.op.op_id, performance.now());
    this.storage.addPending(entry);
    // While a rebase is pending, new ops wait in storage and go out with the next sync.
    if (!this.rebaseRequired) this._send({ type: 'op', ...entry });
  }

  sendSync(lastSeq, pending, reason = 'gap') {
    this._send({ type: 'sync', reason, site_id: this.siteId, last_seq: lastSeq, pending });
  }

  sendAIRequest(requestData) {
    this._send({ type: 'ai_request', ...requestData });
  }

  sendAICancel(jobId) {
    this._send({ type: 'ai_cancel', job_id: jobId });
  }

  sendSuggestionAccept(suggestionId) {
    this._send({ type: 'suggestion_accept', suggestion_id: suggestionId });
  }

  sendSuggestionReject(suggestionId) {
    this._send({ type: 'suggestion_reject', suggestion_id: suggestionId });
  }

  sendPresence(presenceData) {
    this._send({ type: 'presence', ...presenceData });
  }
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { SyncClient, OfflineStorage };
}
