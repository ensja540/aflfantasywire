# AFLFantasyWire — Onboarding / Project Brief

An AFL Fantasy / SuperCoach web app (live news, prices, stats, form, injuries) plus an
automated X/Twitter presence.

## Repo / deploy
- Local: `C:\aflfantasywire` (Windows, git). GitHub: `ensja540/aflfantasywire`, branch `main`.
- Hosting: Cloudflare Worker, **auto-deploys on push to `main`**. Live at
  `https://aflfantasywire.com` and `https://aflfantasywire.ensor-jack.workers.dev`
  (both serve the app; `www.` does NOT resolve).
- `worker.js` serves the static assets (ASSETS binding) and proxies AI:
  `POST /api/ai` (Anthropic, key in the `ANTHROPIC_API_KEY` Cloudflare secret) and
  `POST /api/article-summary` (fetches an article + summarises). `wrangler.jsonc` is the config.

## Frontend — `index.html`
- A **single compiled/minified React bundle** (no JSX source in the repo). All UI edits are
  made by exact-string replacement directly in the minified file, then validated with `node`
  (parse each `<script>` via `vm.Script`). ~570 KB.
- Tabs: Home, Players, Prices, Form Guide, Waiver, Watchlist, AI Analyst, Tools
  (Tools + AI hidden on mobile nav). Tapping a player switches to the "feed"/player-detail
  view (component `F7`), which has a "Back to list" button + scroll restore.
- Theme: dark (default) + light mode via CSS vars; greens/reds are vars
  `--g/--r/--grn/--rd` that darken in light mode.
- Home right-panel embeds a **Twitter List timeline** (list id `233131558`) via a `TW`
  component that loads `widgets.js` and calls `twttr.widgets.load()`.
- News items show an AI summary (component `AS`) that uses a scrape-time `ai_full` summary
  or fetches `/api/article-summary` on expand.

## Data pipeline (run on a HOME machine — Footywire blocks cloud IPs)
- `fetch_data.py` → scrapes Footywire (SC/AFL Fantasy prices, break-evens, per-round +
  games-log scores) + AFL Fantasy Classic ownership → **`players.json`** (~595 players).
  Price history reconstructed from real scores; per-round scores span 8 rounds (byes kept
  as 0 for bar charts but excluded from averages).
- `news_scraper.py` (+ `news_filter.py`, `news_history.py`) → scrapes AFL.com.au RSS,
  AFL Medical Room, Footywire injuries, Google News, club pages → **`news.json`** (rolling
  archive). Features: rumour mill (`rumours.json`, topped up from injury-cloud players since
  Twitter reads are paywalled), feed-quality gate (drops season-ending/stale injuries + empty
  items), full-article AI summaries at scrape time, multi-player tagging via `find_players_all`
  against the full ~800 AFL roster, NewsHistory new/ongoing/resolved tracking. Google News
  items require a recent pubDate.
- `auto_scrape.py` = the home-machine loop: every 5 min runs both scrapers, commits/pushes
  `players.json`/`news.json`/etc., and runs `tweet_bot.py --auto`.

## Twitter/X integration
- `tweet_bot.py` generates ~5 brand-compliant tweets/day from the data (fixed numeric
  templates → no hallucination) and posts via the X v2 API (OAuth1; creds in repo-root
  `.env`: `X_CONSUMER_KEY/SECRET`, `X_BEARER_TOKEN`, `X_ACCESS_TOKEN/SECRET`).
  - `python tweet_bot.py` = preview; `--post` = publish; `--count=N` caps a run;
    `--auto` self-throttles to 5/day, spaced ~3h, **6am–11pm AEST** (log in `tweeted.json`,
    gitignored).
  - Account `@AFLFantasyWire`. X reads are paywalled (HTTP 402); posting works only with the
    app set to **Read and Write** and **regenerated** access tokens.
- Social share card: `og.png` (1200×630) + `twitter:card=summary_large_image` meta tags.

## Tweet brand rules (must follow)
- Always include `#SuperCoach #AFLFantasy`.
- Informative + light recommendation (no hard buy/sell, no slang like "bench-fodder").
- **Separate Classic** (price/break-even/ownership) **from Draft** (form/role only — no prices).
- **Never invent a cause or role** — only verifiable numbers (scores, 3-rd avg, season avg,
  break-even, price, ownership). e.g. data may tag a player "MID" but that is not a claim
  about minutes/role.
- Breaking only if genuinely fresh.
- Site-wide rule: **AFL only, never AFLW.**

## Key gotchas
- `index.html` is minified → edit by unique exact-string match; always `node`-validate before commit.
- Scrapers must run from home (Footywire blocks cloud IPs); the GitHub Actions scrape workflow is disabled.
- Cloudflare and X cache aggressively (hard-refresh; X cards re-scrape on a fresh share).
- Don't commit `.env` (gitignored) or `tweeted.json`. The Anthropic key is a Cloudflare secret, not in the repo.

## Common commands
```
venv/Scripts/python.exe fetch_data.py        # refresh players.json (home machine)
venv/Scripts/python.exe news_scraper.py      # refresh news.json
venv/Scripts/python.exe tweet_bot.py         # preview tweets
venv/Scripts/python.exe tweet_bot.py --post --count=1   # post one now
node -e '...vm.Script...'                     # validate the index.html bundle after edits
```

## Known open items
- Far-right table column alignment polish (`.ptable` on Players/Prices).
- Profile bio website link points at `www.aflfantasywire.com` (broken — needs de-`www` or a `www` DNS record).
- Twitter list / Inside-Scoop not shown on mobile (the right-panel is desktop-only).
