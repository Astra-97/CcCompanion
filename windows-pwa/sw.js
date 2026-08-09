const CACHE = 'cccompanion-desk-v1';
const ASSETS = ['./', './index.html', './manifest.webmanifest', './src/styles.css', './src/api.js', './src/bootstrap.js', './src/data.js', './src/app.js', './assets/cc-mark.svg', './assets/icon-192.png', './assets/icon-512.png'];
self.addEventListener('install', (event) => event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(ASSETS))));
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));
self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET') return;
  event.respondWith(caches.match(event.request).then((cached) => cached || fetch(event.request)));
});
