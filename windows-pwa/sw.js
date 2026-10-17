const CACHE = 'cccompanion-desk-v20';
const ASSETS = ['./', './index.html', './manifest.webmanifest', './src/styles.css?v=20', './src/api.js?v=20', './src/bootstrap.js?v=20', './src/composer-state.js?v=20', './src/data.js?v=20', './src/pairing-code.js?v=20', './src/live-messages.js?v=20', './src/clipboard-images.js?v=20', './src/sticker-protocol.js?v=20', './src/markdown.js?v=20', './src/unread.js?v=20', './src/notifications.js?v=20', './src/memory-calendar.js?v=20', './src/home-dashboard.js?v=20', './src/app.js?v=20', './assets/cc-mark.svg', './assets/icon-192.png', './assets/icon-512.png'];
self.addEventListener('activate', (event) => event.waitUntil(
  caches.keys()
    .then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))))
    .then(() => self.clients.claim()),
));
self.addEventListener('install', (event) => event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(ASSETS)).then(() => self.skipWaiting())));
self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET') return;
  const url = new URL(event.request.url);
  const isShellAsset = ASSETS.some((asset) => new URL(asset, self.location.href).pathname === url.pathname);
  // /web/pwa/ is a static shell path, but session and all data routes are
  // private.  Never even query Cache Storage for those requests.
  const isPrivateRoute = /^\/(?:web\/session|chat|memory|attachments|stickers)(?:\/|$)/.test(url.pathname);
  if (url.origin !== self.location.origin || !isShellAsset || isPrivateRoute) return;
  event.respondWith(caches.match(event.request).then((cached) => cached || fetch(event.request)));
});
