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
    this.aiMenu = document.getElementById('aiMenu');
    this.aiMenuBtn = document.getElementById('aiMenuBtn');
    this.aiMenuHint = document.getElementById('aiMenuHint');
    this.suggestionCard = document.getElementById('suggestionCard');

    // 1. Initialize local CRDT replica
    const RGAEngine = window.RGAEngine || (typeof require !== 'undefined' ? require('./rga.js') : null);
    this.RGA = RGAEngine.RGA;
    this.CharId = RGAEngine.CharId;
    this.ROOT = RGAEngine.ROOT;
    this.rebasePending = RGAEngine.rebasePending;

    this.rga = new this.RGA(this.siteId);
    // The CRDT counts code points; the textarea counts UTF-16 units. They only differ
    // when the text contains astral characters (emoji), so the conversion is skipped
    // otherwise.
    this.hasAstral = false;

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
      onInitReceived: (msg, pending, mustRebase) => this._handleInit(msg, pending, mustRebase),
      onAckReceived: (msg, rtt) => this._handleAck(msg, rtt),
      siteId: this.siteId,
      onOpsReceived: (items) => this._handleRemoteOps(items),
      onRejected: (rejected) => this._handleRejected(rejected),
      onPresenceReceived: (data) => this.presence.handleRemotePresence(data),
      onStatusChange: (status) => this._handleStatusChange(status),
      onAIStatusReceived: (msg) => this._handleAIStatus(msg),
      onServerError: (msg) => this._handleServerError(msg),
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
        // Keep focus (and the visible selection) in the textarea.
        btn.addEventListener('mousedown', (e) => e.preventDefault());
        btn.addEventListener('click', (e) => {
          e.preventDefault();
          const action = btn.getAttribute('data-action');
          this._triggerAIAction(action);
        });
      });
    }

    if (this.aiMenu && this.aiMenuBtn) {
      this.aiMenuBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        this._toggleAiMenu(!this.aiMenu.classList.contains('is-open'));
      });
      this.aiMenu.querySelectorAll('button[data-action]').forEach((btn) => {
        btn.addEventListener('click', (e) => {
          e.preventDefault();
          this._toggleAiMenu(false);
          this._triggerAIAction(btn.getAttribute('data-action'));
        });
      });
      document.addEventListener('click', (e) => {
        if (!this.aiMenu.contains(e.target)) this._toggleAiMenu(false);
      });
      document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') this._toggleAiMenu(false);
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
      const toolbarHeight = this.floatingToolbar.offsetHeight || 40;
      const containerWidth = this.textarea.clientWidth;
      const halfWidth = (this.floatingToolbar.offsetWidth || 400) / 2;
      // Sit above the selection, or below it when there is no room (first lines);
      // the container clips overflow, so a toolbar above line 1 would be invisible.
      const below = coords.top < toolbarHeight + 12;
      this.floatingToolbar.classList.toggle('is-below', below);
      const top = below ? coords.top + coords.height : coords.top;
      const left = Math.min(Math.max(halfWidth + 8, coords.left), containerWidth - halfWidth - 8);
      this.floatingToolbar.style.top = `${top}px`;
      this.floatingToolbar.style.left = `${left}px`;
    } else {
      this.floatingToolbar.style.display = 'none';
    }
  }

  _toggleAiMenu(open) {
    if (!this.aiMenu) return;
    if (open && this.aiMenuHint) {
      const hasSelection = this.textarea.selectionEnd > this.textarea.selectionStart;
      this.aiMenuHint.textContent = hasSelection
        ? 'Applies to the selected text'
        : 'Applies to the whole document';
    }
    this.aiMenu.classList.toggle('is-open', open);
    if (this.aiMenuBtn) this.aiMenuBtn.setAttribute('aria-expanded', String(open));
  }

  _triggerAIAction(action) {
    const start = this.textarea.selectionStart;
    const end = this.textarea.selectionEnd;
    const hasSelection = end > start;

    if (this.activeAiJobId) {
      if (window.toast) toast.warning('An AI request is already running');
      return;
    }
    if (!this.textarea.value.trim()) {
      if (window.toast) toast.warning('Write something first: AI works on existing text');
      return;
    }
    if (!this.sync.isConnected) {
      if (window.toast) toast.error('Not connected: AI requests need a live connection');
      return;
    }

    // Anchors are the first and last selected characters; with no selection both
    // are omitted and the server uses the whole document.
    let startAnchor = null;
    let endAnchor = null;
    if (hasSelection) {
      const first = this._anchorBefore(this._nextOffset(start));
      const last = this._anchorBefore(end);
      if (first) startAnchor = first.toString();
      if (last) endAnchor = last.toString();
    }

    const target = hasSelection ? 'selected text' : 'the whole document';
    this.sync.sendAIRequest({
      kind: action,
      anchor_start: startAnchor,
      anchor_end: endAnchor,
      instruction: `Perform ${action} on ${target}`
    });

    if (this.floatingToolbar) {
      this.floatingToolbar.style.display = 'none';
    }
    if (window.toast) {
      toast.info(`AI ${action} requested for ${target}...`);
    }
  }

  /** UTF-16 offset just past the code point starting at `utf16Pos`. */
  _nextOffset(utf16Pos) {
    const code = this.textarea.value.codePointAt(utf16Pos);
    return utf16Pos + (code !== undefined && code > 0xffff ? 2 : 1);
  }

  _handleServerError(msg) {
    if (!window.toast) return;
    if (['rate_limited', 'token_budget_exceeded', 'forbidden', 'bad_request'].includes(msg.code)) {
      toast.error(msg.message || 'Request failed');
    }
  }

  _handleAIStatus(msg) {
    if (!this.aiStatusPill || !this.aiStatusText) return;

    if (msg.status === 'queued' || msg.status === 'running') {
      this.activeAiJobId = msg.job_id;
      this.aiStatusPill.style.display = 'inline-flex';
      this.aiStatusText.textContent = msg.status === 'queued' ? 'AI queued...' : 'AI writing...';
    } else if (msg.status === 'done') {
      const isSuggestion = (msg.message || '').startsWith('Suggestion');
      this.aiStatusText.textContent = isSuggestion ? 'AI suggestion ready ✓' : 'AI edit applied ✓';
      if (window.toast) {
        toast.success(isSuggestion ? 'AI suggestion ready for review' : 'AI changes applied successfully');
      }
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
      if (window.toast) toast.error(msg.message ? `AI failed: ${msg.message}` : 'AI generation encountered an error');
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

  // --- Text offset helpers (UTF-16 textarea <-> code-point CRDT) ---

  _refreshAstral(text) {
    this.hasAstral = /[\uD800-\uDFFF]/.test(text);
  }

  _cpIndex(text, utf16Index) {
    return this.hasAstral ? Array.from(text.slice(0, utf16Index)).length : utf16Index;
  }

  _utf16Index(text, cpIndex) {
    if (!this.hasAstral) return cpIndex;
    let i = 0;
    for (let n = 0; n < cpIndex && i < text.length; n++) {
      i += text.codePointAt(i) > 0xffff ? 2 : 1;
    }
    return i;
  }

  /** CharId of the character just before textarea offset `utf16Pos`, or null at the start. */
  _anchorBefore(utf16Pos) {
    const cp = this._cpIndex(this.textarea.value, utf16Pos);
    if (cp <= 0 || cp > this.rga.visibleLen()) return null;
    return this.rga.charIdAt(cp - 1);
  }

  /** Textarea offset just after `anchor` (or `fallback` if it was deleted). */
  _offsetAfter(anchor, fallback) {
    if (!anchor) return 0;
    const pos = this.rga.posOfCharId(anchor);
    if (pos === null) return Math.min(fallback, this.textarea.value.length);
    return this._utf16Index(this.textarea.value, pos + 1);
  }

  _render() {
    const text = this.rga.text();
    this._refreshAstral(text);
    this.textarea.value = text;
    this._updateWordCount();
  }

  // --- Sync callbacks ---

  _handleInit(msg, pending, mustRebase) {
    const fresh = this.RGA.fromDict(msg.snapshot || {}, this.siteId);
    let outcome;
    if (mustRebase) {
      // Our pending ops were rejected or reference tombstones the server has since
      // collected. Replay them onto the fresh state under a new site id.
      const newSiteId = `${this.siteId.split('~')[0]}~${Math.random().toString(36).slice(2, 8)}`;
      fresh.siteId = newSiteId;
      const ops = this.rebasePending(this.rga, fresh, pending.map((e) => e.op));
      outcome = {
        siteId: newSiteId,
        pending: ops.map((op) => ({ op: op.toDict(), base_seq: msg.head_seq })),
      };
      this.siteId = newSiteId;
      if (window.toast && pending.length) toast.info('Re-applied your unsynced edits to the latest version');
    } else {
      for (const entry of pending) fresh.apply(entry.op);
      outcome = { pending };
    }

    const selStart = this._anchorBefore(this.textarea.selectionStart);
    this.rga = fresh;
    this._render();
    const pos = this._offsetAfter(selStart, this.textarea.selectionStart);
    this.textarea.setSelectionRange(pos, pos);
    if (this.statDocSeq) this.statDocSeq.textContent = msg.head_seq;
    this.presence._updateAllCursorPositions();
    return outcome;
  }

  _handleRejected(rejected) {
    console.warn('Server rejected ops; rebasing:', rejected);
  }

  _handleLocalInput() {
    if (this.isApplyingRemote) return;

    const oldText = this.rga.text();
    const newText = this.textarea.value;
    if (oldText === newText) return;

    // Common prefix / suffix in UTF-16 units, never splitting a surrogate pair.
    let prefix = 0;
    const minLen = Math.min(oldText.length, newText.length);
    while (prefix < minLen && oldText.charCodeAt(prefix) === newText.charCodeAt(prefix)) prefix++;
    if (prefix > 0 && /[\uD800-\uDBFF]/.test(oldText[prefix - 1])) prefix--;

    let oldEnd = oldText.length;
    let newEnd = newText.length;
    while (oldEnd > prefix && newEnd > prefix && oldText.charCodeAt(oldEnd - 1) === newText.charCodeAt(newEnd - 1)) {
      oldEnd--;
      newEnd--;
    }
    if (oldEnd < oldText.length && /[\uDC00-\uDFFF]/.test(oldText[oldEnd])) {
      oldEnd++;
      newEnd++;
    }

    const astral = this.hasAstral || /[\uD800-\uDFFF]/.test(newText);
    const cpPos = astral ? Array.from(oldText.slice(0, prefix)).length : prefix;
    const deleteCount = astral ? Array.from(oldText.slice(prefix, oldEnd)).length : oldEnd - prefix;
    const inserted = newText.slice(prefix, newEnd);

    // One run-length op per side of the edit, however many characters it touches.
    if (deleteCount > 0) {
      this.sync.sendOp(this.rga.localDelete(cpPos, deleteCount));
      this.localOpsCount++;
    }
    if (inserted.length > 0) {
      this.sync.sendOp(this.rga.localInsert(cpPos, inserted));
      this.localOpsCount++;
    }
    this._refreshAstral(newText);

    if (this.statLocalOps) this.statLocalOps.textContent = this.localOpsCount;
    this._handleCursorActivity();
  }

  _handleRemoteOps(items) {
    this.isApplyingRemote = true;

    // Anchor the selection to characters, not offsets, so remote edits don't move it.
    const startAnchor = this._anchorBefore(this.textarea.selectionStart);
    const endAnchor = this._anchorBefore(this.textarea.selectionEnd);
    const startFallback = this.textarea.selectionStart;
    const endFallback = this.textarea.selectionEnd;

    let changed = false;
    let lastSeq = null;
    for (const { op, seq } of items) {
      if (this.rga.apply(op)) {
        changed = true;
        this.remoteOpsCount++;
      }
      if (seq) lastSeq = seq;
    }

    if (this.statDocSeq && lastSeq) this.statDocSeq.textContent = lastSeq;
    if (changed) {
      if (this.statRemoteOps) this.statRemoteOps.textContent = this.remoteOpsCount;
      // One re-render per batch, not per op.
      this._render();
      const start = this._offsetAfter(startAnchor, startFallback);
      const end = Math.max(start, this._offsetAfter(endAnchor, endFallback));
      this.textarea.setSelectionRange(start, end);
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
    this.presence.updateLocalCursor(this._anchorBefore(pos), pos, this.username);
  }

  /** Where a collaborator's cursor is now, from the CharId they reported. */
  resolveRemoteCursor(anchorString, fallback) {
    if (!anchorString) return Math.min(fallback || 0, this.textarea.value.length);
    try {
      return this._offsetAfter(this.CharId.fromString(anchorString), fallback || 0);
    } catch (e) {
      return Math.min(fallback || 0, this.textarea.value.length);
    }
  }
}

if (typeof window !== 'undefined') {
  window.CollaborativeEditorApp = CollaborativeEditorApp;
}
