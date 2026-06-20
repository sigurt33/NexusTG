/* NexusTG service worker — минимальный, network-first для навигации.
 * Главное: НЕ кэшировать HTML, чтобы после деплоя не показывать старый код.
 * Версию повышать при изменении стратегии (старые кэши чистятся в activate).
 */
const CACHE = "nexustg-v3";
const PRECACHE = ["/static/app.css", "/static/pico.min.css"];

self.addEventListener("install", (e) => {
  self.skipWaiting();
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(PRECACHE).catch(() => {})));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return; // POST/HTMX-мутации — мимо кэша
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return; // только свой origin

  // Навигация (HTML) — всегда из сети; при оффлайне отдаём что есть в кэше.
  if (req.mode === "navigate") {
    e.respondWith(fetch(req).catch(() => caches.match(req)));
    return;
  }

  // Статика — stale-while-revalidate: мгновенно из кэша, в фоне обновляем.
  if (url.pathname.startsWith("/static/")) {
    e.respondWith(
      caches.match(req).then((cached) => {
        const net = fetch(req)
          .then((res) => {
            caches.open(CACHE).then((c) => c.put(req, res.clone()));
            return res;
          })
          .catch(() => cached);
        return cached || net;
      })
    );
  }
});
