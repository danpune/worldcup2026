#!/usr/bin/env python3
"""Build squads.json from Wikipedia's "2026 FIFA World Cup squads" page.

One-time / occasional run — WC squads are fixed once announced. Output is served
statically from GitHub Pages (no API key, no per-request cost). Re-run only if a
squad is officially revised. ponytail: static file beats 52 API calls per match view.

  python3 build_squads.py        # writes squads.json
"""
import json, re, sys, urllib.request

WIKI = ("https://en.wikipedia.org/w/api.php?action=parse"
        "&page=2026_FIFA_World_Cup_squads&prop=wikitext&format=json")

# Wikipedia team-section name -> the name this site uses (FLAGMAP keys). Only the
# ones that differ are listed; anything else passes through unchanged.
TEAM_RENAME = {
    "Czech Republic": "Czechia",
    "Bosnia and Herzegovina": "Bosnia & Herzegovina",
    "United States": "USA",
    "Turkey": "Türkiye",
    "Ivory Coast": "Ivory Coast",
    "DR Congo": "DR Congo",
    "Cape Verde": "Cape Verde",
}
# Wikipedia position code -> the label the site's squad renderer groups by.
POS = {"GK": "Goalkeeper", "DF": "Defender", "MF": "Midfielder", "FW": "Attacker"}

# Section headers that are NOT teams (group dividers + the trailing statistics tables).
NOT_TEAMS = {"Group A","Group B","Group C","Group D","Group E","Group F",
             "Group G","Group H","Group I","Group J","Group K","Group L",
             "Notes","References","See also","Age","Average age of squads",
             "Player representation by club","Player representation by league system",
             "Player representation by club confederation","Coach representation by country"}

# FIFA/IOC 3-letter club-country code -> (flag emoji, short country name).
# Covers the leagues WC players actually play in; unknown codes fall back to the raw code.
CLUBNAT = {
 "ENG":("🏴󠁧󠁢󠁥󠁮󠁧󠁿","England"),"ESP":("🇪🇸","Spain"),"GER":("🇩🇪","Germany"),"ITA":("🇮🇹","Italy"),
 "FRA":("🇫🇷","France"),"NED":("🇳🇱","Netherlands"),"POR":("🇵🇹","Portugal"),"USA":("🇺🇸","USA"),
 "KSA":("🇸🇦","Saudi Arabia"),"TUR":("🇹🇷","Türkiye"),"BEL":("🇧🇪","Belgium"),"SCO":("🏴󠁧󠁢󠁳󠁣󠁴󠁿","Scotland"),
 "MEX":("🇲🇽","Mexico"),"BRA":("🇧🇷","Brazil"),"ARG":("🇦🇷","Argentina"),"GRE":("🇬🇷","Greece"),
 "RUS":("🇷🇺","Russia"),"UKR":("🇺🇦","Ukraine"),"AUT":("🇦🇹","Austria"),"SUI":("🇨🇭","Switzerland"),
 "SCG":("🇷🇸","Serbia"),"SRB":("🇷🇸","Serbia"),"CRO":("🇭🇷","Croatia"),"DEN":("🇩🇰","Denmark"),
 "SWE":("🇸🇪","Sweden"),"NOR":("🇳🇴","Norway"),"POL":("🇵🇱","Poland"),"CZE":("🇨🇿","Czechia"),
 "JPN":("🇯🇵","Japan"),"KOR":("🇰🇷","South Korea"),"CHN":("🇨🇳","China"),"QAT":("🇶🇦","Qatar"),
 "UAE":("🇦🇪","UAE"),"EGY":("🇪🇬","Egypt"),"MAR":("🇲🇦","Morocco"),"RSA":("🇿🇦","South Africa"),
 "AUS":("🇦🇺","Australia"),"CAN":("🇨🇦","Canada"),"COL":("🇨🇴","Colombia"),"URU":("🇺🇾","Uruguay"),
 "CHI":("🇨🇱","Chile"),"ECU":("🇪🇨","Ecuador"),"PAR":("🇵🇾","Paraguay"),"IRN":("🇮🇷","Iran"),
 "IRQ":("🇮🇶","Iraq"),"CYP":("🇨🇾","Cyprus"),"ROU":("🇷🇴","Romania"),"HUN":("🇭🇺","Hungary"),
 "BUL":("🇧🇬","Bulgaria"),"ISR":("🇮🇱","Israel"),"FIN":("🇫🇮","Finland"),"SVK":("🇸🇰","Slovakia"),
 "SVN":("🇸🇮","Slovenia"),"NZL":("🇳🇿","New Zealand"),"IND":("🇮🇳","India"),"THA":("🇹🇭","Thailand"),
 "AZE":("🇦🇿","Azerbaijan"),"KAZ":("🇰🇿","Kazakhstan"),"UZB":("🇺🇿","Uzbekistan"),"TUN":("🇹🇳","Tunisia"),
 "ALG":("🇩🇿","Algeria"),"DZA":("🇩🇿","Algeria"),"SEN":("🇸🇳","Senegal"),"CIV":("🇨🇮","Ivory Coast"),
 "GHA":("🇬🇭","Ghana"),"NGA":("🇳🇬","Nigeria"),"COD":("🇨🇩","DR Congo"),"ANG":("🇦🇴","Angola"),
 "JOR":("🇯🇴","Jordan"),"CPV":("🇨🇻","Cape Verde"),"PAN":("🇵🇦","Panama"),"CRC":("🇨🇷","Costa Rica"),
 "VEN":("🇻🇪","Venezuela"),"PER":("🇵🇪","Peru"),"BOL":("🇧🇴","Bolivia"),"HAI":("🇭🇹","Haiti"),
 "CUW":("🇨🇼","Curaçao"),"SAU":("🇸🇦","Saudi Arabia"),"GEO":("🇬🇪","Georgia"),"ARM":("🇦🇲","Armenia"),
 "BIH":("🇧🇦","Bosnia & Herzegovina"),"HON":("🇭🇳","Honduras"),"IDN":("🇮🇩","Indonesia"),
 "IRL":("🇮🇪","Ireland"),"MAS":("🇲🇾","Malaysia"),"WAL":("🏴󠁧󠁢󠁷󠁬󠁳󠁿","Wales"),
}

