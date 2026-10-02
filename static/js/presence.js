/**
 * Live Presence and Remote Cursor Manager.
 * Handles avatar stacks, tooltips, and real-time remote cursor rendering on textareas.
 */

class PresenceManager {
  constructor({ containerId, barId, textareaId = 'collaborativeEditor', onBroadcastPresence }) {
    this.container = document.getElementById(containerId);
    this.presenceBar = document.getElementById(barId);
    this.textarea = document.getElementById(textareaId);
    this.onBroadcastPresence = onBroadcastPresence;

    this.collaborators = new Map(); // username -> { color, lastSeen, cursorAnchor, cursorPos }
    this.myColor = this._generateColor();
    this.lastBroadcast = 0;

    // Create cursor overlay layer if editor container exists
    this.cursorLayer = null;
    this.cursorElements = new Map(); // username -> element
    this.mirrorDiv = null;
    this._initCursorLayer();
  }

  _generateColor() {
    const colors = [
      '#ef4444', '#f97316', '#f59e0b', '#10b981',
      '#06b6d4', '#3b82f6', '#6366f1', '#8b5cf6', '#ec4899'
    ];
    return colors[Math.floor(Math.random() * colors.length)];
  }

  _initCursorLayer() {
    if (!this.container || !this.textarea) return;

    // Create cursor container overlay
    this.cursorLayer = document.createElement('div');
    this.cursorLayer.className = 'remote-cursor-layer';
    this.cursorLayer.style.position = 'absolute';
    this.cursorLayer.style.top = '0';
    this.cursorLayer.style.left = '0';
    this.cursorLayer.style.width = '100%';
    this.cursorLayer.style.height = '100%';
    this.cursorLayer.style.pointerEvents = 'none';
    this.cursorLayer.style.overflow = 'hidden';
    this.cursorLayer.style.zIndex = '15';
    this.container.appendChild(this.cursorLayer);

    // Create hidden mirror div for caret position calculation
    this.mirrorDiv = document.createElement('div');
    this.mirrorDiv.style.position = 'absolute';
    this.mirrorDiv.style.visibility = 'hidden';
    this.mirrorDiv.style.pointerEvents = 'none';
    this.mirrorDiv.style.whiteSpace = 'pre-wrap';
    this.mirrorDiv.style.wordWrap = 'break-word';
    this.mirrorDiv.style.top = '0';
    this.mirrorDiv.style.left = '0';
    document.body.appendChild(this.mirrorDiv);

    // Reposition cursors on scroll or resize
    this.textarea.addEventListener('scroll', () => this._updateAllCursorPositions());
    window.addEventListener('resize', () => this._updateAllCursorPositions());
  }

  updateLocalCursor(anchorCharId, cursorPos, username) {
    const now = performance.now();
    // Throttle presence broadcasts to 100ms
    if (now - this.lastBroadcast > 100) {
      this.lastBroadcast = now;
      this.onBroadcastPresence({
        user: username,
        color: this.myColor,
        cursor_anchor: anchorCharId ? anchorCharId.toString() : null,
        cursor_pos: cursorPos
      });
    }
  }

  handleRemotePresence(data) {
    const user = data.user;
    if (!user || user === 'Anonymous') return;

    if (data.type === 'presence_leave') {
      this.collaborators.delete(user);
      this._removeCursor(user);
      this._renderPresenceBar();
      return;
    }

    this.collaborators.set(user, {
      color: data.color || '#6366f1',
      cursorAnchor: data.cursor_anchor,
      cursorPos: typeof data.cursor_pos === 'number' ? data.cursor_pos : 0,
      lastSeen: Date.now()
    });

    this._renderPresenceBar();
    this._renderCursor(user);
  }

