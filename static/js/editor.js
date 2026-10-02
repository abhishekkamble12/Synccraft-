/**
 * Main Collaborative Editor Controller with Textarea Diffing, Cursor Anchoring,
 * Selection-Aware AI Floating Toolbar, Word/Character Counters, and Toast Notifications.
 */

class CollaborativeEditorApp {
  constructor(config) {
    this.docId = config.docId;
    this.siteId = config.siteId;
    this.username = config.username;
    this.userRole = config.userRole;
    this.wsUrl = config.wsUrl;
    this.csrfToken = config.csrfToken;

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
    this.activeAiJobId = null;

    // AI UI Elements
    this.aiStatusPill = document.getElementById('aiStatusPill');
    this.aiStatusText = document.getElementById('aiStatusText');
    this.cancelAiBtn = document.getElementById('cancelAiBtn');
    this.floatingToolbar = document.getElementById('floatingAiToolbar');
    this.suggestionCard = document.getElementById('suggestionCard');

    // 1. Initialize local CRDT replica
    const RGAEngine = window.RGAEngine || (typeof require !== 'undefined' ? require('./rga.js') : null);
    this.RGA = RGAEngine.RGA;
    this.CharId = RGAEngine.CharId;
    this.ROOT = RGAEngine.ROOT;

    this.rga = new this.RGA(this.siteId);

    // 2. Initialize Presence Manager with Remote Cursor rendering
    this.presence = new PresenceManager({
      containerId: 'editorContainer',
      barId: 'presenceBar',
      textareaId: 'collaborativeEditor',
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
      onStatusChange: (status) => this._handleStatusChange(status),
      onAIStatusReceived: (msg) => this._handleAIStatus(msg),
      onMissedSummaryReceived: (msg) => this._handleMissedSummary(msg),
      onSuggestionReceived: (msg) => this._handleSuggestion(msg),
      onSuggestionUpdated: (msg) => this._handleSuggestionUpdated(msg)
    });

    // 4. Attach Event Listeners & Title Editor
    this._attachEventListeners();
    this._initTitleEditor();
    this._updateWordCount();

    // 5. Connect
    this.sync.connect();
  }

