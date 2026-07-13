/*
 * wc2026-api — Cloudflare Worker (live scores + match stats + goal-alert detector)
 * -----------------------------------------------------------------------------
 * SOURCE OF TRUTH for the deployed Worker — .github/workflows/deploy-worker.yml deploys this exact file on push.
 *
 * HTTP routes (fetch handler):
 *   GET /scores        -> all WC2026 fixtures (live+finished+scheduled), read from KV (cron-written)
 *   GET /match?id=     -> {stats, events, lineups} for one fixture, read from KV (cron-written; lazy-fetch on miss)
 *
 * DECOUPLED READS: the once-a-minute cron is the ONLY thing that calls API-Football for scores/match data.
 * It writes a `scores` snapshot and `match:<fid>` detail into KV; the public /scores and /match routes read
 * from KV, so visitor traffic never hits the upstream API (no per-minute rate-limit risk at any scale).
 * Live data is therefore at most ~60s old (the cron cadence).
 *                         stats = possession/shots/xG…; events = goal/card/sub timeline;
 *                         lineups = starting XI + formation per team
 *   GET /status        -> API-Football account/key check
 *   GET /testpush[?team=X] -> send a test push (admin; Authorization: Bearer <ADMIN_KEY>)
 *   GET /run           -> run the alert detector once (admin; first call seeds silently)
 *   GET /reset         -> clear the detector's KV memory (admin; re-seeds next run)
 *   GET /              -> health check
 *   (/testpush, /run, /reset require "Authorization: Bearer <ADMIN_KEY>"; they fail closed with 403 otherwise.)
 *
 * Scheduled handler: a Cron trigger ("* * * * *", every minute) runs the detector,
 * which sends OneSignal pushes for kickoff / goals / cards / subs / VAR / half-time /
 * full-time, targeted to subscribers who follow either team (or all_matches).
 * Alert tiers: "core" events (kickoff, goals, full-time) go to everyone following the
 * team; "extra" events (cards, subs, VAR, half-time) only reach subscribers whose
 * alerts="all" tag is set (the site's "Goals only" vs "Everything" preference).
 *
 * Cloudflare setup:
 *   - Secret  APISPORTS_KEY        = API-Football (api-sports.io) key
 *   - Secret  ONESIGNAL_REST_KEY   = OneSignal REST API Key (legacy format -> "Basic")
 *   - Secret  ADMIN_KEY            = shared secret guarding /testpush /run /reset
 *   - KV binding  STATE            = stores detector `state`, the `scores` snapshot, and `match:<fid>` detail
 *   - Workers Paid plan recommended (the cron writes KV every minute — above the free KV write limit)
 *   - Cron trigger  * * * * *
 * No secrets are stored in this file.
 */
var WC_LEAGUE = 1, SEASON = 2026;
var API = "https://v3.football.api-sports.io";
var OS_APP = "ac62fcac-4bee-4703-a067-8cf227bd1e92";
var LIVE_S = ["1H", "2H", "ET", "BT", "P", "LIVE", "INT"], FINAL_S = ["FT", "AET", "PEN"];
// API-Football names that differ from the site's canonical names — must mirror fetch_scores.py's ALIAS
// so the team_<tag> we target matches the team_<tag> the front-end subscribes the user to.
var TEAM_ALIAS = { "Cape Verde Islands": "Cape Verde", "Congo DR": "DR Congo" };
function teamTag(name) { return tagKey(TEAM_ALIAS[name] || name); }

// Valid WC2026 fixture ids come from the cron-written scores snapshot. Used to reject
// /match?id=<random>, which would otherwise be an unauthenticated path to the metered API.
async function knownFixtureIds(env) {
  try { var ps = JSON.parse(await env.STATE.get("scores")); if (ps && ps.matches && ps.matches.length) return new Set(ps.matches.map(function (m) { return String(m.id); })); } catch (e) {}
  return null;   // snapshot unavailable (cold start) -> fail open so legit stats never break
}

