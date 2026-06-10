const CACHE_NAME = "businessbuilder-ai-v1";
const CORE_ASSETS = [
  "/landing",
  "/pricing",
  "/static/style.css",
  "/static/logo.png",
  "/static/icon-192.png",
  "/static/icon-512.png",
  "/static/favicon.png",
  "/static/social-preview.png",
  "/static/offline.html",
  "/static/manifest.json"
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(CORE_ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.map((key) => key !== CACHE_NAME ? caches.delete(key) : null))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);

  if (request.method !== "GET" || url.origin !== self.location.origin) {
    return;
  }

  const isStatic = url.pathname.startsWith("/static/");
  const isPublicPage = ["/landing", "/pricing"].includes(url.pathname);

  if (!isStatic && !isPublicPage) {
    return;
  }

  event.respondWith(
    caches.match(request).then((cached) => {
      if (cached) {
        return cached;
      }

      return fetch(request)
        .then((response) => {
          if (!response || response.status !== 200 || response.type !== "basic") {
            return response;
          }

          const responseToCache = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(request, responseToCache));
          return response;
        })
        .catch(() => {
          if (request.mode === "navigate") {
            return caches.match("/static/offline.html");
          }
          return caches.match("/static/offline.html");
        });
    })
  );
});
