#!/usr/bin/env python3
"""
Are the tracker sites still updating?

Each tracker already emails when its own workflow fails. What nothing watched was
the opposite: the site still serving, still looking normal, quietly handing out old
data. epl-tracker did exactly that for 13 days in September 2026 (ESPN dropped
support for date ranges, the fetcher raised before the commit step), and the only
signal was a failure email nobody had to act on.

So this checks the published artefact, not the pipeline. It catches every way a
tracker can go quiet at once:
  - the fetcher crashes                (run fails, data frozen)
  - the cron never fires               (no run at all, nothing to fail)
  - a self-check rejects every tick    (run is green, nothing published)
  - a repo runs out of Actions minutes (job refused before it starts)

Run locally any time:  python3 staleness.py
"""
import datetime
import json
import os
import sys
import time
import urllib.request

# Hours of silence before a tracker is considered stale. Each fetches several times
# a day, but GitHub throttles low-traffic crons hard — wrexham's 30-minute schedule
# really fires every 2-5h. 12h is well clear of that and still catches an outage on
# day one rather than day 13.
STALE_H = 12

TRACKERS = [
    ("EPL",     "https://danpune.github.io/epl-tracker/data.json"),
    ("Cricket", "https://danpune.github.io/india-cricket-tracker/data.json"),
    ("Tennis",  "https://danpune.github.io/tennis-slams-tracker/data.json"),
    ("Wrexham", "https://danpune.github.io/wrexham-tracker/data.json"),
]

HEADERS = {"User-Agent": "tracker-staleness/1.0 (github.com/danpune)"}
rows, fails = [], []


def age_hours(url):
    """Hours since the live data.json was last written. Raises if it can't be read."""
    req = urllib.request.Request(url + "?cb=%d" % int(time.time()), headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        if r.getcode() != 200:
            raise RuntimeError("HTTP %s" % r.getcode())
        stamp = json.loads(r.read().decode("utf-8", "replace")).get("updated")
    if not stamp:
        raise RuntimeError("no 'updated' field")
    when = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    now = datetime.datetime.now(datetime.timezone.utc)
    # a stamp from the future means a clock or format problem, not freshness
    if when > now + datetime.timedelta(minutes=5):
        raise RuntimeError("updated is in the future: %s" % stamp)
    return (now - when).total_seconds() / 3600, stamp


for name, url in TRACKERS:
    try:
        hours, stamp = age_hours(url)
    except Exception as e:                    # unreachable counts as broken, not skipped
        rows.append("| %s | unreadable | %s |" % (name, e))
        fails.append("%s: %s" % (name, e))
        continue
    stale = hours > STALE_H
    rows.append("| %s | %.1fh | %s |" % (name, hours, "STALE" if stale else "ok"))
    if stale:
        fails.append("%s has not updated for %.1fh (last %s)" % (name, hours, stamp))

report = ["# Tracker staleness — %s" % datetime.datetime.now(datetime.timezone.utc)
          .strftime("%Y-%m-%d %H:%MZ"),
          "", "Stale after %dh of no new data." % STALE_H, "",
          "| Tracker | Data age | |", "|---|---|---|"] + rows
if fails:
    report += ["", "## Not updating", ""] + ["- %s" % f for f in fails]
text = "\n".join(report) + "\n"

print(text)
with open("report.md", "w") as f:
    f.write(text)
summary = os.environ.get("GITHUB_STEP_SUMMARY")
if summary:
    with open(summary, "a") as f:
        f.write(text)

sys.exit(1 if fails else 0)
