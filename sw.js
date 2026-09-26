// kratosNet service worker — app-shell caching + stale-while-revalidate for data
const CACHE_NAME = "kratosnet-v1";
const APP_SHELL = [
  "/",
  "/index.html",
  "/manifest.json",
  "/icon-192.png",
  "/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(APP_SHELL))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin) return; // never intercept third-party APIs (Overpass, Nominatim, Diavgeia, fonts)

  const isData = url.pathname.endsWith("/data/programs.json");

  if (isData) {
      // app shell: cache-first with normalized request and offline fallback
  event.respondWith(
    caches.match(event.request).then((cached) => {
      // 1. Return immediately if found in cache
      if (cached) return cached;
      
      // 2. Fetch from network, but add a catch for network errors
      return fetch(event.request).catch(() => {
        // 3. Fallback: If network fails and it's a navigation request (like opening the app), 
        // try to return the cached index.html
        if (event.request.mode === 'navigate' || url.pathname === '/' || url.pathname === '/index.html') {
          return caches.match('./index.html').then(fallback => {
              // If index.html isn't specifically matched, try the root '/'
              return fallback || caches.match('./');
          });
        }
        // If it's not navigation and no cache, let it fail gracefully rather than crashing the SW
      });
    })
  );