def parse_template(block):
    """Pull |key=value pairs out of one nat-fs-g-player body. Splits on | only at the
    top level — the nested age={{birth date and age2|...}} template's pipes are skipped
    by tracking both [[ ]] and {{ }} depth."""
    out = {}
    parts, bdepth, cdepth, cur = [], 0, 0, ""
    for ch in block:
        if ch == "[": bdepth += 1
        elif ch == "]": bdepth = max(0, bdepth-1)
        elif ch == "{": cdepth += 1
        elif ch == "}": cdepth = max(0, cdepth-1)
        if ch == "|" and bdepth == 0 and cdepth == 0:
            parts.append(cur); cur = ""
        else:
            cur += ch
    parts.append(cur)
    for p in parts:
        if "=" in p:
            k, v = p.split("=", 1)
            out[k.strip()] = v.strip()
    return out

def extract_templates(wt):
    """Yield (start_index, body) for every {{nat fs g player ... }} in wt, matching the
    template's own closing braces (one level of nested {{ }} is handled)."""
    needle = "{{nat fs g player"
    pos = 0
    while True:
        i = wt.find(needle, pos)
        if i < 0:
            return
        # walk forward tracking brace depth from the opening {{
        depth, j = 0, i
        while j < len(wt):
            if wt[j:j+2] == "{{": depth += 1; j += 2; continue
            if wt[j:j+2] == "}}":
                depth -= 1; j += 2
                if depth == 0: break
                continue
            j += 1
        body = wt[i+len(needle):j-2]   # strip the leading template name and trailing }}
        yield i, body
        pos = j

def clean_name(raw):
    """[[Yassine Bounou|Bounou]] -> Bounou ; [[Matěj Kovář]] -> Matěj Kovář ; strip refs/markup."""
    raw = re.sub(r"<ref.*?</ref>", "", raw, flags=re.S)
    raw = re.sub(r"<ref[^>]*/>", "", raw)
    m = re.search(r"\[\[([^\]]+)\]\]", raw)
    if m:
        inner = m.group(1)
        raw = inner.split("|")[-1] if "|" in inner else inner
    return raw.replace("'''", "").strip()

def main():
    req = urllib.request.Request(WIKI, headers={"User-Agent": "wc2026-squads-build/1.0"})
    wt = json.load(urllib.request.urlopen(req, timeout=30))["parse"]["wikitext"]["*"]

    squads, unknown_nat = {}, set()
    # Index every team-section header (=== Team ===) by position, then assign each player
    # template to whichever header most recently precedes it.
    headers = []  # (start_index, site_team_name) — None name means "not a team"
    for m in re.finditer(r"^===\s*([^=]+?)\s*===\s*$", wt, re.M):
        nm = m.group(1).strip()
        headers.append((m.start(), None if nm in NOT_TEAMS else TEAM_RENAME.get(nm, nm)))

    def team_for(idx):
        team = None
        for start, nm in headers:
            if start <= idx:
                team = nm
            else:
                break
        return team

    for idx, body in extract_templates(wt):
        team = team_for(idx)
        if not team:
            continue
        t = parse_template(body)
        name = clean_name(t.get("name", ""))
        if not name:
            continue
        club = clean_name(t.get("club", "")) or "—"
        cn = (t.get("clubnat", "") or "").upper()
        if cn and cn not in CLUBNAT:
            unknown_nat.add(cn)
        flag, country = CLUBNAT.get(cn, ("", cn))
        squads.setdefault(team, []).append({
            "no": int(t["no"]) if t.get("no", "").isdigit() else None,
            "name": name,
            "pos": POS.get(t.get("pos", "").upper(), t.get("pos", "")),
            "club": club,
            "cflag": flag,
            "cnat": country,
        })

    # Sanity: every team should have a full squad (23–26). Surface anything thin.
    thin = {k: len(v) for k, v in squads.items() if len(v) < 18}
    print(f"Teams: {len(squads)} | total players: {sum(len(v) for v in squads.values())}")
    if thin:
        print("WARN thin squads:", thin, file=sys.stderr)
    if unknown_nat:
        print("WARN unmapped club-country codes (will show raw code):", sorted(unknown_nat), file=sys.stderr)

    out = {"source": "en.wikipedia.org/wiki/2026_FIFA_World_Cup_squads", "teams": squads}
    with open("squads.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print("Wrote squads.json")

    # ponytail self-check: a known team parses with clubs attached.
    assert len(squads) >= 40, "expected ~48 teams"
    sample = squads.get("Morocco") or next(iter(squads.values()))
    assert sample and sample[0]["name"] and sample[0]["club"], "player rows must have name+club"

if __name__ == "__main__":
    main()