export default {
  async fetch(request, env, ctx) {
    var url = new URL(request.url);
    var headers = { "x-apisports-key": env.APISPORTS_KEY };
    if (request.method === "OPTIONS") return new Response(null, { headers: cors() });

    if (url.pathname === "/status") {
      var s = await fetch(API + "/status", { headers });
      var body = await s.json();
      // Strip account PII (name/email) — this route is public; never expose it. Keep subscription + quota only.
      if (body && body.response && body.response.account) delete body.response.account;
      // Surface API-Football's rate-limit headers so the per-minute ceiling is visible (it's not in the body).
      body.rateLimit = {
        perMinuteLimit: s.headers.get("x-ratelimit-limit"),
        perMinuteRemaining: s.headers.get("x-ratelimit-remaining"),
        dailyLimit: s.headers.get("x-ratelimit-requests-limit"),
        dailyRemaining: s.headers.get("x-ratelimit-requests-remaining")
      };
      return json(body);
    }

    if (url.pathname === "/match") {
      var id = url.searchParams.get("id"); if (!id) return json({ error: "missing id" });
      var known = await knownFixtureIds(env);   // block random ids from reaching the paid API (negative results aren't cached, so each miss = 4 upstream calls)
      if (known && !known.has(String(id))) return new Response(JSON.stringify({ error: "unknown fixture" }), { status: 404, headers: Object.assign({}, cors(), { "content-type": "application/json", "cache-control": "max-age=3600" }) });
      // Decoupled: live & finished matches are written to KV by the cron — read those, no API call.
      var kvMatch = await env.STATE.get("match:" + id);
      var refreshThin = false;
      if (kvMatch) {
        try {
          var pm0 = JSON.parse(kvMatch);
          // "thin" = no event timeline (goals/cards/subs) — the most fan-visible part, and what the provider
          // often posts AFTER full-time. The cron stops refreshing a match once it ends, so a payload can freeze
          // with an empty timeline. Once it's a bit old, refresh so the cache catches up. Throttled by 'updated'.
          var thin0 = !(pm0.events && pm0.events.length);
          var age0 = pm0.updated ? (Date.now() - Date.parse(pm0.updated)) : Infinity;
          refreshThin = thin0 && age0 > 5 * 60000;
        } catch (e) {}
        if (!refreshThin) return new Response(kvMatch, { headers: Object.assign({}, cors(), { "content-type": "application/json", "cache-control": "max-age=20" }) });
      }
      // KV miss, or a stale+thin payload to refresh — fetch fresh from the API.
      var mc = caches.default, mk = new Request(new URL("/match?id=" + id, url.origin).toString());
      if (!refreshThin) { var mh = await mc.match(mk); if (mh) return mh; }   // edge cache only on a true miss (skip on refresh, else it re-serves the stale copy)
      var stats = [], events = [], lineups = [], referee = null, venue = null, me = null;
      try {
        var r = await Promise.all([
          fetch(API + "/fixtures/statistics?fixture=" + id, { headers }).then(function (x) { return x.json(); }),
          fetch(API + "/fixtures/events?fixture=" + id, { headers }).then(function (x) { return x.json(); }),
          fetch(API + "/fixtures/lineups?fixture=" + id, { headers }).then(function (x) { return x.json(); }),
          fetch(API + "/fixtures?id=" + id, { headers }).then(function (x) { return x.json(); })   // referee/venue: reliable even when statistics aren't
        ]);
        stats = r[0].response || []; events = r[1].response || []; lineups = r[2].response || [];
        var fx = (r[3].response && r[3].response[0] && r[3].response[0].fixture) || {};
        referee = fx.referee || null; venue = (fx.venue && fx.venue.name) || null;
        if (r[0].errors && Object.keys(r[0].errors).length) me = r[0].errors;
      } catch (e) { me = String(e); }
      var payloadM = { id: id, stats: stats, events: events, lineups: lineups, referee: referee, venue: venue, errors: me, updated: new Date().toISOString() };
      // keep-best: a flapped-empty fetch must not downgrade the goals/stats we already cached for this match
      if (!me && typeof pm0 !== "undefined" && pm0) payloadM = bestMatch(pm0, payloadM);
      var mr = new Response(JSON.stringify(payloadM, null, 2), { headers: Object.assign({}, cors(), { "content-type": "application/json", "cache-control": me ? "max-age=0" : "max-age=30" }) });
      if (!me && (refreshThin || payloadM.events.length || payloadM.stats.length || payloadM.lineups.length)) {   // on a refresh, always rewrite so 'updated' resets the 5-min throttle even if still thin
        ctx.waitUntil(mc.put(mk, mr.clone()));
        ctx.waitUntil(env.STATE.put("match:" + id, JSON.stringify(payloadM), { expirationTtl: 86400 }));  // serve from KV next time
      }
      return mr;
    }

    if (url.pathname === "/scores") {
      // Decoupled: serve the snapshot the cron wrote to KV — visitor traffic never calls API-Football.
      var kvScores = await env.STATE.get("scores");
      if (kvScores) return new Response(kvScores, { headers: Object.assign({}, cors(), { "content-type": "application/json", "cache-control": "max-age=15" }) });
      // KV not warmed yet (first minute after deploy) — fall back to a direct fetch this once.
      var c = caches.default;
      var ck = new Request(new URL("/scores", url.origin).toString());          // primary, short TTL
      var lk = new Request(new URL("/scores_lastgood", url.origin).toString());  // last-good, long TTL
      var hit = await c.match(ck); if (hit) return hit;
      function scoresRes(obj, maxAge) { return new Response(JSON.stringify(obj, null, 2), { headers: Object.assign({}, cors(), { "content-type": "application/json", "cache-control": "max-age=" + maxAge }) }); }
      var matches = [], errors = null;
      try {
        var data = await (await fetch(API + "/fixtures?league=" + WC_LEAGUE + "&season=" + SEASON, { headers })).json();
        if (data.errors && Object.keys(data.errors).length) errors = data.errors;
        matches = (data.response || []).map(function (x) { return { id: x.fixture.id, home: x.teams.home.name, away: x.teams.away.name, h: x.goals.home, a: x.goals.away, status: x.fixture.status.short, minute: x.fixture.status.elapsed, w: x.teams.home.winner === true ? "h" : (x.teams.away.winner === true ? "a" : null) }; });
      } catch (e) { errors = String(e); }
      if (matches.length && !errors) {
        // Good data: serve it (cache 30s) and refresh the long-lived last-good copy (10 min).
        var payload = { updated: new Date().toISOString(), count: matches.length, matches: matches, errors: null };
        var res = scoresRes(payload, 30);
        ctx.waitUntil(c.put(ck, res.clone()));
        ctx.waitUntil(c.put(lk, scoresRes(payload, 600)));
        return res;
      }
      // API rate-limited / errored / empty: serve the last good scores so the live layer doesn't blank out
      // to the delayed feed. Cache the stale copy briefly to throttle retries while the limit clears.
      var lastGood = await c.match(lk);
      if (lastGood) {
        var lg = await lastGood.json();
        lg.stale = true;
        var sres = scoresRes(lg, 10);
        ctx.waitUntil(c.put(ck, sres.clone()));
        return sres;
      }
      return scoresRes({ updated: new Date().toISOString(), count: matches.length, matches: matches, errors: errors }, 0);  // nothing cached to fall back to
    }

    // Client error beacon: the site posts anonymous JS-error info here for triage. We log ONLY the error
    // text + source location + browser — no IP, no identifiers, nothing persisted beyond Cloudflare's logs.
    if (url.pathname === "/log") {
      if (request.method !== "POST") return new Response(null, { status: 405, headers: cors() });
      if ((request.headers.get("origin") || "").indexOf("danpune.github.io") < 0) return new Response(null, { status: 204, headers: cors() });   // ignore off-site beacons (log-spam guard)
      try {
        var e = JSON.parse((await request.text()).slice(0, 2000));
        console.log("wc2026 client-error: " + JSON.stringify({
          m: String(e.m || "").slice(0, 300), s: String(e.s || "").slice(0, 200),
          l: e.l, c: e.c, p: String(e.p || "").slice(0, 120),
          ua: (request.headers.get("user-agent") || "").slice(0, 200)
        }));
      } catch (_) {}
      return new Response(null, { status: 204, headers: cors() });
    }

    // World Cup news headlines — fetched server-side from Google News RSS (free, no key).
    // Stale-while-revalidate: cache hit = instant; cache miss = serve stale KV immediately while
    // background-refreshing via ctx.waitUntil (user never waits for the 8-9s RSS fetch on a miss).
    if (url.pathname === "/news") {
      var newc = caches.default, newk = new Request(new URL("/news", url.origin).toString());
      var newh = await newc.match(newk); if (newh) return newh;
      // Cache miss: fetch stale from KV while triggering background refresh
      var lastNews = await env.STATE.get("news:last");
      var doRefreshNews = async function() {
        var nitems = [];
        try {
          var rss = await (await fetch("https://news.google.com/rss/search?q=" + encodeURIComponent("FIFA World Cup 2026") + "&hl=en-US&gl=US&ceid=US:en",
            { headers: { "User-Agent": "Mozilla/5.0 (compatible; wc2026-news/1.0)" } })).text();
          var parts = rss.split("<item>").slice(1);
          for (var i = 0; i < parts.length && nitems.length < 24; i++) {
            var b = parts[i];
            var title = decodeEntities(stripCdata((b.match(/<title>([\s\S]*?)<\/title>/) || [])[1] || "")).trim();
            var link = stripCdata((b.match(/<link>([\s\S]*?)<\/link>/) || [])[1] || "").trim();
            var src = decodeEntities(stripCdata((b.match(/<source[^>]*>([\s\S]*?)<\/source>/) || [])[1] || "")).trim();
            var pub = ((b.match(/<pubDate>([\s\S]*?)<\/pubDate>/) || [])[1] || "").trim();
            var img = ((b.match(/<media:thumbnail[^>]+url="([^"]+)"/) || [])[1] || (b.match(/<media:content[^>]+url="([^"]+)"/) || [])[1] || "").trim();
            if (src && title.length > src.length + 3 && title.slice(-(src.length + 3)) === " - " + src) title = title.slice(0, -(src.length + 3)).trim();
            if (title && /^https?:\/\//i.test(link)) nitems.push({ title: title, link: link, source: src, pub: pub, img: img || null });
          }
        } catch (e) {}
        if (nitems.length) {
          var good = JSON.stringify({ updated: new Date().toISOString(), count: nitems.length, items: nitems, errors: null }, null, 2);
          await env.STATE.put("news:last", good, { expirationTtl: 86400 });
          var newr = new Response(good, { headers: Object.assign({}, cors(), { "content-type": "application/json", "cache-control": "max-age=900" }) });
          await newc.put(newk, newr.clone());
          return newr;
        }
        return null;
      };
      if (lastNews) {
        ctx.waitUntil(doRefreshNews());  // background refresh, user gets stale immediately
        return new Response(lastNews, { headers: Object.assign({}, cors(), { "content-type": "application/json", "cache-control": "max-age=60" }) });
      }
      // First boot: no stale data yet — must wait for the live fetch this once
      var freshResp = await doRefreshNews();
      if (freshResp) return freshResp;
      return new Response(JSON.stringify({ updated: new Date().toISOString(), count: 0, items: [], errors: "no items" }, null, 2),
        { headers: Object.assign({}, cors(), { "content-type": "application/json", "cache-control": "max-age=0" }) });
    }

    // Golden Boot race — top scorers for the tournament. Slow-changing, so edge-cached 30 min with a KV
    // last-good fallback (so it never blanks if the API is briefly rate-limited). Slim payload for the client.
    if (url.pathname === "/topscorers") {
      var tsc = caches.default, tsk = new Request(new URL("/topscorers", url.origin).toString());
      var tsh = await tsc.match(tsk); if (tsh) return tsh;
      var scorers = [], tse = null;
      try {
        var tsd = await (await fetch(API + "/players/topscorers?league=" + WC_LEAGUE + "&season=" + SEASON, { headers })).json();
        scorers = (tsd.response || []).slice(0, 20).map(function (row) {
          var pl = row.player || {}, st = (row.statistics && row.statistics[0]) || {};
          return {
            name: pl.name || "", team: (st.team && st.team.name) || "", nat: pl.nationality || "",
            goals: (st.goals && st.goals.total) || 0, assists: (st.goals && st.goals.assists) || 0,
            pens: (st.penalty && st.penalty.scored) || 0, apps: (st.games && st.games.appearences) || 0
          };
        }).filter(function (s) { return s.goals > 0; });
        if (tsd.errors && Object.keys(tsd.errors).length) tse = tsd.errors;
      } catch (e) { tse = String(e); }
      if (scorers.length) {
        var tgood = JSON.stringify({ updated: new Date().toISOString(), count: scorers.length, scorers: scorers, errors: null }, null, 2);
        ctx.waitUntil(env.STATE.put("topscorers:last", tgood, { expirationTtl: 86400 }));
        var tsr = new Response(tgood, { headers: Object.assign({}, cors(), { "content-type": "application/json", "cache-control": "max-age=1800" }) }); // 30 min
        ctx.waitUntil(tsc.put(tsk, tsr.clone()));
        return tsr;
      }
      var lastTs = await env.STATE.get("topscorers:last");
      if (lastTs) return new Response(lastTs, { headers: Object.assign({}, cors(), { "content-type": "application/json", "cache-control": "max-age=600" }) });
      return new Response(JSON.stringify({ updated: new Date().toISOString(), count: 0, scorers: [], errors: tse || "no data" }, null, 2),
        { headers: Object.assign({}, cors(), { "content-type": "application/json", "cache-control": "max-age=0" }) });
    }

    // iCal endpoint — returns a real .ics served as text/calendar so iOS/macOS open the native
    // "Add to Calendar" sheet directly (the reliable way for Apple devices). Fields come as query
    // params from the page; all are escaped for iCalendar and dates are format-validated.
    if (url.pathname === "/ics") {
      var qp = url.searchParams;
      var icsE = function (v) { return String(v == null ? "" : v).replace(/\\/g, "\\\\").replace(/;/g, "\\;").replace(/,/g, "\\,").replace(/[\r\n]+/g, " ").slice(0, 300); };
      var ds = (qp.get("s") || "").replace(/[^0-9TZ]/g, ""), de = (qp.get("e") || "").replace(/[^0-9TZ]/g, "");
      if (!/^\d{8}T\d{6}Z$/.test(ds) || !/^\d{8}T\d{6}Z$/.test(de)) return new Response("invalid dates", { status: 400, headers: cors() });
      var dnow = new Date().toISOString().replace(/[-:]/g, "").replace(/\.\d{3}/, "");   // YYYYMMDDTHHMMSSZ
      var uid = (qp.get("uid") || "").replace(/[\r\n,;]/g, "").slice(0, 120) || ("wc2026-" + ds + "@danpune.github.io");
      var fnm = String(qp.get("t") || "").normalize("NFKD").replace(/[^A-Za-z0-9 ]/g, "").trim().replace(/ +/g, "-").slice(0, 60) || "wc2026-match";   // "France vs Morocco  World Cup 2026" -> France-vs-Morocco-World-Cup-2026.ics
      var icsBody = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//WC2026 companion//EN", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        "BEGIN:VEVENT", "UID:" + uid, "DTSTAMP:" + dnow, "DTSTART:" + ds, "DTEND:" + de,
        "SUMMARY:" + icsE(qp.get("t") || "World Cup 2026 match"), "LOCATION:" + icsE(qp.get("loc")), "DESCRIPTION:" + icsE(qp.get("d")),
        "BEGIN:VALARM", "TRIGGER:-PT30M", "ACTION:DISPLAY", "DESCRIPTION:" + icsE(qp.get("t") || "World Cup 2026 match"), "END:VALARM",
        "END:VEVENT", "END:VCALENDAR"
      ].join("\r\n");
      return new Response(icsBody, { headers: Object.assign({}, cors(), {
        "content-type": "text/calendar; charset=utf-8",
        "content-disposition": 'inline; filename="' + fnm + '.ics"',
        "cache-control": "max-age=3600"
      }) });
    }

    // Voice / Apple-Shortcut endpoint: a ready-to-speak World Cup summary, built from the same KV snapshot
    // (no upstream API call). Optional ?team=NAME for one team. Returns plain text so a Shortcut can speak it directly.
    if (url.pathname === "/say") {
      var scRaw = await env.STATE.get("scores");
      var scData = null; try { scData = scRaw ? JSON.parse(scRaw) : null; } catch (e) {}
      var ms = (scData && scData.matches) || [];
      var nowS = Math.floor(Date.now() / 1000);
      var LIVE = { "1H": 1, "2H": 1, "ET": 1, "P": 1, "BT": 1, "LIVE": 1 };
      var rel = function (t) { if (!t) return "soon"; var d = t - nowS; if (d <= 60) return "shortly"; var m = Math.round(d / 60); if (m < 60) return "in about " + m + " minute" + (m === 1 ? "" : "s"); var h = Math.round(m / 60); if (h < 24) return "in about " + h + " hour" + (h === 1 ? "" : "s"); var dy = Math.round(h / 24); return "in about " + dy + " day" + (dy === 1 ? "" : "s"); };
      var score = function (m) { return m.home + " " + (m.h == null ? 0 : m.h) + ", " + m.away + " " + (m.a == null ? 0 : m.a); };
      var liveLabel = function (m) { return m.status === "HT" ? "half-time" : (m.minute != null ? m.minute + " minutes" : "in play"); };
      var isLive = function (m) { return LIVE[m.status] || m.status === "HT"; };
      var isDone = function (m) { return m.status === "FT" || m.status === "AET" || m.status === "PEN"; };
      var say, team = (url.searchParams.get("team") || "").trim().toLowerCase();
      if (team) {
        var alias = { "usa": "united states", "us": "united states", "south korea": "korea republic", "iran": "ir iran", "turkey": "türkiye", "czechia": "czech republic", "cape verde": "cabo verde", "ivory coast": "côte d'ivoire", "dr congo": "congo dr" };
        var q = alias[team] || team;
        var mine = ms.filter(function (m) { var h = (m.home || "").toLowerCase(), a = (m.away || "").toLowerCase(); return h.indexOf(q) > -1 || a.indexOf(q) > -1 || h.indexOf(team) > -1 || a.indexOf(team) > -1; });
        var lv = mine.filter(isLive), up = mine.filter(function (m) { return m.status === "NS"; }).sort(function (x, y) { return (x.t || 0) - (y.t || 0); }), dn = mine.filter(isDone).sort(function (x, y) { return (y.t || 0) - (x.t || 0); });
        if (lv.length) say = "Live: " + score(lv[0]) + ", " + liveLabel(lv[0]) + ".";
        else if (up.length) say = up[0].home + " play " + up[0].away + " " + rel(up[0].t) + ".";
        else if (dn.length) say = "Latest: " + score(dn[0]) + ", full-time.";
        else say = "I couldn't find a World Cup match for that team.";
        say = "World Cup 2026. " + say;
      } else {
        var live = ms.filter(isLive).slice(0, 4);
        var upN = ms.filter(function (m) { return m.status === "NS" && m.t; }).sort(function (x, y) { return x.t - y.t; });
        var ft = ms.filter(function (m) { return m.status === "FT"; }).sort(function (x, y) { return (y.t || 0) - (x.t || 0); });
        var parts = [];
        if (live.length) parts.push("Live now: " + live.map(function (m) { return score(m) + ", " + liveLabel(m); }).join("; ") + ".");
        else if (ft.length) parts.push("Latest result: " + score(ft[0]) + ".");
        if (upN.length) parts.push("Up next: " + upN[0].home + " versus " + upN[0].away + " " + rel(upN[0].t) + ".");
        say = "World Cup 2026. " + (parts.length ? parts.join(" ") : "No match updates right now.");
      }
      return new Response(say, { headers: Object.assign({}, cors(), { "content-type": "text/plain; charset=utf-8", "cache-control": "max-age=30" }) });
    }

    // ===== Fan wall: public submissions held for review; only admin-approved comments publish =====
    //   POST /comments {name,text}          -> pending queue (rate-limited, link-free, daily-capped)
    //   GET  /comments                      -> approved list (public, cached)
    //   GET  /comments/pending              -> admin: review queue
    //   POST /comments/mod {id,action}      -> admin: approve | reject | block | remove (remove = un-publish)
    if (url.pathname === "/comments" && request.method === "GET") {
      var oklist = await env.STATE.get("cmt:ok");
      return new Response(oklist || "[]", { headers: Object.assign({}, cors(), { "content-type": "application/json", "cache-control": "max-age=60" }) });
    }
    if (url.pathname === "/comments" && request.method === "POST") {
      var cb; try { cb = await request.json(); } catch (e) { return json({ error: "bad json" }); }
      var ctext = String((cb && cb.text) || "").replace(/\s+/g, " ").trim().slice(0, 500);
      var cname = String((cb && cb.name) || "").replace(/\s+/g, " ").trim().slice(0, 40) || "Anonymous";
      if (ctext.length < 2) return json({ error: "Comment is empty." });
      if (/https?:|www\./i.test(ctext + " " + cname)) return json({ error: "Links aren't allowed." });
      var chash = await ipHash(request.headers.get("cf-connecting-ip") || "0", env);
      if (await env.STATE.get("cmt:block:" + chash)) return json({ error: "Posting is disabled for this connection." });
      var crk = "cmt:rl:" + chash, crn = parseInt(await env.STATE.get(crk) || "0", 10);
      if (crn >= 5) return json({ error: "Rate limit: 5 comments per hour." });
      var cday = "cmt:day:" + new Date().toISOString().slice(0, 10);
      var cdn = parseInt(await env.STATE.get(cday) || "0", 10);
      if (cdn >= 50) return json({ error: "The wall is full for today - try tomorrow!" });   // caps KV writes so comments can never starve goal alerts
      var cid = Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
      await env.STATE.put("cmt:p:" + cid, JSON.stringify({ id: cid, n: cname, t: ctext, ts: Date.now(), h: chash }), { expirationTtl: 60 * 86400 });
      ctx.waitUntil(env.STATE.put(crk, String(crn + 1), { expirationTtl: 3600 }));
      ctx.waitUntil(env.STATE.put(cday, String(cdn + 1), { expirationTtl: 172800 }));
      return json({ ok: true });
    }
    if (url.pathname === "/comments/pending" || url.pathname === "/comments/mod") {
      var cak = (request.headers.get("authorization") || "").replace(/^Bearer\s+/i, "");
      if (!env.ADMIN_KEY || cak !== env.ADMIN_KEY) return new Response("forbidden", { status: 403, headers: cors() });
    }
    if (url.pathname === "/comments/pending") {
      var cls = await env.STATE.list({ prefix: "cmt:p:" });
      var pend = [];
      for (var ci = 0; ci < cls.keys.length && ci < 100; ci++) {
        var cpv = await env.STATE.get(cls.keys[ci].name);
        if (cpv) pend.push(JSON.parse(cpv));
      }
      pend.sort(function (a, b) { return b.ts - a.ts; });
      return json({ pending: pend });
    }
    if (url.pathname === "/comments/mod" && request.method === "POST") {
      var mb; try { mb = await request.json(); } catch (e) { return json({ error: "bad json" }); }
      var mid = String((mb && mb.id) || "").replace(/[^a-z0-9]/gi, "").slice(0, 30);
      var mact = String((mb && mb.action) || "");
      if (mact === "remove") {   // un-publish an already-approved comment
        var cur0 = JSON.parse(await env.STATE.get("cmt:ok") || "[]");
        await env.STATE.put("cmt:ok", JSON.stringify(cur0.filter(function (x) { return x.id !== mid; })));
        return json({ ok: true, action: "remove" });
      }
      var praw = await env.STATE.get("cmt:p:" + mid);
      if (!praw) return json({ error: "not found" });
      var pc = JSON.parse(praw);
      if (mact === "approve") {
        var cur = JSON.parse(await env.STATE.get("cmt:ok") || "[]");
        cur.unshift({ id: pc.id, n: pc.n, t: pc.t, ts: pc.ts });
        await env.STATE.put("cmt:ok", JSON.stringify(cur.slice(0, 200)));
      } else if (mact === "block") {
        await env.STATE.put("cmt:block:" + pc.h, "1");   // silently drops future posts from this connection
      } else if (mact !== "reject") {
        return json({ error: "unknown action" });
      }
      await env.STATE.delete("cmt:p:" + mid);
      return json({ ok: true, action: mact });
    }

    // Admin routes can send pushes / wipe detector state — require a secret key (set ADMIN_KEY as a Worker secret).
    // Fails closed: if ADMIN_KEY is unset or the Bearer token doesn't match, these routes return 403. (Cron is unaffected.)
    if (url.pathname === "/testpush" || url.pathname === "/run" || url.pathname === "/reset") {
      // Admin auth via "Authorization: Bearer <key>" ONLY — keeps the secret out of request-log URLs.
      var adminKey = (request.headers.get("authorization") || "").replace(/^Bearer\s+/i, "");
      if (!env.ADMIN_KEY || adminKey !== env.ADMIN_KEY)
        return new Response("forbidden", { status: 403, headers: cors() });
    }

    if (url.pathname === "/testpush") {
      var team = url.searchParams.get("team");
      var tr = await sendPush(env, "⚽ Test alert", team ? ("Targeted test for " + team + " followers") : "Your World Cup alerts are working!", team ? [team] : null);
      return json({ sent: tr });
    }
    if (url.pathname === "/run") { return json(await runDetector(env)); }
    if (url.pathname === "/reset") { await env.STATE.delete("state"); return json({ reset: true }); }

    return new Response("wc2026-api ok", { status: 200, headers: cors() });
  },
  async scheduled(event, env, ctx) {
    ctx.waitUntil(runDetector(env).then(function (r) {
      if (r && r.error) console.log("wc2026 detector error:", JSON.stringify(r.error));
    }));
  }
};

