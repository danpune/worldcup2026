importScripts("https://cdn.onesignal.com/sdks/web/v16/OneSignalSDK.sw.js");

/* App-shell caching layered on top of the OneSignal worker (they share the /worldcup2026/ scope).
 * Strategy:
 *   - cache-first for static assets (fonts, icons, the share-card lib) -> instant repeat loads, offline-capable
 *   - network-first for the app shell (index.html) -> visitors always get the latest UI when online;
 *     the cached copy is used only as an offline fallback (prevents 'stuck on an old version')
 *   - PASS-THROUGH (untouched) for everything cross-origin (the Worker API, OneSignal, Open-Meteo,
 *     YouTube, analytics) and the dynamic JSON feeds -> live data & push are never affected
 * Safety: the install precache tolerates any missing file, so it can never fail and take the
 * OneSignal worker (and push) down with it. */
var SHELL_CACHE = "wc2026-shell-v2";
var SHELL = [
  "index.html", "manifest.json", "apple-touch-icon.png", "icon-192.png", "icon-512.png",
  "fonts/inter-400-latin.woff2", "fonts/inter-500-latin.woff2", "fonts/inter-600-latin.woff2",
  "fonts/oswald-400-latin.woff2", "fonts/oswald-500-latin.woff2", "fonts/oswald-600-latin.woff2"
];
self.addEventListener("install", function (e) {
  self.skipWaiting();   // take over promptly so UI updates reach returning visitors without waiting for all tabs to close
  e.waitUntil(caches.open(SHELL_CACHE).then(function (c) {
    return Promise.all(SHELL.map(function (u) { return c.add(u).catch(function () {}); })); // tolerate any 404
  }));
});
self.addEventListener("activate", function (e) {
  e.waitUntil(caches.keys().then(function (ks) {
    return Promise.all(ks.map(function (k) {
      if (k !== SHELL_CACHE && k.indexOf("wc2026-") === 0) return caches.delete(k); // only ever touch our own caches
    }));
  }).then(function () { return self.clients.claim(); }));
});
self.addEventListener("fetch", function (e) {
  var req = e.request;
  if (req.method !== "GET") return;
  var url = new URL(req.url);
  if (url.origin !== self.location.origin) return;            // API / OneSignal / YouTube / Open-Meteo / analytics -> untouched
  var p = url.pathname;
  if (p.indexOf("scores.json") > -1 || p.indexOf("wc-history.json") > -1 || p.indexOf("highlights.json") > -1) return; // dynamic -> network
  if (req.mode === "navigate" || p === "/" || p.slice(-1) === "/" || p.indexOf("index.html") > -1) {
    e.respondWith(caches.open(SHELL_CACHE).then(function (c) {
      // network-first: always try the live shell, refresh the cache, and fall back to cache only when offline
      return fetch(req).then(function (r) { if (r && r.ok) c.put("index.html", r.clone()); return r; })
                       .catch(function () { return c.match("index.html"); });
    }));
    return;
  }
  e.respondWith(caches.match(req).then(function (c) {
    return c || fetch(req).then(function (r) {
      if (r && r.ok && r.type === "basic") { var cl = r.clone(); caches.open(SHELL_CACHE).then(function (cc) { cc.put(req, cl); }); }
      return r;
    });
  }));
});
