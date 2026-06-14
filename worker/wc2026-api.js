/*
 * wc2026-api — Cloudflare Worker (live scores + match stats + goal-alert detector)
 * -----------------------------------------------------------------------------
 * Backup/documentation copy of the deployed Worker.
 *
 * HTTP routes (fetch handler):
 *   GET /scores        -> all World Cup 2026 fixtures (live + finished + scheduled)
 *   GET /match?id=     -> {stats, events, lineups} for one fixture
 *                         stats = possession/shots/xG…; events = goal/card/sub timeline;
 *                         lineups = starting XI + formation per team
 *   GET /status        -> API-Football account/key check
 *   GET /testpush?key=…[&team=X] -> send a test push (admin; requires ADMIN_KEY)
 *   GET /run?key=…     -> run the alert detector once (admin; first call seeds silently)
 *   GET /reset?key=…   -> clear the detector's KV memory (admin; re-seeds next run)
 *   GET /              -> health check
 *   (/testpush, /run, /reset require ?key=<ADMIN_KEY>; they fail closed with 403 otherwise.)
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
 *   - KV binding  STATE            = namespace storing the detector's seen state
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

export default {
  async fetch(request, env, ctx) {
    var url = new URL(request.url);
    var headers = { "x-apisports-key": env.APISPORTS_KEY };
    if (request.method === "OPTIONS") return new Response(null, { headers: cors() });

    if (url.pathname === "/status") {
      var s = await fetch(API + "/status", { headers });
      var body = await s.json();
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
      var mc = caches.default, mk = new Request(new URL("/match?id=" + id, url.origin).toString());
      var mh = await mc.match(mk); if (mh) return mh;
      var stats = [], events = [], lineups = [], me = null;
      try {
        var r = await Promise.all([
          fetch(API + "/fixtures/statistics?fixture=" + id, { headers }).then(function (x) { return x.json(); }),
          fetch(API + "/fixtures/events?fixture=" + id, { headers }).then(function (x) { return x.json(); }),
          fetch(API + "/fixtures/lineups?fixture=" + id, { headers }).then(function (x) { return x.json(); })
        ]);
        stats = r[0].response || []; events = r[1].response || []; lineups = r[2].response || [];
        if (r[0].errors && Object.keys(r[0].errors).length) me = r[0].errors;
      } catch (e) { me = String(e); }
      var mr = new Response(JSON.stringify({ id: id, stats: stats, events: events, lineups: lineups, errors: me }, null, 2), { headers: Object.assign({}, cors(), { "content-type": "application/json", "cache-control": "max-age=30" }) });
      ctx.waitUntil(mc.put(mk, mr.clone())); return mr;
    }

    if (url.pathname === "/scores") {
      var c = caches.default;
      var ck = new Request(new URL("/scores", url.origin).toString());          // primary, short TTL
      var lk = new Request(new URL("/scores_lastgood", url.origin).toString());  // last-good, long TTL
      var hit = await c.match(ck); if (hit) return hit;
      function scoresRes(obj, maxAge) { return new Response(JSON.stringify(obj, null, 2), { headers: Object.assign({}, cors(), { "content-type": "application/json", "cache-control": "max-age=" + maxAge }) }); }
      var matches = [], errors = null;
      try {
        var data = await (await fetch(API + "/fixtures?league=" + WC_LEAGUE + "&season=" + SEASON, { headers })).json();
        if (data.errors && Object.keys(data.errors).length) errors = data.errors;
        matches = (data.response || []).map(function (x) { return { id: x.fixture.id, home: x.teams.home.name, away: x.teams.away.name, h: x.goals.home, a: x.goals.away, status: x.fixture.status.short, minute: x.fixture.status.elapsed }; });
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

    // Admin routes can send pushes / wipe detector state — require a secret key (set ADMIN_KEY as a Worker secret).
    // Fails closed: if ADMIN_KEY is unset or the ?key= doesn't match, these routes return 403. (Cron is unaffected.)
    if (url.pathname === "/testpush" || url.pathname === "/run" || url.pathname === "/reset") {
      if (!env.ADMIN_KEY || url.searchParams.get("key") !== env.ADMIN_KEY)
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

async function runDetector(env) {
  var state = {}; try { state = JSON.parse(await env.STATE.get("state")) || {}; } catch (e) {}
  state.fx = state.fx || {};
  var sending = !!state.seeded, log = [], sent = 0;
  var headers = { "x-apisports-key": env.APISPORTS_KEY }, fixtures = [];
  try {
    var fxResp = await (await fetch(API + "/fixtures?league=" + WC_LEAGUE + "&season=" + SEASON, { headers })).json();
    // API-Football signals quota/param problems as a 200 with a non-empty errors object — surface it instead of
    // silently doing nothing (which would advance no state but emit no signal during a live match).
    if (fxResp.errors && Object.keys(fxResp.errors).length) return { error: fxResp.errors };
    fixtures = fxResp.response || [];
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
    if (short === "NS" && !st.ko && ko - now > 0 && ko - now <= 11 * 60000) { await fire("🔜 Kicking off soon", home + " vs " + away + " starts in ~10 min", teams, "core"); st.ko = true; }
    if (short === "HT" && st.short !== "HT") await fire("⏸️ Half-time", home + " " + score + " " + away, teams, "extra");
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
      } catch (e) {}
    }
    st.short = short; st.score = score; state.fx[fid] = st;
  }
  state.seeded = true; await env.STATE.put("state", JSON.stringify(state));
  return { ok: true, justSeeded: !sending, sent: sent, log: log.slice(0, 30) };
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
function json(obj) { return new Response(JSON.stringify(obj, null, 2), { headers: Object.assign({}, cors(), { "content-type": "application/json" }) }); }