// Extract shirt-primary hex per team from an API-Football line-ups response (matched by team id).
function kitColors(lineups, homeId, awayId) {
  if (!lineups || !lineups.length) return null;
  function hex(id) {
    for (var i = 0; i < lineups.length; i++) {
      var lu = lineups[i];
      if (lu.team && lu.team.id === id) {
        var c = lu.team.colors && lu.team.colors.player;
        return (c && /^[0-9a-fA-F]{6}$/.test(c.primary || "")) ? c.primary : null;
      }
    }
    return null;
  }
  var h = hex(homeId), a = hex(awayId);
  return (h || a) ? { h: h, a: a } : null;
}

function sleep(ms) { return new Promise(function (resolve) { setTimeout(resolve, ms); }); }
// API-Football signals a transient problem (rate limit, etc.) as an HTTP 200 with a non-empty
// `errors` object, not an HTTP error code. Left unretried, a single throttled cron tick freezes
// the live scores snapshot for a full minute (the caller returns early without writing anything) —
// give a failing call a couple of chances to recover within the same tick before giving up.
async function fetchJSONRetry(url, headers, tries) {
  var j = null;
  for (var i = 0; i < (tries || 3); i++) {
    j = await (await fetch(url, { headers: headers })).json();
    if (!(j.errors && Object.keys(j.errors).length)) return j;
    if (i < (tries || 3) - 1) await sleep(1500 * (i + 1));   // 1.5s, then 3s
  }
  return j;   // still erroring after all attempts — caller surfaces j.errors as before
}

