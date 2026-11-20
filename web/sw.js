// Keeps the app shell available so a clear message shows even if the library server is restarting.
const SHELL = 'sg-shell-v8';
const FILES = ['/', '/ui/plat.js', '/ui/app.css', '/ui/app.js', '/ui/map.js', '/ui/terrain3d.js', '/ui/chat.js', '/ui/words.js', '/ui/icon.svg'];
self.addEventListener('install', e => e.waitUntil(caches.open(SHELL).then(c => c.addAll(FILES)).then(() => self.skipWaiting())));
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', e => {
  const u = new URL(e.request.url);
  if (e.request.method !== 'GET' || u.origin !== location.origin) return;
  if (FILES.includes(u.pathname) || u.pathname === '/api/topics') {
    // network first (the server is local and fast), cached copy if it is down
    e.respondWith(fetch(e.request).then(r => { const c = r.clone(); caches.open(SHELL).then(s => s.put(e.request, c)); return r }).catch(() => caches.match(e.request, { ignoreSearch: true })));   // the page asks for /ui/app.css?v=..: any copy of it will do
  }
});
