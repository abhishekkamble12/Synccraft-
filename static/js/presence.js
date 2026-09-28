/**
 * Live Presence and Remote Cursor Manager.
 */

class PresenceManager {
  constructor({ containerId, barId, onBroadcastPresence }) {
    this.container = document.getElementById(containerId);
    this.presenceBar = document.getElementById(barId);
    this.onBroadcastPresence = onBroadcastPresence;

    this.collaborators = new Map(); // username -> { color, lastSeen, cursorAnchor, cursorIndex }
    this.myColor = this._generateColor();
    this.lastBroadcast = 0;
  }

  _generateColor() {
    const colors = [
      '#ef4444', '#f97316', '#f59e0b', '#10b981',
      '#06b6d4', '#3b82f6', '#6366f1', '#8b5cf6', '#ec4899'
    ];
    return colors[Math.floor(Math.random() * colors.length)];
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
      this._renderPresenceBar();
      return;
    }

    this.collaborators.set(user, {
      color: data.color || '#6366f1',
      cursorAnchor: data.cursor_anchor,
      cursorPos: data.cursor_pos,
      lastSeen: Date.now()
    });

    this._renderPresenceBar();
  }

  _renderPresenceBar() {
    if (!this.presenceBar) return;
    this.presenceBar.innerHTML = '';

    const now = Date.now();
    for (const [user, info] of this.collaborators.entries()) {
      // Drop stale users after 45s without heartbeat
      if (now - info.lastSeen > 45000) {
        this.collaborators.delete(user);
        continue;
      }

      const avatar = document.createElement('div');
      avatar.className = 'presence-avatar';
      avatar.style.backgroundColor = info.color;
      avatar.setAttribute('data-tooltip', `${user} (Online)`);
      avatar.textContent = user.substring(0, 2).toUpperCase();
      this.presenceBar.appendChild(avatar);
    }
  }
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { PresenceManager };
}
