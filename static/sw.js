const CACHE = 'intentions-v3';
const OFFLINE_DISPLAY = '/static/display.html';

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE)
      .then(cache => cache.add(OFFLINE_DISPLAY))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(
        keys.filter(key => key !== CACHE).map(key => caches.delete(key))
      ))
      .then(() => self.clients.claim())
  );
});

async function networkFirst(request) {
  const cache = await caches.open(CACHE);
  try {
    const response = await fetch(request, {cache: 'no-store'});
    if (response && response.ok && request.method === 'GET') {
      await cache.put(request, response.clone());
    }
    return response;
  } catch (error) {
    return (await cache.match(request)) ||
      (await cache.match(OFFLINE_DISPLAY)) ||
      new Response('Offline', {status: 503});
  }
}

self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin || event.request.method !== 'GET') return;

  // API data should never be cached by the service worker.
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/auth/')) {
    event.respondWith(fetch(event.request));
    return;
  }

  if (url.pathname === '/favicon.ico') return;

  // Always ask the server for application code first. Cached HTML is only an
  // offline fallback, preventing old deployments from persisting in browsers.
  if (
    event.request.mode === 'navigate' ||
    event.request.destination === 'document' ||
    url.pathname.endsWith('.html') ||
    url.pathname === '/dashboard' ||
    url.pathname.endsWith('/display')
  ) {
    event.respondWith(networkFirst(event.request));
  }
});
