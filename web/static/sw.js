/* NexusTG service worker — network-first для всего своего origin.
 * Так после деплоя никогда не показывается старый код/стили; кэш —
 * только оффлайн-фолбэк. Версию повышать при изменении логики.
 */
const CACHE = "nexustg-v5";
const PRECACHE = ["/", "/static/app.css", "/static/pico.min.css"];

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
  if (req.method !== "GET") return; // POST/HTMX-мутации — мимо
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return; // только свой origin

  // Сеть в первую очередь; кэш обновляем в фоне и используем только при оффлайне.
  e.respondWith(
    fetch(req)
      .then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {});
        return res;
      })
      .catch(() => caches.match(req))
  );
});
