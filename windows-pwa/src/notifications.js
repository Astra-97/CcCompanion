/**
 * Desktop notification gate for the PWA.  Disabled by default; enabling asks
 * for Notification permission (a user-gesture requirement in browsers), and
 * the toggle doubles as the obvious opt-out demanded by the UX contract.
 */
const STORAGE_KEY = 'cc-pwa-notifications-enabled-v1';

export function createNotifier({
  notificationApi = globalThis.Notification,
  visibility = globalThis.document,
  storage = globalThis.localStorage,
  windowLike = globalThis,
  icon = './assets/app-icon-192.png',
} = {}) {
  let enabled = false;
  try { enabled = storage?.getItem(STORAGE_KEY) === '1'; } catch { enabled = false; }
  const persist = () => { try { storage?.setItem(STORAGE_KEY, enabled ? '1' : '0'); } catch { /* best-effort */ } };
  const supported = () => Boolean(notificationApi);
  const granted = () => supported() && notificationApi.permission === 'granted';
  return {
    supported,
    isEnabled: () => enabled && granted(),
    /** Returns whether notifications are live after the toggle attempt. */
    async setEnabled(next) {
      if (!next) { enabled = false; persist(); return false; }
      if (!supported()) { enabled = false; persist(); return false; }
      let permission = notificationApi.permission;
      if (permission === 'default' && typeof notificationApi.requestPermission === 'function') {
        try { permission = await notificationApi.requestPermission(); } catch { permission = notificationApi.permission; }
      }
      enabled = permission === 'granted';
      persist();
      return enabled;
    },
    /**
     * Fire only while the page is hidden: a visible desk already shows the
     * message.  `onOpen(contactId)` focuses and switches to the contact.
     */
    notify({ contactId, title, body = '', onOpen } = {}) {
      if (!enabled || !granted()) return false;
      const hidden = visibility ? (visibility.visibilityState ? visibility.visibilityState === 'hidden' : visibility.hidden === true) : true;
      if (!hidden) return false;
      let notification;
      try {
        notification = new notificationApi(String(title || 'CcCompanion'), { body: String(body || ''), icon, tag: `cc-${contactId}` });
      } catch { return false; }
      notification.onclick = () => {
        try { windowLike.focus?.(); } catch { /* focusing is best-effort */ }
        onOpen?.(contactId);
        notification.close?.();
      };
      return true;
    },
  };
}