async function runDetector(env) {
  var raw = null, state = {}; try { raw = await env.STATE.get("state"); state = JSON.parse(raw) || {}; } catch (e) {}
  state.fx = state.fx || {};
  state.kits = state.kits || {};   // fid -> {h,a} shirt-primary hex, captured once when line-ups publish
  state.kitTry = state.kitTry || {}; // fid -> attempts where line-ups existed but had no colours (bounded retry)
  var sending = !!state.seeded, log = [], sent = 0;
  var headers = { "x-apisports-key": env.APISPORTS_KEY }, fixtures = [];
  try {
    var fxResp = await fetchJSONRetry(API + "/fixtures?league=" + WC_LEAGUE + "&season=" + SEASON, headers);
    // API-Football signals quota/param problems as a 200 with a non-empty errors object — surface it instead of
    // silently doing nothing (which would advance no state but emit no signal during a live match).
    if (fxResp.errors && Object.keys(fxResp.errors).length) return { error: fxResp.errors };
    fixtures = fxResp.response || [];
    // Decoupled reads: write the public scores snapshot to KV so visitor /scores reads never touch the API.
    var snap = fixtures.map(function (x) { var k = state.kits[String(x.fixture.id)] || {}; return { id: x.fixture.id, home: x.teams.home.name, away: x.teams.away.name, h: x.goals.home, a: x.goals.away, status: x.fixture.status.short, minute: x.fixture.status.elapsed, w: x.teams.home.winner === true ? "h" : (x.teams.away.winner === true ? "a" : null), kh: k.h || null, ka: k.a || null, t: x.fixture.timestamp }; });
    // Write the scores snapshot only when it actually CHANGED (or every ~10 min as a freshness heartbeat).
    // The minute advances every tick while a match is live, so this still refreshes each minute during a game —
    // but skips the many idle minutes between matches. Biggest KV-write saver (was 1/tick = up to 1440/day).
    if (snap.length) {
      var snapStr = JSON.stringify(snap), prevSnapStr = null, prevAge = Infinity;
      try { var ps = JSON.parse(await env.STATE.get("scores")); prevSnapStr = JSON.stringify(ps.matches); prevAge = Date.now() - Date.parse(ps.updated); } catch (e) {}
      if (snapStr !== prevSnapStr || prevAge > 600000) await env.STATE.put("scores", JSON.stringify({ updated: new Date().toISOString(), count: snap.length, matches: snap, errors: null }));
    }
  } catch (e) { return { error: String(e) }; }
  var now = Date.now();
  // tier "core" -> everyone following the team gets it; tier "extra" -> only subscribers whose alerts="all"
  async function fire(title, body, teams, tier) { if (sending) { await sendPush(env, title, body, teams, tier); sent++; log.push(title); } }
  for (var i = 0; i < fixtures.length; i++) {
    var f = fixtures[i], fid = String(f.fixture.id), short = f.fixture.status.short;
    var home = f.teams.home.name, away = f.teams.away.name, teams = [home, away];
    var gh = f.goals.home == null ? 0 : f.goals.home, ga = f.goals.away == null ? 0 : f.goals.away, score = gh + "–" + ga;
    var ko = Date.parse(f.fixture.date), st = state.fx[fid] || {};
    var live = LIVE_S.indexOf(short) >= 0, finished = FINAL_S.indexOf(short) >= 0;
    // Kit colours, pre-KO only: one lineups call/min once published (~40 min out). Live matches reuse
    // the detail-path lineups below — no extra API call (saves 1 call/min/live match; rate-limit headroom).
    // Bounded: if lineups exist but carry no colours ~10 times, the provider isn't going to add them — stop.
    if (!state.kits[fid] && short === "NS" && ko - now > 0 && ko - now <= 50 * 60000 && (state.kitTry[fid] || 0) < 10) {
      try {
        var luK = (await (await fetch(API + "/fixtures/lineups?fixture=" + fid, { headers })).json()).response || [];
        var kc = kitColors(luK, f.teams.home.id, f.teams.away.id);
        if (kc) { state.kits[fid] = kc; delete state.kitTry[fid]; }
        else if (luK.length) state.kitTry[fid] = (state.kitTry[fid] || 0) + 1;   // published but colourless
      } catch (e) {}
    }
    if (short === "NS" && !st.ko && ko - now > 0 && ko - now <= 16 * 60000) { await fire("🔜 Kicking off soon", home + " vs " + away + " starts in ~15 min", teams, "core"); st.ko = true; }
    if (live && !st.started) { await fire("🟢 Kick-off — " + home + " vs " + away, "The match is underway", teams, "core"); st.started = true; }
    if (short === "HT" && st.short !== "HT") await fire("⏸️ Half-time", home + " " + score + " " + away, teams, "core");
    if (finished && !st.ft) { await fire("🏁 Full-time", home + " " + score + " " + away, teams, "core"); st.ft = true; }
    if (live || (finished && !st.evDone)) {
      try {
        var events = (await (await fetch(API + "/fixtures/events?fixture=" + fid, { headers })).json()).response || [];
        // De-dup by a stable per-event signature (not array length), so a feed that reorders or drops an
        // event (e.g. VAR overturns a goal) can't re-fire or skip alerts. fired[] holds signatures already seen.
        var migrate = (st.fired == null && st.seen != null);   // legacy state: mark current events, don't re-alert on the deploy
        if (st.fired == null) st.fired = {};
        for (var j = 0; j < events.length; j++) {
          var sig = eventSig(events[j]);
          if (st.fired[sig]) continue;
          st.fired[sig] = 1;
          if (migrate) continue;
          var m = eventMessage(events[j]); if (m) await fire(m.title, m.body, teams, m.tier);
        }
        if (finished) { st.evDone = true; st.fired = {}; }       // free memory once the match is over
        // Decoupled reads: write match detail to KV so visitor /match reads never touch the API for this match.
        try {
          var md = await Promise.all([
            fetch(API + "/fixtures/statistics?fixture=" + fid, { headers }).then(function (x) { return x.json(); }),
            fetch(API + "/fixtures/lineups?fixture=" + fid, { headers }).then(function (x) { return x.json(); })
          ]);
          var kcL = kitColors(md[1].response || [], f.teams.home.id, f.teams.away.id); if (kcL) state.kits[fid] = kcL;   // reuse (no extra call); overwrite so provider corrections propagate mid-match
          var freshM = { id: fid, stats: md[0].response || [], events: events, lineups: md[1].response || [], referee: (f.fixture && f.fixture.referee) || null, venue: (f.fixture && f.fixture.venue && f.fixture.venue.name) || null, errors: null };
          var prevMRaw = await env.STATE.get("match:" + fid);   // keep-best: don't let a flapped-empty tick wipe goals/stats already captured
          var prevM = null; try { prevM = prevMRaw ? JSON.parse(prevMRaw) : null; } catch (e) {}
          var mergedM = prevM ? bestMatch(prevM, freshM) : freshM;
          // Write the goal/timeline content (events, line-ups, ref, venue) the INSTANT it changes, but throttle the
          // noisy possession/shots STATS churn to ~once / 3 min — caps KV writes on busy days without delaying goals.
          var core = function (o) { return JSON.stringify({ e: o.events, l: o.lineups, r: o.referee, v: o.venue }); };
          var coreChanged = !prevM || core(mergedM) !== core(prevM);
          var statsChanged = !prevM || JSON.stringify(mergedM.stats) !== JSON.stringify(prevM.stats);
          var mdAge = (prevM && prevM.updated) ? (now - Date.parse(prevM.updated)) : Infinity;
          if (coreChanged || (statsChanged && mdAge > 180000)) { mergedM.updated = new Date().toISOString(); await env.STATE.put("match:" + fid, JSON.stringify(mergedM), { expirationTtl: 604800 }); }
        } catch (e) {}
      } catch (e) {}
    }
    st.short = short; st.score = score; state.fx[fid] = st;
  }
  state.seeded = true;
  // Only write KV when the state actually changed — a no-op minute (no live match, no new event) writes
  // nothing, keeping us under the Workers KV free-tier write limit (~1000/day) instead of 1440/day.
  var next = JSON.stringify(state);
  var wrote = next !== raw;
  if (wrote) await env.STATE.put("state", next);
  return { ok: true, justSeeded: !sending, sent: sent, wrote: wrote, log: log.slice(0, 30) };
}