  _attachEventListeners() {
    this.textarea.addEventListener('input', () => {
      this._handleLocalInput();
      this._updateWordCount();
      this.presence._updateAllCursorPositions();
    });

    this.textarea.addEventListener('keyup', () => {
      this._handleCursorActivity();
      this._handleSelectionChange();
    });

    this.textarea.addEventListener('mouseup', () => {
      this._handleSelectionChange();
    });

    this.textarea.addEventListener('click', () => {
      this._handleCursorActivity();
      this._handleSelectionChange();
    });

    if (this.cancelAiBtn) {
      this.cancelAiBtn.addEventListener('click', () => {
        if (this.activeAiJobId) {
          this.sync.sendAICancel(this.activeAiJobId);
        }
      });
    }

    if (this.floatingToolbar) {
      const buttons = this.floatingToolbar.querySelectorAll('button[data-action]');
      buttons.forEach((btn) => {
        btn.addEventListener('click', (e) => {
          e.preventDefault();
          const action = btn.getAttribute('data-action');
          this._triggerAIAction(action);
        });
      });
    }

    // Keyboard shortcuts for suggestion card
    window.addEventListener('keydown', (e) => {
      if (this.suggestionCard && this.suggestionCard.style.display !== 'none') {
        if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
          e.preventDefault();
          const acceptBtn = document.getElementById('acceptSuggestionBtn');
          if (acceptBtn) acceptBtn.click();
        } else if (e.key === 'Escape') {
          const rejectBtn = document.getElementById('rejectSuggestionBtn');
          if (rejectBtn) rejectBtn.click();
        }
      }
    });
  }

  _initTitleEditor() {
    const titleEl = document.getElementById('editableDocTitle');
    const inputEl = document.getElementById('docTitleInput');
    const editBtn = document.getElementById('editTitleBtn');
    if (!titleEl || !inputEl) return;

    const startEdit = () => {
      if (this.userRole === 'viewer') return;
      titleEl.style.display = 'none';
      if (editBtn) editBtn.style.display = 'none';
      inputEl.style.display = 'inline-block';
      inputEl.value = titleEl.textContent.trim();
      inputEl.focus();
      inputEl.select();
    };

    const saveEdit = async () => {
      const newTitle = inputEl.value.trim();
      inputEl.style.display = 'none';
      titleEl.style.display = 'inline-block';
      if (editBtn) editBtn.style.display = 'inline-flex';

      if (!newTitle || newTitle === titleEl.textContent.trim()) return;

      try {
        const res = await fetch(`/docs/${this.docId}/rename/`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': this.csrfToken
          },
          body: JSON.stringify({ title: newTitle })
        });
        const data = await res.json();
        if (res.ok && data.success) {
          titleEl.textContent = data.title;
          document.title = `${data.title} — Monach Sync`;
          if (window.toast) toast.success('Document renamed');
        } else {
          if (window.toast) toast.error(data.error || 'Failed to rename');
        }
      } catch (err) {
        if (window.toast) toast.error('Error renaming document');
      }
    };

    if (editBtn) editBtn.addEventListener('click', startEdit);
    titleEl.addEventListener('click', startEdit);
    inputEl.addEventListener('blur', saveEdit);
    inputEl.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        inputEl.blur();
      } else if (e.key === 'Escape') {
        inputEl.value = titleEl.textContent.trim();
        inputEl.style.display = 'none';
        titleEl.style.display = 'inline-block';
        if (editBtn) editBtn.style.display = 'inline-flex';
      }
    });
  }

  _updateWordCount() {
    const text = this.textarea.value.trim();
    const words = text ? text.split(/\s+/).length : 0;
    const chars = this.textarea.value.length;
    const wordEl = document.getElementById('statWordCount');
    const charEl = document.getElementById('statCharCount');
    if (wordEl) wordEl.textContent = words;
    if (charEl) charEl.textContent = chars;
  }

  _handleSelectionChange() {
    if (!this.floatingToolbar || this.userRole === 'viewer') return;
    const start = this.textarea.selectionStart;
    const end = this.textarea.selectionEnd;

    if (end > start) {
      this.floatingToolbar.style.display = 'flex';
      const coords = this.presence._getCaretCoordinates(start);
      const top = Math.max(10, coords.top - 48);
      const left = Math.max(120, coords.left);
      this.floatingToolbar.style.top = `${top}px`;
      this.floatingToolbar.style.left = `${left}px`;
    } else {
      this.floatingToolbar.style.display = 'none';
    }
  }

  _triggerAIAction(action) {
    const start = this.textarea.selectionStart;
    const end = this.textarea.selectionEnd;

    let startAnchor = null;
    let endAnchor = null;

    if (start > 0 && start <= this.rga.visibleLen()) {
      try {
        startAnchor = this.rga.charIdAt(start - 1).toString();
      } catch (e) {
        startAnchor = null;
      }
    }

    if (end > 0 && end <= this.rga.visibleLen()) {
      try {
        endAnchor = this.rga.charIdAt(end - 1).toString();
      } catch (e) {
        endAnchor = null;
      }
    }

    this.sync.sendAIRequest({
      kind: action,
      anchor_start: startAnchor,
      anchor_end: endAnchor,
      instruction: `Perform ${action} on selected text`
    });

    if (this.floatingToolbar) {
      this.floatingToolbar.style.display = 'none';
    }
    if (window.toast) {
      toast.info(`AI ${action} requested...`);
    }
  }

  _handleAIStatus(msg) {
    if (!this.aiStatusPill || !this.aiStatusText) return;

    if (msg.status === 'queued' || msg.status === 'running') {
      this.activeAiJobId = msg.job_id;
      this.aiStatusPill.style.display = 'inline-flex';
      this.aiStatusText.textContent = msg.status === 'queued' ? 'AI queued...' : 'AI writing...';
    } else if (msg.status === 'done') {
      this.aiStatusText.textContent = 'AI edit applied ✓';
      if (window.toast) toast.success('AI changes applied successfully');
      setTimeout(() => {
        this.aiStatusPill.style.display = 'none';
      }, 2500);
      this.activeAiJobId = null;
    } else if (msg.status === 'cancelled') {
      this.aiStatusText.textContent = 'AI cancelled';
      if (window.toast) toast.warning('AI request was cancelled');
      setTimeout(() => {
        this.aiStatusPill.style.display = 'none';
      }, 2000);
      this.activeAiJobId = null;
    } else if (msg.status === 'failed') {
      this.aiStatusText.textContent = 'AI error';
      if (window.toast) toast.error('AI generation encountered an error');
      setTimeout(() => {
        this.aiStatusPill.style.display = 'none';
      }, 2000);
      this.activeAiJobId = null;
    }
  }

  _handleMissedSummary(msg) {
    const modal = document.getElementById('missedSummaryModal');
    const bullets = document.getElementById('summaryBullets');
    const authors = document.getElementById('summaryAuthors');
    if (!modal || !bullets) return;

    if (authors && msg.authors) {
      authors.textContent = `Edits made by: ${msg.authors.join(', ')} (Revisions #${msg.from_seq} → #${msg.to_seq})`;
    }
    bullets.textContent = msg.summary;
    modal.classList.add('is-open');
  }

  _handleSuggestion(msg) {
    if (!this.suggestionCard) return;
    const body = document.getElementById('suggestionProposedText');
    const acceptBtn = document.getElementById('acceptSuggestionBtn');
    const rejectBtn = document.getElementById('rejectSuggestionBtn');

    if (body) body.textContent = msg.proposed_text;
    this.suggestionCard.style.display = 'flex';

    if (acceptBtn) {
      acceptBtn.onclick = () => {
        this.sync.sendSuggestionAccept(msg.suggestion_id);
        this.suggestionCard.style.display = 'none';
        if (window.toast) toast.success('Suggestion accepted');
      };
    }
    if (rejectBtn) {
      rejectBtn.onclick = () => {
        this.sync.sendSuggestionReject(msg.suggestion_id);
        this.suggestionCard.style.display = 'none';
        if (window.toast) toast.info('Suggestion discarded');
      };
    }
  }

  _handleSuggestionUpdated(msg) {
    if (this.suggestionCard) {
      this.suggestionCard.style.display = 'none';
    }
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
    this._updateWordCount();
    if (this.statDocSeq) {
      this.statDocSeq.textContent = msg.head_seq;
    }
    this.presence._updateAllCursorPositions();
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

    // 1. Generate local delete operations
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
      this._updateWordCount();

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
      this.presence._updateAllCursorPositions();
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
