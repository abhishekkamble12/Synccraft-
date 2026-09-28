/**
 * Main Collaborative Editor Controller with Textarea Diffing and Cursor Anchoring.
 */

class CollaborativeEditorApp {
  constructor(config) {
    this.docId = config.docId;
    this.siteId = config.siteId;
    this.username = config.username;
    this.userRole = config.userRole;
    this.wsUrl = config.wsUrl;

    this.textarea = document.getElementById('collaborativeEditor');
    this.statusDot = document.getElementById('connectionDot');
    this.statusText = document.getElementById('connectionStatusText');
    this.statDocSeq = document.getElementById('statDocSeq');
    this.statLocalOps = document.getElementById('statLocalOps');
    this.statRemoteOps = document.getElementById('statRemoteOps');
    this.statLatency = document.getElementById('statLatency');

    this.localOpsCount = 0;
    this.remoteOpsCount = 0;
    this.isApplyingRemote = false;

    // 1. Initialize local CRDT replica
    const RGAEngine = window.RGAEngine || (typeof require !== 'undefined' ? require('./rga.js') : null);
    this.RGA = RGAEngine.RGA;
    this.CharId = RGAEngine.CharId;
    this.ROOT = RGAEngine.ROOT;

    this.rga = new this.RGA(this.siteId);

    // 2. Initialize Presence Manager
    this.presence = new PresenceManager({
      containerId: 'editorContainer',
      barId: 'presenceBar',
      onBroadcastPresence: (data) => this.sync.sendPresence(data)
    });

    // 3. Initialize WebSocket Sync Client with Offline Persistence
    this.sync = new SyncClient({
      url: this.wsUrl,
      docId: this.docId,
      onInitReceived: (msg) => this._handleInit(msg),
      onOpReceived: (op, seq) => this._handleRemoteOp(op, seq),
      onAckReceived: (msg, rtt) => this._handleAck(msg, rtt),
      onPresenceReceived: (data) => this.presence.handleRemotePresence(data),
      onStatusChange: (status) => this._handleStatusChange(status)
    });

    // 4. Attach Event Listeners
    this._attachEventListeners();

    // 5. Connect
    this.sync.connect();
  }

  _attachEventListeners() {
    this.textarea.addEventListener('input', () => this._handleLocalInput());
    this.textarea.addEventListener('keyup', () => this._handleCursorActivity());
    this.textarea.addEventListener('click', () => this._handleCursorActivity());
  }

  _handleStatusChange(status) {
    if (!this.statusDot || !this.statusText) return;

    this.statusDot.className = 'status-dot';
    if (status === 'connected') {
      this.statusDot.classList.add('connected');
      this.statusText.textContent = 'Connected (Live)';
    } else if (status === 'connecting') {
      this.statusText.textContent = 'Connecting...';
    } else {
      this.statusDot.classList.add('disconnected');
      this.statusText.textContent = 'Offline (Buffering)';
    }
  }

  _handleInit(msg) {
    if (msg.snapshot && msg.snapshot.nodes && msg.snapshot.nodes.length > 0) {
      this.rga = this.RGA.fromDict(msg.snapshot, this.siteId);
    }
    this.textarea.value = this.rga.text();
    if (this.statDocSeq) {
      this.statDocSeq.textContent = msg.head_seq;
    }
  }

  _handleLocalInput() {
    if (this.isApplyingRemote) return;

    const oldText = this.rga.text();
    const newText = this.textarea.value;

    if (oldText === newText) return;

    // Find common prefix
    let prefixLen = 0;
    const minLen = Math.min(oldText.length, newText.length);
    while (prefixLen < minLen && oldText[prefixLen] === newText[prefixLen]) {
      prefixLen++;
    }

    // Find common suffix
    let oldSuffixLen = oldText.length - 1;
    let newSuffixLen = newText.length - 1;
    while (
      oldSuffixLen >= prefixLen &&
      newSuffixLen >= prefixLen &&
      oldText[oldSuffixLen] === newText[newSuffixLen]
    ) {
      oldSuffixLen--;
      newSuffixLen--;
    }

    const deleteCount = oldSuffixLen - prefixLen + 1;
    const insertChars = newText.slice(prefixLen, newSuffixLen + 1);

    // 1. Generate local delete operations (delete from prefixLen)
    for (let i = 0; i < deleteCount; i++) {
      if (prefixLen < this.rga.visibleLen()) {
        const delOp = this.rga.localDelete(prefixLen);
        this.sync.sendOp(delOp);
        this.localOpsCount++;
      }
    }

    // 2. Generate local insert operations
    for (let i = 0; i < insertChars.length; i++) {
      const char = insertChars[i];
      const insertPos = prefixLen + i;
      const insOp = this.rga.localInsert(insertPos, char);
      this.sync.sendOp(insOp);
      this.localOpsCount++;
    }

    if (this.statLocalOps) {
      this.statLocalOps.textContent = this.localOpsCount;
    }

    this._handleCursorActivity();
  }

  _handleRemoteOp(opData, seq) {
    this.isApplyingRemote = true;

    // --- CURSOR ANCHORING ---
    // Record current cursor position and find its preceding CharId anchor
    const cursorPos = this.textarea.selectionStart;
    let anchorCharId = null;
    if (cursorPos > 0 && cursorPos <= this.rga.visibleLen()) {
      try {
        anchorCharId = this.rga.charIdAt(cursorPos - 1);
      } catch (e) {
        anchorCharId = null;
      }
    }

    // Apply remote CRDT operation
    const applied = this.rga.apply(opData);
    if (applied) {
      this.remoteOpsCount++;
      if (this.statRemoteOps) this.statRemoteOps.textContent = this.remoteOpsCount;
      if (this.statDocSeq && seq) this.statDocSeq.textContent = seq;

      // Update textarea content
      this.textarea.value = this.rga.text();

      // Restore cursor position relative to anchored CharId
      let newCursorPos = 0;
      if (anchorCharId) {
        const anchorPos = this.rga.posOfCharId(anchorCharId);
        newCursorPos = anchorPos !== null ? anchorPos + 1 : cursorPos;
      } else {
        newCursorPos = 0;
      }

      newCursorPos = Math.min(newCursorPos, this.textarea.value.length);
      this.textarea.setSelectionRange(newCursorPos, newCursorPos);
    }

    this.isApplyingRemote = false;
  }

  _handleAck(msg, rtt) {
    if (this.statDocSeq && msg.seq) {
      this.statDocSeq.textContent = msg.seq;
    }
    if (rtt !== null && this.statLatency) {
      this.statLatency.textContent = `${rtt} ms`;
    }
  }

  _handleCursorActivity() {
    const pos = this.textarea.selectionStart;
    let anchor = null;
    if (pos > 0 && pos <= this.rga.visibleLen()) {
      try {
        anchor = this.rga.charIdAt(pos - 1);
      } catch (e) {
        anchor = null;
      }
    }
    this.presence.updateLocalCursor(anchor, pos, this.username);
  }
}

if (typeof window !== 'undefined') {
  window.CollaborativeEditorApp = CollaborativeEditorApp;
}
