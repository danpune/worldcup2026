#!/usr/bin/env python3
"""
Fetch FIFA World Cup 2026 scores from football-data.org and write scores.json,
which the webpage reads. Run by GitHub Actions on a schedule.
Needs env var FOOTBALL_DATA_API_KEY (store it as a repo Secret).
No third-party packages required (uses Python's standard library).
"""
import json, os, sys, urllib.request, unicodedata
from datetime import datetime, timezone

API_URL = "https://api.football-data.org/v4/competitions/WC/matches"

# group games: matchNo -> [home, away]  (canonical names used on the page)
SCHEDULE_PAIRS = [[1, ["Mexico", "South Africa"]], [2, ["South Korea", "Czechia"]], [3, ["Canada", "Bosnia & Herzegovina"]], [4, ["USA", "Paraguay"]], [5, ["Qatar", "Switzerland"]], [6, ["Brazil", "Morocco"]], [7, ["Haiti", "Scotland"]], [8, ["Australia", "Türkiye"]], [9, ["Germany", "Curaçao"]], [10, ["Netherlands", "Japan"]], [11, ["Ivory Coast", "Ecuador"]], [12, ["Sweden", "Tunisia"]], [13, ["Spain", "Cape Verde"]], [14, ["Belgium", "Egypt"]], [15, ["Saudi Arabia", "Uruguay"]], [16, ["Iran", "New Zealand"]], [17, ["France", "Senegal"]], [18, ["Iraq", "Norway"]], [19, ["Argentina", "Algeria"]], [20, ["Austria", "Jordan"]], [21, ["Portugal", "DR Congo"]], [22, ["England", "Croatia"]], [23, ["Ghana", "Panama"]], [24, ["Uzbekistan", "Colombia"]], [25, ["Czechia", "South Africa"]], [26, ["Switzerland", "Bosnia & Herzegovina"]], [27, ["Canada", "Qatar"]], [28, ["Mexico", "South Korea"]], [29, ["USA", "Australia"]], [30, ["Scotland", "Morocco"]], [31, ["Brazil", "Haiti"]], [32, ["Türkiye", "Paraguay"]], [33, ["Netherlands", "Sweden"]], [34, ["Germany", "Ivory Coast"]], [35, ["Ecuador", "Curaçao"]], [36, ["Tunisia", "Japan"]], [37, ["Spain", "Saudi Arabia"]], [38, ["Belgium", "Iran"]], [39, ["Uruguay", "Cape Verde"]], [40, ["New Zealand", "Egypt"]], [41, ["Argentina", "Austria"]], [42, ["France", "Iraq"]], [43, ["Norway", "Senegal"]], [44, ["Jordan", "Algeria"]], [45, ["Portugal", "Uzbekistan"]], [46, ["England", "Ghana"]], [47, ["Panama", "Croatia"]], [48, ["Colombia", "DR Congo"]], [49, ["Switzerland", "Canada"]], [50, ["Bosnia & Herzegovina", "Qatar"]], [51, ["Scotland", "Brazil"]], [52, ["Morocco", "Haiti"]], [53, ["Czechia", "Mexico"]], [54, ["South Africa", "South Korea"]], [55, ["Curaçao", "Ivory Coast"]], [56, ["Ecuador", "Germany"]], [57, ["Japan", "Sweden"]], [58, ["Tunisia", "Netherlands"]], [59, ["Türkiye", "USA"]], [60, ["Paraguay", "Australia"]], [61, ["Norway", "France"]], [62, ["Senegal", "Iraq"]], [63, ["Cape Verde", "Saudi Arabia"]], [64, ["Uruguay", "Spain"]], [65, ["Egypt", "Iran"]], [66, ["New Zealand", "Belgium"]], [67, ["Panama", "England"]], [68, ["Croatia", "Ghana"]], [69, ["Colombia", "Portugal"]], [70, ["DR Congo", "Uzbekistan"]], [71, ["Algeria", "Austria"]], [72, ["Jordan", "Argentina"]]]
# football-data.org name (normalized) -> canonical name on the page
ALIAS = {"unitedstates": "USA", "usa": "USA", "unitedstatesofamerica": "USA", "korearepublic": "South Korea", "southkorea": "South Korea", "korea": "South Korea", "republicofkorea": "South Korea", "turkey": "Türkiye", "turkiye": "Türkiye", "iriran": "Iran", "iran": "Iran", "islamicrepublicofiran": "Iran", "cotedivoire": "Ivory Coast", "ivorycoast": "Ivory Coast", "congodr": "DR Congo", "drcongo": "DR Congo", "democraticrepublicofthecongo": "DR Congo", "democraticrepublicofcongo": "DR Congo", "congo": "DR Congo", "caboverde": "Cape Verde", "capeverde": "Cape Verde", "bosniaandherzegovina": "Bosnia & Herzegovina", "bosniaherzegovina": "Bosnia & Herzegovina", "bosnia": "Bosnia & Herzegovina", "czechrepublic": "Czechia", "czechia": "Czechia", "curacao": "Curaçao"}

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
    if n in ALIAS: return ALIAS[n]
    if n in CANON_NORM: return CANON_NORM[n]
    for cn, canon in CANON_NORM.items():       # fuzzy: one contains the other
        if cn and (cn in n or n in cn): return canon
    return None

STATUS_MAP = {"AWARDED": "FINISHED"}

def build_scores(api_matches):
    """Map football-data.org matches to our match numbers. Returns (scores, unresolved)."""
    scores, unresolved = {}, set()
    for m in api_matches:
        h = resolve_team((m.get("homeTeam") or {}).get("name", ""))
        a = resolve_team((m.get("awayTeam") or {}).get("name", ""))
        if not h or not a:
            for raw, got in ((m.get("homeTeam") or {}).get("name"), h), ((m.get("awayTeam") or {}).get("name"), a):
                if raw and not got: unresolved.add(raw)
            continue
        no = PAIR_LOOKUP.get(frozenset((h, a)))   # only group games map here
        if not no: continue
        ft = (m.get("score") or {}).get("fullTime") or {}
        gh, ga = ft.get("home"), ft.get("away")
        status = STATUS_MAP.get(m.get("status"), m.get("status"))
        if gh is None or ga is None: continue
        if status not in ("FINISHED", "IN_PLAY", "PAUSED"): continue
        scores[str(no)] = {"h": gh, "a": ga, "s": status}
    return scores, unresolved

def main():
    key = os.environ.get("FOOTBALL_DATA_API_KEY")
    if not key:
        print("ERROR: set FOOTBALL_DATA_API_KEY", file=sys.stderr); sys.exit(1)
    req = urllib.request.Request(API_URL, headers={"X-Auth-Token": key})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            payload = json.load(r)
    except Exception as e:
        # Don't overwrite a good file if the feed is down — just exit cleanly.
        print(f"Fetch failed ({e}); leaving existing scores.json untouched.", file=sys.stderr)
        sys.exit(0)
    api_matches = payload.get("matches", [])
    scores, unresolved = build_scores(api_matches)
    if unresolved:
        print("Unmatched team names (add to ALIAS):", sorted(unresolved), file=sys.stderr)
    out = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "football-data.org",
        "scores": scores,
    }
    with open("scores.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=0)
    print(f"Wrote scores.json with {len(scores)} live/final group results from {len(api_matches)} feed matches.")

if __name__ == "__main__":
    main()