// Stable signature for de-duping alerts — uses ids/minute/detail, not array position.
function eventSig(e) {
  var t = e.time ? ((e.time.elapsed == null ? "" : e.time.elapsed) + "+" + (e.time.extra || 0)) : "";
  var pid = (e.player && (e.player.id || e.player.name)) || "";
  var team = (e.team && (e.team.id || e.team.name)) || "";
  return [e.type || "", e.detail || "", t, team, pid].join("|");
}
function eventMessage(e) {
  var team = e.team && e.team.name ? e.team.name : "", player = e.player && e.player.name ? e.player.name : "";
  var t = e.time && e.time.elapsed != null ? e.time.elapsed + "'" : "", type = e.type, detail = e.detail || "";
  if (type === "Goal") {
    if (detail === "Missed Penalty") return { title: "🎯 Penalty missed — " + team, body: player + " " + t, tier: "core" };
    var lb = detail === "Own Goal" ? "Own goal" : (detail === "Penalty" ? "Penalty goal" : "GOAL!");
    return { title: "⚽ " + lb + " — " + team, body: player + " " + t, tier: "core" };
  }
  if (type === "Card") {
    if (detail === "Red Card") return { title: "🟥 Red card — " + team, body: player + " " + t, tier: "extra" };
    if (detail === "Yellow Card") return { title: "🟨 Yellow card — " + team, body: player + " " + t, tier: "extra" };
    return null;
  }
  if (type === "subst") return { title: "🔀 Substitution — " + team, body: player + " " + t, tier: "extra" };
  if (type === "Var") return { title: "📺 VAR — " + team, body: detail + " " + t, tier: "extra" };
  return null;
}

