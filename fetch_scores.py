#!/usr/bin/env python3
"""
Fetch FIFA World Cup 2026 scores and write scores.json, which the webpage reads as
its fallback layer (the page's live view uses the same worker feed directly).

Source: the project's own Cloudflare Worker /scores endpoint, which serves the
API-Football snapshot the cron writes to KV. This is the SAME data the live page
overlays, so scores.json now carries identical, reliable detail — including the
penalty-shootout winner (w) that football-data.org's free tier reported unreliably.
No API key required (the worker route is public). Run by GitHub Actions on a schedule.
No third-party packages (Python standard library only).
"""
import json, os, sys, urllib.request, unicodedata
from datetime import datetime, timezone

WORKER_SCORES = "https://wc2026-api.footy26.workers.dev/scores"

# group games: matchNo -> [home, away]  (canonical names used on the page)
SCHEDULE_PAIRS = [[1, ["Mexico", "South Africa"]], [2, ["South Korea", "Czechia"]], [3, ["Canada", "Bosnia & Herzegovina"]], [4, ["USA", "Paraguay"]], [5, ["Qatar", "Switzerland"]], [6, ["Brazil", "Morocco"]], [7, ["Haiti", "Scotland"]], [8, ["Australia", "Türkiye"]], [9, ["Germany", "Curaçao"]], [10, ["Netherlands", "Japan"]], [11, ["Ivory Coast", "Ecuador"]], [12, ["Sweden", "Tunisia"]], [13, ["Spain", "Cape Verde"]], [14, ["Belgium", "Egypt"]], [15, ["Saudi Arabia", "Uruguay"]], [16, ["Iran", "New Zealand"]], [17, ["France", "Senegal"]], [18, ["Iraq", "Norway"]], [19, ["Argentina", "Algeria"]], [20, ["Austria", "Jordan"]], [21, ["Portugal", "DR Congo"]], [22, ["England", "Croatia"]], [23, ["Ghana", "Panama"]], [24, ["Uzbekistan", "Colombia"]], [25, ["Czechia", "South Africa"]], [26, ["Switzerland", "Bosnia & Herzegovina"]], [27, ["Canada", "Qatar"]], [28, ["Mexico", "South Korea"]], [29, ["USA", "Australia"]], [30, ["Scotland", "Morocco"]], [31, ["Brazil", "Haiti"]], [32, ["Türkiye", "Paraguay"]], [33, ["Netherlands", "Sweden"]], [34, ["Germany", "Ivory Coast"]], [35, ["Ecuador", "Curaçao"]], [36, ["Tunisia", "Japan"]], [37, ["Spain", "Saudi Arabia"]], [38, ["Belgium", "Iran"]], [39, ["Uruguay", "Cape Verde"]], [40, ["New Zealand", "Egypt"]], [41, ["Argentina", "Austria"]], [42, ["France", "Iraq"]], [43, ["Norway", "Senegal"]], [44, ["Jordan", "Algeria"]], [45, ["Portugal", "Uzbekistan"]], [46, ["England", "Ghana"]], [47, ["Panama", "Croatia"]], [48, ["Colombia", "DR Congo"]], [49, ["Switzerland", "Canada"]], [50, ["Bosnia & Herzegovina", "Qatar"]], [51, ["Scotland", "Brazil"]], [52, ["Morocco", "Haiti"]], [53, ["Czechia", "Mexico"]], [54, ["South Africa", "South Korea"]], [55, ["Curaçao", "Ivory Coast"]], [56, ["Ecuador", "Germany"]], [57, ["Japan", "Sweden"]], [58, ["Tunisia", "Netherlands"]], [59, ["Türkiye", "USA"]], [60, ["Paraguay", "Australia"]], [61, ["Norway", "France"]], [62, ["Senegal", "Iraq"]], [63, ["Cape Verde", "Saudi Arabia"]], [64, ["Uruguay", "Spain"]], [65, ["Egypt", "Iran"]], [66, ["New Zealand", "Belgium"]], [67, ["Panama", "England"]], [68, ["Croatia", "Ghana"]], [69, ["Colombia", "Portugal"]], [70, ["DR Congo", "Uzbekistan"]], [71, ["Algeria", "Austria"]], [72, ["Jordan", "Argentina"]]]
# knockout games: matchNo -> kickoff UTC. Teams aren't known in advance, so these map by
# exact kickoff time (every KO match has a unique slot, >=3h apart) instead of by team-pair.
KO_SCHEDULE = [[73, "2026-06-28T19:00:00Z"], [74, "2026-06-29T20:30:00Z"], [75, "2026-06-30T01:00:00Z"], [76, "2026-06-29T17:00:00Z"], [77, "2026-06-30T21:00:00Z"], [78, "2026-06-30T17:00:00Z"], [79, "2026-07-01T01:00:00Z"], [80, "2026-07-01T16:00:00Z"], [81, "2026-07-02T00:00:00Z"], [82, "2026-07-01T20:00:00Z"], [83, "2026-07-02T23:00:00Z"], [84, "2026-07-02T19:00:00Z"], [85, "2026-07-03T03:00:00Z"], [86, "2026-07-03T22:00:00Z"], [87, "2026-07-04T01:30:00Z"], [88, "2026-07-03T18:00:00Z"], [89, "2026-07-04T21:00:00Z"], [90, "2026-07-04T17:00:00Z"], [91, "2026-07-05T20:00:00Z"], [92, "2026-07-06T00:00:00Z"], [93, "2026-07-06T19:00:00Z"], [94, "2026-07-07T00:00:00Z"], [95, "2026-07-07T16:00:00Z"], [96, "2026-07-07T20:00:00Z"], [97, "2026-07-09T20:00:00Z"], [98, "2026-07-10T19:00:00Z"], [99, "2026-07-11T21:00:00Z"], [100, "2026-07-12T01:00:00Z"], [101, "2026-07-14T19:00:00Z"], [102, "2026-07-15T19:00:00Z"], [103, "2026-07-18T21:00:00Z"], [104, "2026-07-19T19:00:00Z"]]

