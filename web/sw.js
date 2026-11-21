// Keeps the app shell available so a clear message shows even if the library server is restarting.
const SHELL = 'sg-shell-v13';
const FILES = ['/', '/ui/plat.js', '/ui/app.css', '/ui/app.js', '/ui/map.js', '/ui/terrain3d.js', '/ui/chat.js', '/ui/words.js', '/ui/update.js', '/ui/video-moments.js', '/ui/icon.svg',
  '/ui/i18n-core.js', '/ui/i18n-dom.js', '/ui/i18n-en.js', '/ui/i18n-locales.js', '/ui/i18n-app.js'];
self.addEventListener('install', e => e.waitUntil(caches.open(SHELL).then(c => c.addAll(FILES)).then(() => self.skipWaiting())));
self.addEventListener('activate', e => e.waitUntil((async () => {
  for (const name of await caches.keys()) {
    if (name !== SHELL && /^sg-shell-v\d+$/.test(name)) await caches.delete(name);
  }
  await self.clients.claim();
})()));
self.addEventListener('fetch', e => {
  const u = new URL(e.request.url);
  if (e.request.method !== 'GET' || u.origin !== location.origin) return;
  if (FILES.includes(u.pathname) || u.pathname === '/api/topics') {
    // network first (the server is local and fast), cached copy if it is down
    e.respondWith((async () => {
      const cache = await caches.open(SHELL);
      const key = u.pathname;  // HTML adds cache-busting query strings to scripts and CSS.
      try {
        const response = await fetch(e.request);
        if (response.ok) await cache.put(key, response.clone()).catch(() => {});
        return response;
      } catch { return (await cache.match(key)) || Response.error(); }
    })());
  }
});
