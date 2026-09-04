#!/usr/bin/env python3
"""
Health check for the World Cup 2026 site + live layer.

Pings the live site, the Cloudflare Worker (health, scores, admin lockdown),
the API-Football plan/quota, and the scores.json feed freshness. Prints a
markdown report, writes it to report.md (and to $GITHUB_STEP_SUMMARY in CI),
and exits non-zero on a hard failure so the GitHub Action fails + alerts.

Run locally any time:  python3 healthcheck.py
"""
import urllib.request
import urllib.error
import json
import datetime
import os
import time
import sys

WORKER = "https://wc2026-api.footy26.workers.dev"
SITE = "https://danpune.github.io/worldcup2026"
FINAL = datetime.date(2026, 7, 19)                       # World Cup 2026 final
HEADERS = {"User-Agent": "wc2026-healthcheck/1.0"}        # avoid Cloudflare bot-block (default UA gets 403)

fails, warns, oks = [], [], []


def fetch(url, timeout=25):
    req = urllib.request.Request(url + ("?cb=%d" % int(time.time())), headers=HEADERS)
    r = urllib.request.urlopen(req, timeout=timeout)
    return r.getcode(), r.read().decode("utf-8", "replace")


# 1) Site up
try:
    code, _ = fetch(SITE + "/")
    (oks if code == 200 else fails).append("Site homepage HTTP %s" % code)
except Exception as e:
    fails.append("Site unreachable: %s" % e)

# 2) Worker up
try:
    code, _ = fetch(WORKER + "/")
    (oks if code == 200 else fails).append("Worker health HTTP %s" % code)
except Exception as e:
    fails.append("Worker unreachable: %s" % e)

# 3) Scores payload sane
try:
    _, body = fetch(WORKER + "/scores")
    d = json.loads(body)
    errs = d.get("errors")
    if errs and isinstance(errs, dict) and "rateLimit" in errs:
        warns.append("Worker /scores hit a transient API per-minute rate limit (self-resets): %s" % errs["rateLimit"])
    elif errs:
        fails.append("Worker /scores errors: %s" % errs)
    elif not d.get("count"):
        fails.append("Worker /scores returned 0 fixtures")
    else:
        oks.append("Worker /scores: %s fixtures" % d["count"])
except Exception as e:
    fails.append("Worker /scores failed: %s" % e)

# 4) Admin routes still locked (security regression check)
try:
    urllib.request.urlopen(urllib.request.Request(WORKER + "/testpush", headers=HEADERS), timeout=20)
    fails.append("SECURITY: /testpush returned 200 without a key (should be 403)")
except urllib.error.HTTPError as e:
    (oks if e.code == 403 else fails).append("/testpush no-key HTTP %s (want 403)" % e.code)
except Exception as e:
    warns.append("/testpush check inconclusive: %s" % e)

# 5) API-Football plan + quota
try:
    _, body = fetch(WORKER + "/status")
    parsed = json.loads(body)
    r = parsed.get("response")
    if not isinstance(r, dict):
        # When rate-limited/errored, API-Football returns response as [] — skip plan/quota this run.
        raise ValueError("status unavailable (%s)" % (parsed.get("errors") or "empty response"))
    sub, req = r.get("subscription", {}), r.get("requests", {})
    cur, lim = req.get("current", 0), req.get("limit_day", 0) or 0
    if not sub.get("active"):
        # Accepted state since Jul 14 2026: the plan lapsed mid-tournament by choice;
        # fetch_scores.py carries scores via the ESPN fallback. Warn, don't page.
        warns.append("API-Football plan inactive (accepted — ESPN fallback carries scores)")
    else:
        oks.append("API-Football plan active (%s)" % sub.get("plan"))
    if lim:
        pct = cur / lim
        msg = "API quota %s/%s (%.0f%%)" % (cur, lim, pct * 100)
        (fails if pct > 0.97 else warns if pct > 0.85 else oks).append(msg)
    end = (sub.get("end") or "")[:10]
    if end:
        try:
            d2 = datetime.date.fromisoformat(end)
            if d2 < FINAL:
                warns.append("API plan ends %s — %d days BEFORE the final (renew/extend!)" % (end, (FINAL - d2).days))
            else:
                oks.append("API plan covers the tournament (ends %s)" % end)
        except Exception:
            pass
except Exception as e:
    warns.append("API /status check failed: %s" % e)