async function sendPush(env, title, body, teams, tier) {
  var payload = { app_id: OS_APP, headings: { en: title }, contents: { en: body } };
  if (teams && teams.length) {
    // OneSignal has no parentheses and ANDs bind tighter than ORs, so we can't write
    // "(team_A OR team_B OR all_matches) AND alerts=all". Instead, build one OR-branch per
    // audience and AND the alerts gate INTO each branch, giving the equivalent:
    //   (team_A AND alerts=all) OR (team_B AND alerts=all) OR (all_matches AND alerts=all)
    // "core" events (kickoff/goals/full-time) omit the gate so every follower gets them.
    var extra = tier === "extra";
    var branches = teams.map(function (t) { return { field: "tag", key: "team_" + teamTag(t), relation: "=", value: "1" }; });
    branches.push({ field: "tag", key: "all_matches", relation: "=", value: "1" });
    var filters = [];
    branches.forEach(function (b, i) {
      if (i > 0) filters.push({ operator: "OR" });
      filters.push(b);
      if (extra) filters.push({ field: "tag", key: "alerts", relation: "=", value: "all" });
    });
    payload.filters = filters;
  } else { payload.included_segments = ["Subscribed Users"]; }
  try { return await (await fetch("https://api.onesignal.com/notifications", { method: "POST", headers: { "Content-Type": "application/json", "Authorization": "Basic " + env.ONESIGNAL_REST_KEY }, body: JSON.stringify(payload) })).json(); }
  catch (e) { return { error: String(e) }; }
}
function tagKey(t) { return t.toLowerCase().replace(/[^a-z0-9]+/g, "_"); }
function cors() { return { "access-control-allow-origin": "*", "access-control-allow-methods": "GET, POST, OPTIONS", "access-control-allow-headers": "*" }; }
async function ipHash(ip, env) {   // salted so raw IPs are never stored (privacy) yet blocks are stable
  var d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(ip + "|" + (env.ADMIN_KEY || "wc26")));
  return Array.prototype.map.call(new Uint8Array(d.slice(0, 8)), function (b) { return ("0" + b.toString(16)).slice(-2); }).join("");
}
function json(obj) { return new Response(JSON.stringify(obj, null, 2), { headers: Object.assign({}, cors(), { "content-type": "application/json" }) }); }
// Merge two match-detail payloads keeping the MORE COMPLETE value of each list. The provider's feed flaps —
// events/stats can momentarily return empty then reappear — so a naive overwrite wipes goals we'd already
// captured. Keep the larger list for each field so a cached match only ever gets richer, never thinner.
function bestMatch(prev, next) {
  prev = prev || {}; next = next || {};
  var pick = function (a, b) { return ((a || []).length > (b || []).length) ? a : b; };
  return {
    id: next.id || prev.id,
    stats: pick(prev.stats, next.stats),
    events: pick(prev.events, next.events),
    lineups: pick(prev.lineups, next.lineups),
    referee: next.referee || prev.referee || null,
    venue: next.venue || prev.venue || null,
    errors: next.errors || null,
    updated: next.updated || prev.updated
  };
}
function stripCdata(s) { return String(s).replace(/^\s*<!\[CDATA\[/, "").replace(/\]\]>\s*$/, ""); }
function decodeEntities(s) { return String(s).replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&#0?39;/g, "'").replace(/&apos;/g, "'").replace(/&amp;/g, "&"); }
