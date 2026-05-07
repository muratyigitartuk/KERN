const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "::1"]);
const CURRENT_HOST = self.location && self.location.hostname ? self.location.hostname : "";
const LOOPBACK_MODE = LOOPBACK_HOSTS.has(CURRENT_HOST);
const CACHE_NAME = "kern-shell-v10";
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
  if (LOOPBACK_MODE) {
    event.waitUntil(Promise.resolve().then(() => self.skipWaiting()));
    return;
  }
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(CORE_ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  if (LOOPBACK_MODE) {
    event.waitUntil(
      caches.keys().then((keys) => Promise.all(keys.map((key) => caches.delete(key)))).then(async () => {
        const clients = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
        await self.registration.unregister();
        await Promise.all(clients.map((client) => client.navigate(client.url)));
      })
    );
    return;
  }
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  if (LOOPBACK_MODE) {
    return;
  }
  if (event.request.method !== "GET") {
    return;
  }

  const url = new URL(event.request.url);

  // H-15: Only cache public, non-authenticated API responses (/health).
  // Authenticated /api/* responses must NOT be cached to prevent post-logout data leaks.
  if (url.pathname === "/health") {
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
              console.error("[KERN] SW health fetch failed:", err);
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

  // Skip caching for authenticated API endpoints.
  if (url.pathname.startsWith("/api/")) {
    return;
  }

  // Network-first for static assets so packaged UI updates are not pinned behind stale browser caches.
  event.respondWith(
    fetch(event.request)
      .then((response) => {
        if (response.ok && (url.pathname.startsWith("/static/") || url.pathname === "/" || url.pathname === "/dashboard")) {
          const copy = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
        }
        return response;
      })
      .catch((err) => {
        console.error("[KERN] SW fetch failed:", err);
        return caches.match(event.request).then((cached) => cached || caches.match("/dashboard"));
      })
  );
});