  _renderPresenceBar() {
    if (!this.presenceBar) return;
    this.presenceBar.innerHTML = '';

    const now = Date.now();
    const activeUsers = [];

    for (const [user, info] of this.collaborators.entries()) {
      if (now - info.lastSeen > 45000) {
        this.collaborators.delete(user);
        this._removeCursor(user);
        continue;
      }
      activeUsers.push({ user, ...info });
    }

    const MAX_VISIBLE = 5;
    const visibleUsers = activeUsers.slice(0, MAX_VISIBLE);
    const overflowCount = activeUsers.length - MAX_VISIBLE;

    for (const item of visibleUsers) {
      const avatar = document.createElement('div');
      avatar.className = 'presence-avatar';
      avatar.style.backgroundColor = item.color;
      avatar.setAttribute('data-tooltip', `${item.user} (Editing)`);
      avatar.textContent = item.user.substring(0, 2).toUpperCase();
      this.presenceBar.appendChild(avatar);
    }

    if (overflowCount > 0) {
      const overflow = document.createElement('div');
      overflow.className = 'presence-overflow';
      overflow.textContent = `+${overflowCount}`;
      overflow.title = `${overflowCount} more collaborators`;
      this.presenceBar.appendChild(overflow);
    }
  }

  _getCaretCoordinates(pos) {
    if (!this.textarea || !this.mirrorDiv) return { top: 0, left: 0, height: 20 };

    const style = window.getComputedStyle(this.textarea);
    const properties = [
      'direction', 'boxSizing', 'width', 'height', 'overflowX', 'overflowY',
      'borderTopWidth', 'borderRightWidth', 'borderBottomWidth', 'borderLeftWidth',
      'borderStyle', 'paddingTop', 'paddingRight', 'paddingBottom', 'paddingLeft',
      'fontStyle', 'fontVariant', 'fontWeight', 'fontStretch', 'fontSize',
      'fontSizeAdjust', 'lineHeight', 'fontFamily', 'textAlign', 'textTransform',
      'textIndent', 'textDecoration', 'letterSpacing', 'wordSpacing', 'tabSize', 'MozTabSize'
    ];

    properties.forEach((prop) => {
      this.mirrorDiv.style[prop] = style[prop];
    });

    const text = this.textarea.value.substring(0, pos);
    this.mirrorDiv.textContent = text;

    const span = document.createElement('span');
    span.textContent = this.textarea.value.substring(pos, pos + 1) || ' ';
    this.mirrorDiv.appendChild(span);

    const spanRect = span.getBoundingClientRect();
    const mirrorRect = this.mirrorDiv.getBoundingClientRect();

    const top = spanRect.top - mirrorRect.top - this.textarea.scrollTop;
    const left = spanRect.left - mirrorRect.left - this.textarea.scrollLeft;
    const height = spanRect.height || parseInt(style.lineHeight, 10) || 20;

    return { top, left, height };
  }

  _renderCursor(username) {
    if (!this.cursorLayer || !this.textarea) return;

    const info = this.collaborators.get(username);
    if (!info) return;

    let el = this.cursorElements.get(username);
    if (!el) {
      el = document.createElement('div');
      el.className = 'remote-cursor';

      const label = document.createElement('div');
      label.className = 'remote-cursor-label';
      label.textContent = username;
      label.style.backgroundColor = info.color;
      el.appendChild(label);

      this.cursorLayer.appendChild(el);
      this.cursorElements.set(username, el);
    }

    el.style.backgroundColor = info.color;
    const label = el.querySelector('.remote-cursor-label');
    if (label) label.style.backgroundColor = info.color;

    // Resolve the collaborator's CharId anchor against our replica so their caret
    // tracks the character they are at, even after edits shifted the offsets.
    const app = typeof window !== 'undefined' ? window.App : null;
    const pos = app && app.resolveRemoteCursor
      ? app.resolveRemoteCursor(info.cursorAnchor, info.cursorPos)
      : info.cursorPos;
    const coords = this._getCaretCoordinates(pos);
    el.style.top = `${coords.top}px`;
    el.style.left = `${coords.left}px`;
    el.style.height = `${coords.height}px`;
  }

  _updateAllCursorPositions() {
    for (const [user] of this.collaborators.entries()) {
      this._renderCursor(user);
    }
  }

  _removeCursor(username) {
    const el = this.cursorElements.get(username);
    if (el && el.parentNode) {
      el.parentNode.removeChild(el);
    }
    this.cursorElements.delete(username);
  }
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { PresenceManager };
}
