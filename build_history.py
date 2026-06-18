#!/usr/bin/env python3
"""
Build wc-history.json — a compact men's-World-Cup history file used by the site
for all-time head-to-head records and per-team history cards.

Data source: The Fjelstul World Cup Database (jfjelstul/worldcup), licensed
CC-BY-SA 4.0 (c) Joshua C. Fjelstul, Ph.D. We redistribute a derived subset, so
the output carries the same attribution + license note (see ATTRIBUTION below)
and the site shows a credit in the footer. ShareAlike: this derived data file is
likewise CC-BY-SA 4.0.

Reproducible: downloads the three CSVs it needs and writes wc-history.json.
Run:  python3 build_history.py
"""
import csv
import io
import json
import urllib.request
from collections import defaultdict

BASE = "https://raw.githubusercontent.com/jfjelstul/worldcup/master/data-csv"
ATTRIBUTION = ("Historical World Cup data: The Fjelstul World Cup Database "
               "(github.com/jfjelstul/worldcup), (c) Joshua C. Fjelstul, Ph.D., "
               "CC-BY-SA 4.0.")

# stage_name (raw) -> short code shown on the site
STAGE = {
    "group stage": "GS", "second group stage": "G2", "final round": "FR",
    "round of 16": "R16", "quarter-finals": "QF", "quarter-final": "QF",
    "semi-finals": "SF", "semi-final": "SF", "third-place match": "3P", "final": "F",
}
# how "deep" a result is, for computing a team's best-ever finish (higher = better)
FINISH_RANK = {  # from final tournament standings (top 4)
    "Champions": 100, "Runners-up": 90, "Third place": 80, "Fourth place": 70}
STAGE_RANK = {   # fallback: deepest stage a team played in a tournament
    "F": 95, "SF": 75, "QF": 60, "G2": 50, "R16": 45, "FR": 55, "GS": 10}
STAGE_LABEL = {
    "SF": "Semi-finals", "QF": "Quarter-finals", "R16": "Round of 16",
    "G2": "2nd group stage", "FR": "Final round", "GS": "Group stage"}
POS_LABEL = {1: "Champions", 2: "Runners-up", 3: "Third place", 4: "Fourth place"}


def load(name):
    with urllib.request.urlopen("%s/%s.csv" % (BASE, name), timeout=60) as r:
        return list(csv.DictReader(io.StringIO(r.read().decode("utf-8"))))


def is_mens(row):
    return "Men's" in row.get("tournament_name", "")


def main():
    matches = [r for r in load("matches") if is_mens(r) and r["replay"] != "1"]
    standings = [r for r in load("tournament_standings") if is_mens(r)]
    tournaments = [t for t in load("tournaments") if is_mens(t)]

    # canonical team name per code (from matches; codes are stable FIFA-style)
    name = {}
    for r in matches:
        name.setdefault(r["home_team_code"], r["home_team_name"])
        name.setdefault(r["away_team_code"], r["away_team_name"])

    # per (team, tournament) best result, for best-finish + appearances
    apps = defaultdict(set)            # code -> {year, ...}
    deepest = defaultdict(dict)        # code -> {year: best STAGE_RANK seen}
    deepest_stage = defaultdict(dict)  # code -> {year: short stage}
    out_matches = []
    for r in matches:
        try:
            year = int(r["tournament_id"].split("-")[1])
        except (IndexError, ValueError):
            continue
        st = STAGE.get(r["stage_name"], "GS")
        h, a = r["home_team_code"], r["away_team_code"]
        try:
            hs, as_ = int(r["home_team_score"]), int(r["away_team_score"])
        except ValueError:
            continue
        pen = 1 if r["penalty_shootout"] == "1" else 0
        out_matches.append([year, st, h, a, hs, as_, pen])
        for code in (h, a):
            apps[code].add(year)
            if STAGE_RANK.get(st, 0) > deepest[code].get(year, 0):
                deepest[code][year] = STAGE_RANK.get(st, 0)
                deepest_stage[code][year] = st

    # final-standings finishes (top 4) override stage-based finish
    finish = defaultdict(dict)  # code -> {year: (rank, label)}
    titles = defaultdict(int)
    finals = defaultdict(int)
    for s in standings:
        code = s["team_code"]
        try:
            year = int(s["tournament_id"].split("-")[1]); pos = int(s["position"])
        except (IndexError, ValueError):
            continue
        label = POS_LABEL.get(pos)
        if not label:
            continue
        finish[code][year] = (FINISH_RANK[label], label)
        if pos == 1:
            titles[code] += 1
        if pos <= 2:
            finals[code] += 1

    teams = {}
    for code in apps:
        # best finish across all tournaments: prefer standings, else deepest stage
        best_rank, best_label, best_year = -1, None, None
        for year in apps[code]:
            if year in finish[code]:
                rank, label = finish[code][year]
            else:
                st = deepest_stage[code].get(year, "GS")
                rank, label = STAGE_RANK.get(st, 0), STAGE_LABEL.get(st, "Group stage")
            if rank > best_rank:
                best_rank, best_label, best_year = rank, label, year
        teams[code] = {
            "name": name.get(code, code),
            "apps": len(apps[code]),
            "titles": titles[code],
            "finals": finals[code],
            "best": best_label,
            "bestYear": best_year,
            "last": max(apps[code]),
        }

    payload = {
        "attribution": ATTRIBUTION,
        "license": "CC-BY-SA-4.0",
        "source": "https://github.com/jfjelstul/worldcup",
        "tournaments": len(tournaments),
        "matchCount": len(out_matches),
        "teams": teams,
        "matches": out_matches,
    }
    with open("wc-history.json", "w") as f:
        json.dump(payload, f, separators=(",", ":"), ensure_ascii=False)
    print("wrote wc-history.json: %d men's matches, %d teams, %d tournaments"
          % (len(out_matches), len(teams), len(tournaments)))


if __name__ == "__main__":
    main()