# API-Football team name (normalized) -> canonical name on the page
ALIAS = {"unitedstates": "USA", "usa": "USA", "unitedstatesofamerica": "USA", "korearepublic": "South Korea", "southkorea": "South Korea", "korea": "South Korea", "republicofkorea": "South Korea", "turkey": "Türkiye", "turkiye": "Türkiye", "iriran": "Iran", "iran": "Iran", "islamicrepublicofiran": "Iran", "cotedivoire": "Ivory Coast", "ivorycoast": "Ivory Coast", "congodr": "DR Congo", "drcongo": "DR Congo", "democraticrepublicofthecongo": "DR Congo", "democraticrepublicofcongo": "DR Congo", "congo": "DR Congo", "caboverde": "Cape Verde", "capeverde": "Cape Verde", "capeverdeislands": "Cape Verde", "bosniaandherzegovina": "Bosnia & Herzegovina", "bosniaherzegovina": "Bosnia & Herzegovina", "bosnia": "Bosnia & Herzegovina", "czechrepublic": "Czechia", "czechia": "Czechia", "curacao": "Curaçao"}

# canonical names that appear in the schedule (for direct/fuzzy matching)
CANON = sorted({t for _, pr in SCHEDULE_PAIRS for t in pr})

def norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return "".join(c for c in s.lower() if c.isalnum())

CANON_NORM = {norm(c): c for c in CANON}
# lookup: frozenset(home,away) -> matchNo
PAIR_LOOKUP = {frozenset(pr): no for no, pr in SCHEDULE_PAIRS}

def resolve_team(name):
    n = norm(name)
    if not n: return None                      # empty name must not fuzzy-match the first team
    if n in ALIAS: return ALIAS[n]
    if n in CANON_NORM: return CANON_NORM[n]
    for cn, canon in CANON_NORM.items():       # fuzzy: one contains the other
        if cn and (cn in n or n in cn): return canon
    return None

def _epoch(iso):
    """'2026-06-28T19:00:00Z' -> unix seconds (UTC)."""
    return int(datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())

KO_SLOTS = [(_epoch(u), no) for no, u in KO_SCHEDULE]

def ko_match_no(t):
    """Map a knockout fixture's kickoff (unix seconds) to our match number by nearest
    scheduled slot. Exact normally; ±2h tolerance absorbs minor drift (slots are >=3h apart,
    and no group game kicks off within 2h of any knockout slot, so this never hits a group game)."""
    try:
        t = int(t)
    except (TypeError, ValueError):
        return None
    best, best_d = None, 7200
    for se, no in KO_SLOTS:
        d = abs(se - t)
        if d < best_d:
            best, best_d = no, d
    return best

# API-Football short status -> the page's status vocabulary (mirrors the page's mapApiStatus)
def map_status(short):
    if short == "HT": return "PAUSED"
    if short in ("1H", "2H", "ET", "BT", "P", "LIVE", "INT"): return "IN_PLAY"
    if short in ("FT", "AET", "PEN"): return "FINISHED"
    return "SCHEDULED"

