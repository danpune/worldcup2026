/*
 * wc2026-api — Cloudflare Worker (live World Cup scores proxy)
 * ------------------------------------------------------------
 * Backup/documentation copy of the Worker deployed at:
 *   https://wc2026-api.<subdomain>.workers.dev
 *
 * It proxies the licensed API-Football feed, caches ~15s, and exposes:
 *   GET /scores  -> all World Cup 2026 fixtures (live + finished + scheduled), clean JSON
 *   GET /status  -> account/key check (subscription, requests used)
 *   GET /        -> health check
 *
 * SETUP (Cloudflare dashboard, no CLI needed):
 *   1. Workers & Pages -> Create -> Worker -> name "wc2026-api".
 *   2. Settings -> Variables and Secrets -> add an ENCRYPTED secret:
 *        Name:  APISPORTS_KEY
 *        Value: your API-Football (api-sports.io) key
 *   3. Edit code -> paste this file -> Deploy.
 *
 * The API key is NEVER stored here — it lives only as the Cloudflare secret
 * APISPORTS_KEY, read at runtime via env.APISPORTS_KEY.
 *
 * The site (index.html) polls /scores and maps fixtures to its own match
 * numbers by team pair, with these two name aliases:
 *   "Cape Verde Islands" -> "Cape Verde",  "Congo DR" -> "DR Congo".
 */
var WC_LEAGUE = 1;        // FIFA World Cup (API-Football league id)
var SEASON = 2026;
var API = "https://v3.football.api-sports.io";

export default {
  async fetch(request, env, ctx) {
    var url = new URL(request.url);
    var headers = { "x-apisports-key": env.APISPORTS_KEY };
    if (request.method === "OPTIONS") return new Response(null, { headers: cors() });

    if (url.pathname === "/status") {
      var s = await fetch(API + "/status", { headers });
      return json(await s.json());
    }

    if (url.pathname === "/scores") {
      var cache = caches.default;
      var cacheKey = new Request(new URL("/scores", url.origin).toString());
      var hit = await cache.match(cacheKey);
      if (hit) return hit;
      var matches = [], errors = null;
      try {
        var r = await fetch(API + "/fixtures?league=" + WC_LEAGUE + "&season=" + SEASON, { headers });
        var data = await r.json();
        if (data.errors && Object.keys(data.errors).length) errors = data.errors;
        matches = (data.response || []).map(function (x) {
          return { id: x.fixture.id, home: x.teams.home.name, away: x.teams.away.name, h: x.goals.home, a: x.goals.away, status: x.fixture.status.short, minute: x.fixture.status.elapsed };
        });
      } catch (e) { errors = String(e); }
      var body = JSON.stringify({ updated: new Date().toISOString(), count: matches.length, matches: matches, errors: errors }, null, 2);
      var res = new Response(body, { headers: Object.assign({}, cors(), { "content-type": "application/json", "cache-control": "max-age=15" }) });
      ctx.waitUntil(cache.put(cacheKey, res.clone()));
      return res;
    }

    return new Response("wc2026-api ok", { status: 200, headers: cors() });
  }
};

function cors() {
  return { "access-control-allow-origin": "*", "access-control-allow-methods": "GET, OPTIONS", "access-control-allow-headers": "*" };
}
function json(obj) {
  return new Response(JSON.stringify(obj, null, 2), { headers: Object.assign({}, cors(), { "content-type": "application/json" }) });
}
