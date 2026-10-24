const CACHE = 'cccompanion-desk-v33';
const ASSETS = ['./', './index.html', './manifest.webmanifest', './src/styles.css?v=33', './src/api.js?v=33', './src/bootstrap.js?v=33', './src/composer-state.js?v=33', './src/data.js?v=33', './src/pairing-code.js?v=33', './src/live-messages.js?v=33', './src/clipboard-images.js?v=33', './src/sticker-protocol.js?v=33', './src/markdown.js?v=33', './src/unread.js?v=33', './src/notifications.js?v=33', './src/memory-calendar.js?v=33', './src/home-dashboard.js?v=33', './src/app.js?v=33', './assets/favicon-32.png', './assets/apple-touch-icon.png', './assets/app-icon-192.png', './assets/app-icon-512.png', './assets/app-icon-maskable-192.png', './assets/app-icon-maskable-512.png', './assets/default_avatar_xiaoke.webp', './assets/default_avatar_kairos.jpg', './assets/default_avatar_ai.jpg', './assets/default_avatar_hakimi.jpg', './assets/default_avatar_group.jpg', './assets/default_avatar_toolbot.jpg', './assets/default_avatar_user.jpg'];
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