def build_scores(matches):
    """Map worker /scores fixtures to our match numbers. Returns (scores, unresolved).
    Each fixture is {home, away, h, a, status (short), minute, t (epoch), w}."""
    scores, unresolved = {}, set()
    for m in matches:
        h = resolve_team(m.get("home", ""))
        a = resolve_team(m.get("away", ""))
        ko_no = ko_match_no(m.get("t"))            # knockout fixtures map by kickoff time, not pair
        if ko_no:
            # Knockout: teams are decided only at draw time. Need both resolved to record the
            # real matchup (pre-draw the feed has TBD names -> skip; the page derives those slots).
            if not h or not a:
                continue
            no, is_ko = ko_no, True
        else:
            if not h or not a:
                for raw, got in ((m.get("home"), h), (m.get("away"), a)):
                    if raw and not got: unresolved.add(raw)
                continue
            no = PAIR_LOOKUP.get(frozenset((h, a)))   # group games map by team-pair
            if not no: continue
            is_ko = False
        gh, ga = m.get("h"), m.get("a")
        status = map_status(m.get("status", ""))
        if gh is None or ga is None: continue
        if status not in ("FINISHED", "IN_PLAY", "PAUSED"): continue
        entry = {"h": gh, "a": ga, "s": status}
        if is_ko:                                  # carry the real teams so the page shows them (koFeedTeams)
            entry["home"], entry["away"] = h, a
            if gh == ga:                           # level score -> who advanced is decided by the shootout
                w = m.get("w")
                if w in ("h", "a"):
                    entry["w"] = w
        if status == "IN_PLAY":                    # attach the live minute only when in play and the feed provides it
            try:
                mn = m.get("minute")
                if mn is not None: entry["min"] = int(mn)
            except (TypeError, ValueError):
                pass
        scores[str(no)] = entry
    return scores, unresolved

# ESPN free public scoreboard — merged as a second source Jul 18 2026, when the
# API-Football plan lapsed mid-tournament and the worker feed froze with the
# semifinals unplayed. No key, fail-safe. Whole-tournament date range: one call,
# no date math, backfills any gap.
ESPN_SB = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard?dates=20260611-20260719&limit=200"

def espn_fix(ev):
    """One ESPN scoreboard event -> a worker-shaped fixture dict, or None if not started/parseable."""
    try:
        comp = ev["competitions"][0]
        sides = {x.get("homeAway"): x for x in comp.get("competitors", [])}
        hm, aw = sides.get("home"), sides.get("away")
        st = (comp.get("status") or {}).get("type") or {}
        state, sname = st.get("state"), st.get("name", "")
        if not hm or not aw or state == "pre":
            return None
        short = "FT" if state == "post" else ("HT" if "HALFTIME" in sname else "1H")
        t = int(datetime.strptime(ev["date"], "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc).timestamp())
        fx = {"home": hm["team"]["displayName"], "away": aw["team"]["displayName"],
              "h": int(hm.get("score") or 0), "a": int(aw.get("score") or 0),
              "status": short, "t": t, "minute": None}
        clock = (comp.get("status") or {}).get("displayClock") or ""
        digits = "".join(c for c in clock.split("+")[0] if c.isdigit())
        if short == "1H" and digits:
            fx["minute"] = int(digits)
        sh, sa = hm.get("shootoutScore"), aw.get("shootoutScore")
        if fx["h"] == fx["a"] and sh is not None and sa is not None:
            fx["w"] = "h" if int(sh) > int(sa) else "a"
        return fx
    except Exception:
        return None

def espn_fixtures():
    try:
        req = urllib.request.Request(ESPN_SB, headers={"User-Agent": "wc2026-scores/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r)
    except Exception as e:
        print(f"ESPN fetch failed ({e}); continuing without it.", file=sys.stderr)
        return []
    return [fx for fx in (espn_fix(ev) for ev in data.get("events", [])) if fx]

def main():
    req = urllib.request.Request(WORKER_SCORES, headers={"User-Agent": "wc2026-scores/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            payload = json.load(r)
        matches = payload.get("matches", [])
    except Exception as e:
        # Worker down is no longer fatal — ESPN can carry the tournament alone.
        print(f"Worker fetch failed ({e}); trying ESPN alone.", file=sys.stderr)
        matches = []
    # ESPN entries come AFTER the worker list, so for the same match number the
    # fresher ESPN result overwrites the (possibly frozen) worker snapshot.
    matches = matches + espn_fixtures()
    scores, unresolved = build_scores(matches)
    if unresolved:
        print("Unmatched team names (add to ALIAS):", sorted(unresolved), file=sys.stderr)
    if not scores:
        # An empty result mid-tournament means the upstream feed hiccuped — never wipe a good file.
        print("No scores parsed; leaving existing scores.json untouched.", file=sys.stderr)
        sys.exit(0)
    out = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "api-football (worker) + espn",
        "scores": scores,
    }
    with open("scores.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=0)
    ko = sum(1 for v in scores.values() if "home" in v)
    print(f"Wrote scores.json with {len(scores)} live/final results ({ko} knockout) from {len(matches)} feed matches.")

if __name__ == "__main__":
    main()
