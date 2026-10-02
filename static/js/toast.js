/**
 * Toast Notification System for Monach Sync Engine.
 * Replaces all alert()/confirm() calls with beautiful non-blocking notifications.
 */

class ToastManager {
  constructor() {
    this.container = document.getElementById('toastContainer');
    if (!this.container) {
      this.container = document.createElement('div');
      this.container.id = 'toastContainer';
      this.container.className = 'toast-container';
      document.body.appendChild(this.container);
    }
    this.toasts = [];
    this.maxToasts = 5;
  }

  /**
   * Show a toast notification.
   * @param {Object} opts
   * @param {string} opts.title - Bold title text
   * @param {string} [opts.message] - Description text
   * @param {'success'|'error'|'warning'|'info'} [opts.type='info'] - Visual style
   * @param {number} [opts.duration=4000] - Auto-dismiss in ms (0 = manual close)
   * @param {boolean} [opts.progress=true] - Show countdown progress bar
   * @returns {HTMLElement} The toast element
   */
  show({ title, message = '', type = 'info', duration = 4000, progress = true }) {
    const icons = {
      success: '✓',
      error: '✕',
      warning: '⚠',
      info: 'ℹ'
    };

    const toast = document.createElement('div');
    toast.className = `toast toast--${type}`;
    toast.innerHTML = `
      <span class="toast-icon">${icons[type] || icons.info}</span>
      <div class="toast-content">
        <div class="toast-title">${this._escapeHtml(title)}</div>
        ${message ? `<div class="toast-message">${this._escapeHtml(message)}</div>` : ''}
      </div>
      <button class="toast-close" aria-label="Close">&times;</button>
      ${progress && duration > 0 ? `<div class="toast-progress" style="width: 100%; transition: width ${duration}ms linear;"></div>` : ''}
    `;

    // Close button
    toast.querySelector('.toast-close').addEventListener('click', () => {
      this._dismiss(toast);
    });

    // Remove oldest if at capacity
    while (this.toasts.length >= this.maxToasts) {
      this._dismiss(this.toasts[0]);
    }

    this.container.appendChild(toast);
    this.toasts.push(toast);

    // Start progress bar countdown
    if (progress && duration > 0) {
      const progressBar = toast.querySelector('.toast-progress');
      if (progressBar) {
        requestAnimationFrame(() => {
          progressBar.style.width = '0%';
        });
      }
    }

    // Auto dismiss
    if (duration > 0) {
      toast._dismissTimer = setTimeout(() => {
        this._dismiss(toast);
      }, duration);
    }

    return toast;
  }

  success(title, message) {
    return this.show({ title, message, type: 'success' });
  }

  error(title, message) {
    return this.show({ title, message, type: 'error', duration: 6000 });
  }

  warning(title, message) {
    return this.show({ title, message, type: 'warning', duration: 5000 });
  }

  info(title, message) {
    return this.show({ title, message, type: 'info' });
  }

  /**
   * Show a confirmation toast with Accept/Cancel actions.
   * @param {string} title
   * @param {string} message
   * @returns {Promise<boolean>} Resolves true on accept, false on cancel
   */
  confirm(title, message) {
    return new Promise((resolve) => {
      const toast = this.show({
        title,
        message,
        type: 'warning',
        duration: 0,
        progress: false
      });

      // Replace close button with action buttons
      const closeBtn = toast.querySelector('.toast-close');
      if (closeBtn) closeBtn.remove();

      const actions = document.createElement('div');
      actions.style.cssText = 'display: flex; gap: 0.5rem; margin-top: 0.5rem;';
      actions.innerHTML = `
        <button class="btn btn-sm btn-secondary toast-cancel-btn">Cancel</button>
        <button class="btn btn-sm btn-danger toast-confirm-btn">Confirm</button>
      `;

      toast.querySelector('.toast-content').appendChild(actions);

      actions.querySelector('.toast-confirm-btn').addEventListener('click', () => {
        this._dismiss(toast);
        resolve(true);
      });

      actions.querySelector('.toast-cancel-btn').addEventListener('click', () => {
        this._dismiss(toast);
        resolve(false);
      });
    });
  }

  _dismiss(toast) {
    if (!toast || !toast.parentElement) return;
    if (toast._dismissTimer) clearTimeout(toast._dismissTimer);

    toast.classList.add('toast--exiting');
    toast.addEventListener('animationend', () => {
      if (toast.parentElement) toast.remove();
      const idx = this.toasts.indexOf(toast);
      if (idx !== -1) this.toasts.splice(idx, 1);
    }, { once: true });
  }

  _escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
  }
}

// Singleton instance
const Toast = new ToastManager();

if (typeof window !== 'undefined') {
  window.Toast = Toast;
}
