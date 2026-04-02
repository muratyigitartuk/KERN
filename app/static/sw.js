const CACHE_NAME = "kern-shell-v4";
const CORE_ASSETS = [
  "/",
  "/dashboard",
  "/static/dashboard.css",
  "/static/app.js",
  "/static/js/dashboard-dom.js",
  "/static/js/dashboard-events.js",
  "/static/js/dashboard-renderer.js",
  "/static/js/knowledge-graph.js",
  "/static/js/i18n.js",
  "/static/js/modal-controller.js",
  "/static/js/socket-client.js",
  "/static/js/theme-controller.js",
  "/static/js/utils.js",
  "/static/locales/en.json",
  "/static/locales/de.json",
  "/static/manifest.webmanifest",
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
      Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") {
    return;
  }

  const url = new URL(event.request.url);

  // Stale-while-revalidate for API responses
  if (url.pathname.startsWith("/api/") || url.pathname === "/health") {
    event.respondWith(
      caches.open(CACHE_NAME).then((cache) =>
        cache.match(event.request).then((cached) => {
          const networkFetch = fetch(event.request)
            .then((response) => {
              if (response.ok) {
                cache.put(event.request, response.clone());
              }
              return response;
            })
            .catch((err) => {
              console.error("[KERN] SW API fetch failed:", err);
              return cached || new Response(JSON.stringify({ error: "offline" }), {
                status: 503,
                headers: { "Content-Type": "application/json" },
              });
            });
          return cached || networkFetch;
        })
      )
    );
    return;
  }

  // Cache-first for static assets
  event.respondWith(
    caches.match(event.request).then((cached) => {
      if (cached) {
        return cached;
      }
      return fetch(event.request).then((response) => {
        if (response.ok && url.pathname.startsWith("/static/")) {
          const copy = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
        }
        return response;
      });
    }).catch((err) => { console.error("[KERN] SW fetch failed:", err); return caches.match("/dashboard"); })
  );
});