# 6) Scores feed freshness (the GitHub Action that writes scores.json)
try:
    _, body = fetch(SITE + "/scores.json")
    _sj = json.loads(body)
    # The 6 Aug 2026 incident (a partial fetch deleted matches 101-104, wiping the champion
    # from the live site) passed every check here because they only looked at freshness.
    _sc = _sj.get("scores") or {}
    _fin = sum(1 for v in _sc.values() if v.get("s") == "FINISHED")
    if datetime.date.today() > FINAL and (_fin < 104 or "104" not in _sc):
        fails.append("scores.json INCOMPLETE: %d finished, final present=%s (expected 104)"
                     % (_fin, "104" in _sc))
    else:
        oks.append("scores.json complete (%d finished results)" % _fin)
    u = _sj.get("updated")
    if u:
        age = (datetime.datetime.now(datetime.timezone.utc)
               - datetime.datetime.fromisoformat(u.replace("Z", "+00:00"))).total_seconds() / 60
        if datetime.date.today() > FINAL:
            # Tournament over: the feed is deliberately frozen (score cron retired), so a stale
            # file is the correct state, not an incident. Report it without raising a warning.
            oks.append("scores.json final (%.0f days after the final)" % (age / 1440))
        else:
            (warns if age > 120 else oks).append("scores.json updated %.0f min ago" % age)
except Exception as e:
    warns.append("scores.json freshness check failed: %s" % e)

# 7) Cloudflare Workers KV usage — only runs if CLOUDFLARE_API_TOKEN + CLOUDFLARE_ACCOUNT_ID are set
# (read-only token). A self-controlled, spoof-proof read of the REAL number the phishing email lied about.
_cf_token = os.environ.get("CLOUDFLARE_API_TOKEN")
_cf_acct = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
if _cf_token and _cf_acct:
    try:
        _now = datetime.datetime.now(datetime.timezone.utc)
        _since = _now.replace(hour=0, minute=0, second=0, microsecond=0)
        _q = {"query": "query($a:String!,$s:Time!,$u:Time!){viewer{accounts(filter:{accountTag:$a}){"
                       "kvOperationsAdaptiveGroups(limit:100,filter:{datetime_geq:$s,datetime_leq:$u}){"
                       "count dimensions{actionType}}}}}",
              "variables": {"a": _cf_acct,
                            "s": _since.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "u": _now.strftime("%Y-%m-%dT%H:%M:%SZ")}}
        _req = urllib.request.Request("https://api.cloudflare.com/client/v4/graphql",
                                      data=json.dumps(_q).encode(),
                                      headers={"Authorization": "Bearer " + _cf_token, "Content-Type": "application/json"})
        _resp = json.loads(urllib.request.urlopen(_req, timeout=25).read().decode())
        if _resp.get("errors"):
            warns.append("Cloudflare KV check: API returned errors (query may need tuning): %s" % str(_resp["errors"])[:200])
        else:
            _g = _resp["data"]["viewer"]["accounts"][0]["kvOperationsAdaptiveGroups"]
            _writes = sum(x["count"] for x in _g if x["dimensions"]["actionType"] in ("write", "delete", "list"))
            _reads = sum(x["count"] for x in _g if x["dimensions"]["actionType"] == "read")
            _pct = _writes / 1000.0 * 100
            _msg = "Cloudflare KV today: %d writes (%.0f%% of 1000 free) · %d reads" % (_writes, _pct, _reads)
            (fails if _pct > 97 else warns if _pct > 80 else oks).append(_msg)
    except Exception as e:
        warns.append("Cloudflare KV check failed (query may need tuning): %s" % e)

# 8) Calendar buttons use resolved team names, not raw knockout placeholders
# (regression guard: gcalUrl/icsLink once read m.home/m.away directly, which for
# knockout matches is static placeholder text like "Winner M89" that never resolves —
# real fix passes the already-resolved dh.name/da.name from dispTeam(). See git log.)
try:
    _, body = fetch(SITE + "/")
    if "gcalUrl(m,dh.name,da.name)" in body and "icsLink(m,dh.name,da.name)" in body:
        oks.append("Calendar buttons pass resolved team names")
    else:
        fails.append("REGRESSION: calendar buttons no longer pass resolved team names "
                     "(gcalUrl/icsLink call site changed — check for raw m.home/m.away)")
except Exception as e:
    warns.append("Calendar team-name check failed: %s" % e)

# Report
status = "FAIL" if fails else ("WARN" if warns else "OK")
lines = ["## Health check: **%s** — %sZ" % (status, datetime.datetime.utcnow().isoformat(timespec="seconds")), ""]
lines += ["- ❌ %s" % x for x in fails]
lines += ["- ⚠️ %s" % x for x in warns]
lines += ["- ✅ %s" % x for x in oks]
report = "\n".join(lines)
print(report)
with open("report.md", "w") as f:
    f.write(report)
summary = os.environ.get("GITHUB_STEP_SUMMARY")
if summary:
    with open(summary, "a") as f:
        f.write(report + "\n")
sys.exit(1 if fails else 0)
