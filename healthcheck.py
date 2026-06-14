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
        fails.append("API-Football plan is NOT active")
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
    u = json.loads(body).get("updated")
    if u:
        age = (datetime.datetime.now(datetime.timezone.utc)
               - datetime.datetime.fromisoformat(u.replace("Z", "+00:00"))).total_seconds() / 60
        (warns if age > 120 else oks).append("scores.json updated %.0f min ago" % age)
except Exception as e:
    warns.append("scores.json freshness check failed: %s" % e)

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
