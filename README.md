# World Cup 2026 — live schedule, standings & calendar page

A single web page that shows the full schedule in **your** timezone, a **Standings** table that
recalculates as results come in, and an **Add to Google Calendar** button on every match.
Scores update automatically from a football data feed and are shown read-only.

You don't need to write any code. Just put these files in a GitHub repo and turn two switches on.

## What each file is

| File | What it does | Where it goes |
|---|---|---|
| `index.html` | The web page itself | repo root |
| `scores.json` | The live scores the page reads (starts empty; the Action overwrites it) | repo root |
| `fetch_scores.py` | Pulls scores from football-data.org and writes `scores.json` | repo root |
| `update-scores.yml` | The scheduled job that runs the fetcher | **`.github/workflows/update-scores.yml`** |

## One-time setup (about 10 minutes)

### 1. Get a free scores key
- Sign up at **https://www.football-data.org/client/register** (free tier).
- Copy the **API token** they email/show you. Keep it handy.

### 2. Make a GitHub repo
- New repo → name it e.g. `worldcup2026` → **Public** → Create.
- Upload `index.html`, `scores.json`, and `fetch_scores.py` (drag-and-drop on the repo page → Commit).
- Add the workflow: **Add file → Create new file**, and for the filename type exactly:
  `.github/workflows/update-scores.yml` — paste the contents of `update-scores.yml` → Commit.
  (Typing the slashes creates the folders for you.)

### 3. Store your key as a secret (so it's never shown on the page)
- Repo **Settings → Secrets and variables → Actions → New repository secret**.
- Name: `FOOTBALL_DATA_API_KEY`  ·  Value: *your token from step 1* → Add secret.

### 4. Turn on the page (GitHub Pages)
- **Settings → Pages → Build and deployment → Source: Deploy from a branch → Branch: `main` / `root`** → Save.
- After a minute your page is live at `https://<your-username>.github.io/worldcup2026/`. That's the link you share.

### 5. Run the scores job once
- **Actions** tab → if prompted, enable workflows → pick **Update World Cup scores** → **Run workflow**.
- It runs in ~30s and commits a fresh `scores.json`. After that it runs itself every ~10 minutes.

Done. Open your Pages link on your phone — flags, your-timezone times, calendar buttons, live standings.

## How "live" works
- The job fetches **final and in-play** group scores. Final scores feed the standings; in-play games show a **LIVE** badge but only count once full-time.
- The bar at the top of the page shows the source and **when it last updated**.
- Scores are **read-only** — they come straight from the feed; there's no manual editing. If the feed is ever down, the job never overwrites a good `scores.json` with a failed fetch, so the last known scores stay put.

## Good to know / troubleshooting
- **Refresh cadence:** every ~10 min (GitHub's scheduler can add a few minutes' delay under load). You can lower it in `update-scores.yml` (`cron`), but football-data.org's free tier allows ~10 calls/minute, so don't go below ~5 min.
- **403 / no data:** the free tier uses competition code `WC`. If you get a 403, confirm the World Cup is enabled on your football-data.org dashboard.
- **Knockout scores:** the standings are group-stage (that's where a points table applies). Knockout team names appear once decided; auto-scoring those is a later add-on.
- **Hosting elsewhere:** you can serve the page on Netlify instead, but keep the repo on GitHub so the Action can write `scores.json`. GitHub Pages is simplest because it's all in one place.

## Sources
- **Official (source of truth):** FIFA — https://www.fifa.com/en/tournaments/mens/worldcup/canadamexicousa2026
- **Live scores feed:** football-data.org — https://www.football-data.org/
- **Schedule curated from:** NBC Sports, cross-checked against World Cup Wiki and FIFA host-city sites (Dallas, NY/NJ, Atlanta).
- **TV (USA):** FOX Sports — https://www.foxsports.com/soccer/fifa-world-cup

Not affiliated with FIFA. For official confirmation of any result, check fifa.com.
