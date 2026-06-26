#!/usr/bin/env python3
"""
AFLFantasyWire — Tweet bot
==========================
Generates ~5 brand-compliant AFL fantasy tweets from players.json / news.json
and posts them to X (Twitter).

BRAND RULES (enforced here, not free-text — so we can't hallucinate):
  - Every tweet ends with "#SuperCoach #AFLFantasy".
  - Tone: informative + a light steer ("one to keep an eye on", "worth a look").
  - No slang / dismissive terms.
  - Tweets are built ONLY from verifiable numbers (3-game / 5-game / season
    averages, last-N scoreline, breakeven, ownership, this-week matchup
    difficulty). We NEVER state a cause/role/why a score moved.
  - VARIETY is deliberate — the daily set rotates across angle FAMILIES and
    phrasings so the feed never reads like the same card three times:
       * Form trend (Classic/Draft) — STRICT gate, only two categories qualify:
            A. BREAKOUT — season avg < 80 AND both 3-game and 5-game avgs > 80
            B. DECLINE  — season avg > 80 AND both 3-game and 5-game avgs < 80
         Both windows on the same side of 80 = a sustained shift, not a spike.
         Headline and the stat block each rotate among several phrasings.
       * Matchup — this week's opponent difficulty (scheduleRating >=7 soft,
         <=4 tough), tied to our prediction model.
       * Value — SuperCoach cash: recent average vs breakeven (rising/dropping).
    No two tweets in a batch share an angle. (The consistency-rating footer and
    the standalone consistency feature were removed — they made every tweet look
    the same.)
  - CTA / round-recap SLOT (1 per day) — drives traffic to the site: a
    round-recap (top-10 scorers) when the round is complete, else a rank-callout
    for a top-100 in-form player. Mixed 1:2 with the varied angle tweets.
  - Breaking only when an item is genuinely fresh (NewsHistory status == "new").

USAGE
  python tweet_bot.py            # preview only (prints the 5 tweets, posts nothing)
  python tweet_bot.py --post     # generate AND post to X

Credentials come from repo-root .env:
  X_CONSUMER_KEY / X_CONSUMER_SECRET / X_ACCESS_TOKEN / X_ACCESS_TOKEN_SECRET
"""
import json, sys, random, subprocess
from pathlib import Path
from datetime import datetime
from collections import defaultdict

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = Path(__file__).parent
HASHTAGS = "#SuperCoach"
TWEETED_LOG = BASE / "tweeted.json"
DAILY_TARGET = 3

# Players we never write a tweet about (manual mute list). Matched on the
# lower-cased name, so spelling variants of the surname don't slip through.
TWEET_BLOCKLIST = {"darcy wilmot", "darcy wilmott"}


def _blocked(name):
    return (name or "").strip().lower() in TWEET_BLOCKLIST
# RISE_GAP / FALL_GAP no longer used — the trend gate is now an absolute
# threshold (both 3-game and 5-game on the same side of 80 as season-avg's
# inverse). Removed to keep the rules in one place.


def load_env():
    env = {}
    p = BASE / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _load(name, key):
    try:
        d = json.loads((BASE / name).read_text(encoding="utf-8"))
        return d.get(key, d) if isinstance(d, dict) else d
    except Exception:
        return []


def money(p):
    p = int(p or 0)
    if p >= 1_000_000:
        return f"${p/1_000_000:.1f}m".replace(".0m", "m")
    return f"${round(p/1000)}k"


def scoreline(scores, n=5):
    return "-".join(str(int(s)) for s in scores[-n:])


def played_scores(p):
    return [s for s in (p.get("scores") or []) if s and s > 0]


def _avg_n(ps, n):
    """Mean of the most recent n played scores (0 if fewer)."""
    s = ps[-n:]
    return round(sum(s) / len(s)) if s else 0


def classic_tweets(players):
    out = []
    for p in players:
        avg = p.get("scAvg") or 0
        avg3 = p.get("scAvg3") or 0
        own = p.get("owned") or 0
        consistency = int(p.get("consistency") or 0)
        ps = played_scores(p)
        # Need at least 5 played scores so avg5 is meaningful — otherwise
        # avg5 collapses to avg3 and the "both windows agree" gate is hollow.
        if not avg or len(ps) < 5:
            continue
        avg5 = _avg_n(ps, 5)
        l3 = scoreline(ps, 3)
        own_bit = f"\n{own}% owned" if own else ""
        # Strict trend gates — only two categories are tweet-worthy:
        #   A: BREAKOUT — season < 80 but BOTH 3-game and 5-game > 80
        #      (low-base player now producing consistently).
        #   B: DECLINE — season > 80 but BOTH 3-game and 5-game < 80
        #      (premium that's faded over a sustained window, not a one-week dip).
        if avg < 80 and avg3 > 80 and avg5 > 80:
            head = random.choice([
                f"\U0001F4C8 {p['name']} trending up",
                f"\U0001F4C8 {p['name']} in strong recent form",
                f"\U0001F4C8 {p['name']} lifting his scores",
            ])
            out.append(("classic", p["id"], "crise",
                        f"{head}\n\n{_form_body(p, 'up')}{own_bit}\n\n{HASHTAGS}"))
        elif avg > 80 and avg3 < 80 and avg5 < 80:
            head = random.choice([
                f"\U0001F4C9 {p['name']} cooling off",
                f"\U0001F4C9 {p['name']} down on recent form",
                f"\U0001F4C9 {p['name']} scoring below his season mark",
            ])
            out.append(("classic", p["id"], "cfall",
                        f"{head}\n\n{_form_body(p, 'down')}\n\n{HASHTAGS}"))
    return out


def draft_tweets(players):
    out = []
    for p in players:
        ps = played_scores(p)
        # Same minimum as classic — 5 played games so avg5 is real.
        if len(ps) < 5:
            continue
        avg = p.get("scAvg") or 0
        if not avg:
            continue
        avg3 = p.get("scAvg3") or 0
        avg5 = _avg_n(ps, 5)
        consistency = int(p.get("consistency") or 0)
        l5 = scoreline(ps, 5)
        # Same two-category gate as classic. The "consistent producer" (dcons)
        # template was removed — the brief is only A (breakout) or B (decline).
        if avg < 80 and avg3 > 80 and avg5 > 80:
            head = random.choice([
                f"\U0001F4C8 {p['name']} on the rise",
                f"\U0001F4C8 {p['name']} trending up",
                f"\U0001F4C8 {p['name']} climbing in recent weeks",
            ])
            out.append(("draft", p["id"], "drise",
                        f"{head}\n\n{_form_body(p, 'up')}\n\n{HASHTAGS}"))
        elif avg > 80 and avg3 < 80 and avg5 < 80:
            head = random.choice([
                f"\U0001F4C9 {p['name']}'s output has eased",
                f"\U0001F4C9 {p['name']} down in recent weeks",
                f"\U0001F4C9 {p['name']} scoring below his average",
            ])
            out.append(("draft", p["id"], "dfall",
                        f"{head}\n\n{_form_body(p, 'down')}\n\n{HASHTAGS}"))
    return out


ABBR_TO_TEAM = {
    "ADE": "Adelaide", "BRL": "Brisbane", "CAR": "Carlton", "COL": "Collingwood",
    "ESS": "Essendon", "FRE": "Fremantle", "GEE": "Geelong", "GCS": "Gold Coast",
    "GWS": "GWS", "HAW": "Hawthorn", "MEL": "Melbourne", "NTH": "North Melbourne",
    "PTA": "Port Adelaide", "RIC": "Richmond", "STK": "St Kilda", "SYD": "Sydney",
    "WCE": "West Coast", "WBD": "Western Bulldogs",
}


def _form_body(p, direction):
    """Varied presentation of recent form vs season avg so tweets don't all read
    identically. `direction` ('up'/'down') keeps the narrative correct. One
    rotation keeps the classic windows block; the others are sentence-led."""
    ps = played_scores(p)
    avg = round(p.get("scAvg") or 0)
    avg3 = round(p.get("scAvg3") or 0)
    avg5 = round(_avg_n(ps, 5))
    l3 = scoreline(ps, 3)
    moved = "up from" if direction == "up" else "down from"
    return random.choice([
        f"3-game avg: {avg3}SC | 5-game avg: {avg5}SC | Season: {avg}SC\nLast 3: {l3}",
        f"Last 3: {l3}\nThat's a {avg3} average, {moved} {avg} on the season.",
        f"Averaging {avg3} across his past three ({avg5} over five), {moved} a season mark of {avg}.",
    ])


def matchup_tweets(players):
    """This week's opponent difficulty, from scheduleOpp[0]/scheduleRating[0]
    (>=7 soft, <=4 tough). A fresh angle that ties straight to our predictions."""
    out = []
    for p in players:
        rng = p.get("scheduleRating") or []
        opp = p.get("scheduleOpp") or []
        avg3 = round(p.get("scAvg3") or 0)
        rank = p.get("rank") or 999
        if not rng or not opp or rank > 120 or avg3 < 70:
            continue
        r0 = rng[0]
        opp_full = ABBR_TO_TEAM.get(opp[0], opp[0])
        pos = (p.get("pos") or "MID")
        if r0 >= 7:
            head = random.choice([
                f"{p['name']} faces {opp_full} next round, one of the easier matchups for {pos}s on our ratings.",
                f"Favourable draw for {p['name']}: {opp_full} next round rates among the friendlier {pos} matchups on our numbers.",
                f"{p['name']} draws {opp_full} next — one of the softer {pos} matchups our ratings flag.",
            ])
        elif r0 <= 4:
            head = random.choice([
                f"{p['name']} faces {opp_full} next round, one of the tougher matchups for {pos}s on our ratings.",
                f"Tough draw for {p['name']}: {opp_full} next round rates among the harder {pos} matchups on our numbers.",
                f"{p['name']} runs into {opp_full} next — one of the stingier {pos} matchups our ratings flag.",
            ])
        else:
            continue
        body = random.choice([
            f"He's averaged {avg3}SC over his past three.",
            f"He's been going at {avg3}SC across his past three.",
            f"Recent form: {avg3}SC over the past three rounds.",
        ])
        out.append(("matchup", p["id"], "mtup", f"{head}\n\n{body}\n\n{HASHTAGS}"))
    return out


def value_tweets(players):
    """SuperCoach cash angle from breakeven vs recent average (Classic only).
    Recent output well clear of breakeven = price rising; well short = drop risk."""
    out = []
    for p in players:
        be = p.get("breakeven")
        avg3 = round(p.get("scAvg3") or 0)
        rank = p.get("rank") or 999
        price = p.get("price") or 0
        if be is None or not price or rank > 150 or avg3 < 55:
            continue
        margin = avg3 - be
        if margin >= 20:
            head = random.choice([
                f"\U0001F4B0 {p['name']}'s breakeven is {be}. He's averaged {avg3}SC over his past three, so his price is rising.",
                f"\U0001F4B0 {p['name']}'s price is climbing: a breakeven of {be} against a {avg3}SC average over three.",
            ])
            out.append(("value", p["id"], "val_rise", f"{head}\n\n{HASHTAGS}"))
        elif margin <= -18 and avg3 < 95:
            head = random.choice([
                f"\U0001F4B0 {p['name']}'s breakeven is {be}, above his {avg3}SC average over the past three. His price is set to drop.",
                f"\U0001F4B0 {p['name']}'s breakeven has climbed to {be} against a {avg3}SC average over his past three.",
            ])
            out.append(("value", p["id"], "val_fall", f"{head}\n\n{HASHTAGS}"))
    return out


def breaking_tweets(news):
    out = []
    for it in news:
        if (it.get("type") == "injury" and it.get("status") == "new"
                and it.get("player") and it.get("pid")
                and not _blocked(it.get("player"))):
            bp = ""
            tags = it.get("tags") or []
            if len(tags) > 1 and tags[1]:
                bp = str(tags[1])
            detail = f" ({bp})" if bp else ""
            out.append(("breaking", it.get("pid"), "binj",
                        f"Team news: {it['player']} is listed on the injury list{detail}. "
                        f"One to check before your team locks. {HASHTAGS}"))
    return out


def load_log():
    try:
        return json.loads(TWEETED_LOG.read_text(encoding="utf-8"))
    except Exception:
        return {"posted": []}


def aest_now():
    """Current time in Melbourne (handles AEST/AEDT); falls back to UTC+10."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Australia/Melbourne"))
    except Exception:
        from datetime import timezone, timedelta
        return datetime.now(timezone.utc) + timedelta(hours=10)


def should_auto_post(log):
    """Gate for --auto: post only during 6am–11pm AEST, max DAILY_TARGET/day,
    spaced so 5 tweets spread across the ~17h window (≈3h apart). Returns
    (ok, reason)."""
    now = aest_now()
    if not (6 <= now.hour < 23):
        return False, f"outside posting window (AEST {now:%H:%M})"
    today = now.strftime("%Y-%m-%d")
    todays = [e for e in log.get("posted", []) if (e.get("at_aest", "")[:10] == today)]
    if len(todays) >= DAILY_TARGET:
        return False, f"already posted {len(todays)}/{DAILY_TARGET} today"
    if todays:
        last = max(e.get("at_aest", "") for e in todays)
        try:
            gap_h = (now - datetime.fromisoformat(last)).total_seconds() / 3600
            if gap_h < 2.8:
                return False, f"only {gap_h:.1f}h since last (spacing ~3h)"
        except Exception:
            pass
    return True, f"clear to post ({len(todays)}/{DAILY_TARGET} today, AEST {now:%H:%M})"


# AFL Pulse API — the public matches endpoint already used by news_scraper.
# 85 is 2026 Toyota AFL Premiership; update annually when the new season
# ID drops (or fetch /afl/v2/compseasons to lookup by year).
AFL_API_SEASON_ID = 85
FIXTURE_CACHE_PATH = BASE / ".fixture_cache.json"
FIXTURE_CACHE_TTL_HOURS = 168  # 7 days — fixture rarely changes once set


# Mapping from the API's team names (e.g. "Gold Coast SUNS") to the team names
# used in our players.json ("Gold Coast"). The API names are inconsistent —
# some have nicknames, some don't.
_TEAM_API_TO_OURS = {
    "Adelaide Crows":    "Adelaide",
    "Brisbane Lions":    "Brisbane",
    "Carlton":           "Carlton",
    "Collingwood":       "Collingwood",
    "Essendon":          "Essendon",
    "Fremantle":         "Fremantle",
    "Geelong Cats":      "Geelong",
    "Gold Coast SUNS":   "Gold Coast",
    "GWS GIANTS":        "GWS Giants",
    "Hawthorn":          "Hawthorn",
    "Melbourne":         "Melbourne",
    "North Melbourne":   "North Melbourne",
    "Port Adelaide":     "Port Adelaide",
    "Richmond":          "Richmond",
    "St Kilda":          "St Kilda",
    "Sydney Swans":      "Sydney",
    "West Coast Eagles": "West Coast",
    "Western Bulldogs":  "Western Bulldogs",
}


def fetch_round_fixture(round_num, season_id=AFL_API_SEASON_ID):
    """Return the set of team names playing in the given round, mapped to
    our players.json team naming. Returns None on network failure (callers
    should fall back to a sensible default rather than blocking).

    Cached to .fixture_cache.json for FIXTURE_CACHE_TTL_HOURS so we don't
    hit the AFL API every tweet pick cycle.
    """
    cache_key = f"r{round_num}_s{season_id}"
    # Cache read
    try:
        cache = json.loads(FIXTURE_CACHE_PATH.read_text(encoding="utf-8"))
        rec = cache.get(cache_key)
        if rec:
            from datetime import timezone
            fetched_at = datetime.fromisoformat(rec["fetched_at"])
            age_h = (datetime.now(timezone.utc)
                     - fetched_at.replace(tzinfo=timezone.utc)
                     if fetched_at.tzinfo is None else
                     datetime.now(timezone.utc) - fetched_at
                    ).total_seconds() / 3600
            if age_h < FIXTURE_CACHE_TTL_HOURS:
                return set(rec["teams"])
    except Exception:
        cache = {}

    # Live fetch
    try:
        import requests
        r = requests.get(
            "https://aflapi.afl.com.au/afl/v2/matches",
            params={"compSeasonId": season_id, "roundNumber": round_num,
                    "pageSize": 20},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        if not r.ok:
            return None
        matches = r.json().get("matches", []) or []
    except Exception:
        return None

    teams = set()
    for m in matches:
        for side in ("home", "away"):
            api_name = (m.get(side, {}).get("team", {}).get("name") or "").strip()
            ours = _TEAM_API_TO_OURS.get(api_name)
            if ours:
                teams.add(ours)
    if not teams:
        return None

    # Persist cache
    try:
        cache[cache_key] = {
            "teams": sorted(teams),
            "fetched_at": datetime.now().isoformat(),
        }
        FIXTURE_CACHE_PATH.write_text(json.dumps(cache, indent=2),
                                       encoding="utf-8")
    except Exception:
        pass
    return teams


def live_teams(cur_round, season_id=AFL_API_SEASON_ID):
    """Set of our-team-names whose game is CURRENTLY in progress — kicked off but
    not yet CONCLUDED. Used to suppress tweets about a player while their match is
    live (the user doesn't want us commenting on players mid-game). Scans the
    in-progress round and the next one. Fails OPEN (returns empty set) on any
    network/parse error so a transient blip never silences the whole feed."""
    from datetime import timezone
    live = set()
    try:
        import requests
        now = datetime.now(timezone.utc)
        for rnd in {max(1, cur_round), cur_round + 1}:
            r = requests.get(
                "https://aflapi.afl.com.au/afl/v2/matches",
                params={"compSeasonId": season_id, "roundNumber": rnd,
                        "pageSize": 20},
                headers={"User-Agent": "Mozilla/5.0"}, timeout=10,
            )
            if not r.ok:
                continue
            for m in r.json().get("matches", []) or []:
                status = (m.get("status") or "").upper()
                if status == "CONCLUDED":
                    continue
                st = (m.get("utcStartTime") or "").replace("Z", "+00:00")
                try:
                    started = bool(st) and now >= datetime.fromisoformat(st)
                except Exception:
                    started = status not in (
                        "SCHEDULED", "", "UPCOMING", "UNCONFIRMED_TEAMS")
                if not started:
                    continue   # not kicked off yet — fine to tweet
                for side in ("home", "away"):
                    ours = _TEAM_API_TO_OURS.get(
                        (m.get(side, {}).get("team", {}).get("name") or "").strip())
                    if ours:
                        live.add(ours)
    except Exception:
        return set()
    return live


SITE_URL = "aflfantasywire.com"
# Tab deep-links — the index.html hash router reads these on load and seeds
# localStorage.afw_tab so the app boots straight into the right tab.
LINK_RANKINGS = f"https://{SITE_URL}/#rankings"
LINK_RISERS   = f"https://{SITE_URL}/#risers"
LINK_FALLERS  = f"https://{SITE_URL}/#fallers"
LINK_WAIVER   = f"https://{SITE_URL}/#waiver"


def consistency_tweets(players, log):
    """Occasional feature on a notable-consistency player.
       High ≥ 85%: 'Start with confidence'
       Low  ≤ 45%: 'Play with caution'
    At most one per day. Requires ≥5 played games so the % is statistically
    meaningful, and player rank ≤ 150 so we're featuring relevant players."""
    today = datetime.now().strftime("%Y-%m-%d")
    for e in log.get("posted") or []:
        ang = e.get("angle") or ""
        if ang.startswith("cons_") and (e.get("at") or "")[:10] == today:
            return []

    high_cands, low_cands = [], []
    for p in players:
        scores = p.get("scores") or []
        if sum(1 for s in scores if s and s > 0) < 5:
            continue
        rank = p.get("rank") or 999
        if rank > 150:
            continue
        # Per spec: skip top-5 ranked players unless they're actually
        # trending (3-game vs season avg gap ≥ 18 either way). Those
        # players are universally known — talking about them on a slow
        # day adds little insight.
        if rank <= 5:
            avg  = p.get("scAvg")  or 0
            avg3 = p.get("scAvg3") or 0
            if abs(avg3 - avg) < 18:
                continue
        con = int(p.get("consistency") or 0)
        if con >= 85:
            high_cands.append((-con, p.get("rank") or 999, p, con))
        elif 0 < con <= 45 and rank > 100:
            # Never flag a top-100 player with "play with caution" on consistency
            # alone — a premium with a volatile profile is still a premium; the
            # caution framing reads wrong for them.
            low_cands.append((con, p.get("rank") or 999, p, con))

    # Alternate high/low day-by-day so the feed varies. Fall back to whichever
    # bucket has a candidate when one is empty.
    pick_high = (datetime.now().toordinal() % 2 == 0)
    if pick_high and high_cands:
        high_cands.sort(key=lambda x: (x[0], x[1]))
        _, _, p, con = high_cands[0]
        emoji, framing, angle = "\U0001F3AF", "Start with confidence", "cons_high"
    elif low_cands:
        low_cands.sort(key=lambda x: (x[0], x[1]))
        _, _, p, con = low_cands[0]
        emoji, framing, angle = "\U000026A0️", "Play with caution", "cons_low"
    elif high_cands:
        high_cands.sort(key=lambda x: (x[0], x[1]))
        _, _, p, con = high_cands[0]
        emoji, framing, angle = "\U0001F3AF", "Start with confidence", "cons_high"
    else:
        return []

    rank = p.get("rank") or 999
    avg  = round(p.get("scAvg")  or 0)
    avg3 = round(p.get("scAvg3") or 0)
    text = (
        f"{emoji} {p['name']} — {con}% consistency rating.\n"
        f"{framing}.\n\n"
        f"3-game avg: {avg3}SC | Season avg: {avg}SC\n"
        f"Ranked #{rank} in our live SuperCoach rankings.\n\n"
        f"Full breakdowns and player form:\n{LINK_RANKINGS}\n"
        f"{HASHTAGS}"
    )
    return [("cta", p["id"], angle, text)]


def cta_tweets(players, log):
    """Drive traffic to the site — at most 1 per day.

    Two templates rotate based on what the data supports:

      A. PLAYER RANK CALLOUT — a top-100 SC ranker who's also trending up.
         "Bailey Dale is in form … ranked #15 in our live SuperCoach
          rankings. See the full top 200: aflfantasywire.com"

      B. GENERIC RISERS CALLOUT — when no single player is the obvious
         hero of the day, surface that "N players are surging" and link
         to the full list.

    Per-day cap is enforced by scanning tweeted.json for any angle
    starting with `cta_` posted today.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    for e in log.get("posted") or []:
        ang = e.get("angle") or ""
        if ang.startswith("cta_") and (e.get("at") or "")[:10] == today:
            return []

    # ── Template A: top-100 player who's trending up ──────────────────
    # Exclude anyone we've already featured in the last ~12 days so the same
    # in-form name (e.g. the day's biggest gap) doesn't get tweeted over and over.
    recent = _recently_tweeted_pids(log, 12)
    candidates = []
    for p in players:
        avg  = p.get("scAvg")  or 0
        avg3 = p.get("scAvg3") or 0
        rank = p.get("rank")   or 999
        if rank > 100 or avg < 60 or avg3 < 95:
            continue
        if p.get("id") in recent:
            continue
        gap = avg3 - avg
        if gap < 12:
            continue
        candidates.append((gap, p, rank))
    if candidates:
        # Largest 3-vs-season gap first — explicit key because the tuple
        # contains a dict (not orderable in Python 3 when gaps tie).
        candidates.sort(key=lambda x: x[0], reverse=True)
        gap, p, rank = candidates[0]
        _a3, _av = round(p.get("scAvg3") or 0), round(p.get("scAvg") or 0)
        head = random.choice([
            f"\U0001F4C8 {p['name']} is in form",
            f"\U0001F4C8 {p['name']} has been climbing",
            f"\U0001F4C8 Form watch: {p['name']}",
            f"\U0001F4C8 {p['name']} on the move up the rankings",
        ])
        body = random.choice([
            f"3-game avg: {_a3}SC | Season avg: {_av}SC\n"
            f"Currently #{rank} in our live SuperCoach rankings.",
            f"Up to #{rank} in our live rankings — {_a3}SC across his past three, "
            f"well clear of his {_av}SC season mark.",
            f"Averaging {_a3}SC over his past three ({_av}SC for the season), now "
            f"#{rank} in our live SuperCoach rankings.",
        ])
        return [(
            "cta", p["id"], "cta_rank",
            f"{head}\n\n{body}\n\n"
            f"Full rankings, breakdowns and form data:\n{LINK_RANKINGS}\n"
            f"{HASHTAGS}"
        )]

    # ── Template B: generic risers callout (fallback) ─────────────────
    n_risers = sum(
        1 for p in players
        if (p.get("scAvg3") or 0) - (p.get("scAvg") or 0) >= 18
        and (p.get("scAvg") or 0) > 60
        and (p.get("rank") or 999) <= 200
    )
    if n_risers >= 3:
        return [(
            "cta", 0, "cta_risers",
            f"\U0001F4C8 {n_risers} players in trade-up form this week — "
            f"3-game averages climbing well clear of season marks.\n\n"
            f"Full list with form windows and breakdowns:\n"
            f"{LINK_RISERS}\n\n"
            f"{HASHTAGS}"
        )]

    return []


def top10_tweet(players, log, current_round, min_players=275, min_teams=18):
    """A single round-recap tweet with the round's top 10 SuperCoach scorers.

    Fires AT MOST ONCE per round. Two gates must both pass:

      * `min_players` players have a positive current-round score (catches
        "lots of players in" but vulnerable to partial games — a half-played
        fixture can push us over the threshold even if a team's data is
        entirely absent).
      * `min_teams` unique TEAMS are represented in current-round scorers.
        This is the more reliable "is the round actually complete" gate —
        18 means every team has had at least one of their players'
        scores processed by Footywire. The reason the R12 recap missed
        West Coast originally: their game ran but Footywire's per-round
        score publication for that fixture lagged by ~24h. The
        player-count gate (250+) was satisfied by the other 12 teams.

    Use min_teams=18 for regular rounds; bye rounds will need a lower
    explicit value passed in.

    Uses `pid=0` (which is not a real player id) as a sentinel so the
    standard per-player-per-round dedup leaves it alone.

    Uses `pid=0` (which is not a real player id) as a sentinel so the
    standard per-player-per-round dedup leaves it alone.
    """
    if not current_round:
        return []

    # Already tweeted this round? Skip.
    for e in log.get("posted") or []:
        if e.get("angle") != "top10":
            continue
        try:
            if int(e.get("round") or 0) == current_round:
                return []
        except (TypeError, ValueError):
            pass

    # Collect everyone who scored in the current round.
    scored = []
    for p in players or []:
        try:
            if int(p.get("lastRound") or 0) != current_round:
                continue
        except (TypeError, ValueError):
            continue
        scores = p.get("scores") or []
        if scores and isinstance(scores[-1], (int, float)) and scores[-1] > 0:
            scored.append((int(scores[-1]), p.get("name") or ""))

    if len(scored) < min_players:
        return []  # Wait until more games are in — current snapshot is partial.

    # Team-coverage gate: derive which teams ACTUALLY play this round from
    # the AFL fixture API (handles byes — R12 2026 has 4 byes, so only
    # 14 teams play). Hold the recap until every team that played has at
    # least one player processed for this round.
    teams_seen = set()
    for p in players or []:
        try:
            if int(p.get("lastRound") or 0) != current_round:
                continue
        except (TypeError, ValueError):
            continue
        scores2 = p.get("scores") or []
        if scores2 and isinstance(scores2[-1], (int, float)) and scores2[-1] > 0:
            t = (p.get("team") or "").strip()
            if t:
                teams_seen.add(t)

    teams_playing = fetch_round_fixture(current_round)
    if teams_playing is None:
        # Fixture lookup failed — fall back to the static min_teams floor.
        if len(teams_seen) < min_teams:
            return []
    else:
        # We know exactly which teams played. Every one of them must have
        # at least one player processed before we publish the top 10.
        missing = teams_playing - teams_seen
        if missing:
            return []  # Footywire still catching up on one or more fixtures.

    scored.sort(reverse=True)
    top = scored[:10]

    def _shorten(name, mode="full"):
        parts = name.split()
        if len(parts) < 2:
            return name
        if mode == "initial":          # "B. Grundy"
            return parts[0][0] + ". " + " ".join(parts[1:])
        if mode == "lastname":         # "Grundy"
            return parts[-1]
        return name                    # "Brodie Grundy"

    # Try progressively shorter formats until we fit ~278 chars. The header
    # carries the "SuperCoach scores" framing so each row's score doesn't need
    # an "SC" suffix — cleaner and shorter.
    for header in (
        f"\U0001F3C6 Top 10 SuperCoach Player Scores — Round {current_round}",
        f"\U0001F3C6 Top 10 SuperCoach Scores — Round {current_round}",
        f"\U0001F3C6 R{current_round} Top 10 SuperCoach Scores",
    ):
        for mode in ("full", "initial", "lastname"):
            lines = [f"{i}. {_shorten(n, mode)} {s}"
                     for i, (s, n) in enumerate(top, 1)]
            text = header + "\n\n" + "\n".join(lines) + f"\n\n{HASHTAGS}"
            if len(text) <= 278:
                return [("topweek", 0, "top10", text)]

    # Last resort: clip names hard to stay under the limit.
    lines = [f"{i}. {_shorten(n, 'lastname')[:10]} {s}"
             for i, (s, n) in enumerate(top, 1)]
    text = (f"\U0001F3C6 R{current_round} Top 10 SC Scores\n\n"
            + "\n".join(lines) + f"\n\n{HASHTAGS}")
    return [("topweek", 0, "top10", text)]


def _current_round(players):
    """Highest lastRound across all players — best proxy for 'this round'."""
    rs = []
    for p in players or []:
        r = p.get("lastRound")
        try:
            r = int(r)
            if r > 0:
                rs.append(r)
        except (TypeError, ValueError):
            continue
    return max(rs) if rs else 0


def _pid_round_history(log):
    """Map pid -> list of (round, angle) for every prior posted tweet."""
    out = {}
    for e in log.get("posted") or []:
        pid = e.get("pid")
        if pid is None:
            continue
        try:
            rnd = int(e.get("round") or 0)
        except (TypeError, ValueError):
            rnd = 0
        ang = e.get("angle") or ""
        out.setdefault(pid, []).append((rnd, ang))
    return out


def _recently_tweeted_pids(log, days=14):
    """pids tweeted within the last `days` — a per-player cooldown so the
    same player is not featured more than once per fortnight."""
    from datetime import timedelta
    cutoff = datetime.now() - timedelta(days=days)
    out = set()
    for e in log.get("posted") or []:
        pid = e.get("pid")
        if not pid:  # None / 0 sentinel
            continue
        ts = (e.get("at") or "").split("+")[0].replace("Z", "")
        try:
            if datetime.fromisoformat(ts) >= cutoff:
                out.add(pid)
        except Exception:
            continue
    return out


def _expand_for_momentum(text, angle):
    """Rewrite the lead so a follow-up tweet about the same trend reads as
    an update (`momentum building` / `slide deepening`) instead of restating
    the same headline as last round."""
    if angle in ("crise", "drise"):
        text = text.replace(" trending up", " — momentum building", 1)
        text = text.replace(" on the rise",  " — momentum building", 1)
    else:
        text = text.replace(" cooling off",         " — slide deepening", 1)
        text = text.replace("'s output has eased", "'s slide is deepening", 1)
    return text


def _add_hook(text, angle):
    """Append a CTA link to the full risers/fallers list before the hashtags."""
    h = ("Full risers list 👉 " + SITE_URL + "/#risers" if angle in ("crise", "drise")
         else "Full fallers list 👉 " + SITE_URL + "/#fallers" if angle in ("cfall", "dfall")
         else "")
    if not h:
        return text
    if HASHTAGS in text:
        return text.replace(HASHTAGS, h + "\n\n" + HASHTAGS, 1)
    return text + "\n\n" + h

NOT_PLAYING_STATUS = {"out", "test", "tbc", "doubtful", "managed",
                      "susp", "suspended", "omitted", "late out"}


def _is_playing(p, cur_round=None):
    """Never tweet about a player who isn't playing the upcoming round. Excludes
    injured / late-out / doubtful / suspended / bye, and anyone out of the side
    for 2+ rounds (catches omissions the injury feed misses, e.g. Kane Farrell,
    flagged 'test' with an adductor and unplayed for weeks)."""
    st = (p.get("injuryStatus") or "").strip().lower()
    if st in NOT_PLAYING_STATUS:
        return False
    if p.get("byeNext"):
        return False
    # Out of the side for 2+ rounds — but only when lastRound is KNOWN, so a
    # games-log data gap (lastRound 0/None) doesn't silence a fit player.
    lr = p.get("lastRound") or 0
    if cur_round and lr and lr < cur_round - 1:
        return False
    return True


def pick(players, news, log):
    """Pick up to DAILY_TARGET varied tweets.

    Dedup rules (per user spec):
      * One tweet per player per round — strict, regardless of angle. If
        a player already got any tweet this round, they're locked out.
      * If a player was tweeted in the IMMEDIATELY previous round with the
        SAME angle and the trend still qualifies under the new rules, the
        text is rewritten via `_expand_for_momentum` so it reads like a
        follow-up update rather than a repeat headline.
    """
    current_round = _current_round(players)
    hist = _pid_round_history(log)
    tweeted_this_round = {pid for pid, evs in hist.items()
                          if any(r == current_round for r, _ in evs)}
    recent14 = _recently_tweeted_pids(log, 14)  # 1 tweet per player / 2 weeks

    # Teams whose game is live right now — never tweet about their players
    # mid-match (per user spec). Fails open (empty set) on API trouble.
    _live = live_teams(current_round)
    if _live:
        print(f"[auto] suppressing players mid-game: {', '.join(sorted(_live))}")
    # Only build form/value/matchup tweets from players who are actually playing
    # (and aren't on the manual mute list — they're excluded from every
    # player-focused angle, but still count toward the top-10 leaderboard), and
    # whose game isn't currently in progress.
    _playing = [p for p in players
                if _is_playing(p, current_round) and not _blocked(p.get("name"))
                and (p.get("team") not in _live)]
    pools = {
        "classic":  classic_tweets(_playing),
        "draft":    draft_tweets(_playing),
        "matchup":  matchup_tweets(_playing),
        "value":    value_tweets(_playing),
    }
    # Rank classic/draft by how strong the move is (biggest |avg3-avg| first);
    # shuffle the matchup/value pools so the same names don't always lead.
    pid_gap = {p["id"]: abs((p.get("scAvg3") or 0) - (p.get("scAvg") or 0)) for p in players}
    for k in ("classic", "draft"):
        pools[k].sort(key=lambda t: pid_gap.get(t[1], 0), reverse=True)
    random.shuffle(pools["matchup"])
    random.shuffle(pools["value"])

    chosen, used_pids, used_angles = [], set(), set()

    # One special slot per day: round-recap (top10) when the round is complete,
    # else a site CTA. (Consistency feature removed.)
    specials = top10_tweet(players, log, current_round) or cta_tweets(_playing, log)
    for s in specials:
        if not s[1] or s[1] not in recent14:
            chosen.append(s)
            if s[1]:
                used_pids.add(s[1])
            used_angles.add(s[2])

    # Varied fill: rotate through angle FAMILIES (form trend, matchup, value) so
    # a day's tweets span different angles rather than three near-identical form
    # cards. No two tweets share an angle, and the usual per-player dedup holds.
    families = ["classic", "draft", "matchup", "value"]
    random.shuffle(families)
    fi = attempts = 0
    while len(chosen) < DAILY_TARGET and attempts < 60:
        attempts += 1
        kind = families[fi % len(families)]
        fi += 1
        for cand in list(pools.get(kind, [])):
            ttype, pid, angle, text = cand
            if pid and (pid in tweeted_this_round or pid in used_pids or pid in recent14):
                pools[kind].remove(cand)
                continue
            if angle in used_angles:
                continue
            # If the same angle was tweeted last round → frame as a follow-up.
            if current_round and any(r == current_round - 1 and a == angle
                                      for r, a in hist.get(pid, [])):
                text = _expand_for_momentum(text, angle)
            text = _add_hook(text, angle)
            if len(text) > 278:
                pools[kind].remove(cand)
                continue
            chosen.append((ttype, pid, angle, text))
            used_pids.add(pid)
            used_angles.add(angle)
            pools[kind].remove(cand)
            break
    return chosen[:DAILY_TARGET]


# ─────────────────────────────────────────────────────────────────────────────
# GOLD PRE-GAME STAT CARDS  (+ success follow-ups)
# One tweet per FINALISED game (teams officially named) within 24h of the bounce:
# the game's top 5 gold expert picks, each with their standout projected stat as a
# low–expected range. After the round, a quote-tweet recaps which calls landed.
# IMPORTANT: gold cards NEVER post until the team is named (afl_lineups only returns
# FINAL_TEAM players) — so we don't talk about a pick until the side is finalised.
# ─────────────────────────────────────────────────────────────────────────────
GOLD_WITHIN_HOURS = 24       # only card a game once it's <=24h away
GOLD_DAILY_CAP    = 6        # gold cards + follow-ups per day (separate from DAILY_TARGET)
GOLD_MIN_GAP_MIN  = 20       # min minutes between gold posts
# Predictions post frequently, so keep their tags lean and game-specific: the two
# clubs' names + #AFL. Club tags rotate per game and tap both fanbases + the broad
# #AFL audience — better reach than the same generic fantasy tags on every card.
GOLD_HASHTAGS     = "#AFL"


# Each club's official X handle, used as a hashtag on game cards (verified Jun 2026).
# NB: as HASHTAGS these don't notify the club — switch to '@handle' (mention) form if
# you'd rather tag the clubs directly.
CLUB_TAG = {
    "Adelaide": "#Adelaide_FC", "Brisbane": "#brisbanelions", "Carlton": "#CarltonFC",
    "Collingwood": "#CollingwoodFC", "Essendon": "#essendonfc", "Fremantle": "#freodockers",
    "Geelong": "#GeelongCats", "Gold Coast": "#GoldCoastSUNS",
    "GWS Giants": "#GWSGIANTS", "GWS": "#GWSGIANTS", "Hawthorn": "#HawthornFC",
    "Melbourne": "#melbournefc", "North Melbourne": "#NMFCOfficial", "Port Adelaide": "#PAFC",
    "Richmond": "#Richmond_FC", "St Kilda": "#stkildafc", "Sydney": "#sydneyswans",
    "West Coast": "#WestCoastEagles", "Western Bulldogs": "#westernbulldogs",
}


def _team_tag(team):
    """Club's official-handle hashtag, e.g. 'West Coast' -> '#WestCoastEagles'. Falls
    back to the space-stripped club name for anything not in the map."""
    return CLUB_TAG.get(team) or ("#" + "".join(ch for ch in (team or "") if ch.isalnum()))


def _game_tags(home, away):
    """Tag line for a game card/recap: both clubs + #SuperCoach."""
    return f"{_team_tag(home)} {_team_tag(away)} {GOLD_HASHTAGS}".strip()

# Headline-stat candidates and the minimum projected volume each must clear to be
# eligible as a player's "standout" (keeps low-noise stats like 1 behind out).
_GOLD_STAT_MIN = {"goals": 1.5, "marks": 5, "tackles": 4,
                  "clearances": 4, "handballs": 13, "kicks": 14}


def _gold_baselines(players):
    """Per-position mean of each candidate stat (from statPred) — the yardstick for
    'what this player does better than his positional peers'."""
    base = defaultdict(lambda: defaultdict(list))
    for p in players:
        sp = p.get("statPred")
        if not sp:
            continue
        for s in _GOLD_STAT_MIN:
            if sp.get(s) is not None:
                base[p.get("pos")][s].append(sp[s])
    return {pos: {s: sum(v) / len(v) for s, v in dd.items() if v}
            for pos, dd in base.items()}


def _range(p, s):
    """(stat, low, exp, 'L-E') for one stat from statPred/statPredLow, or None."""
    sp = p.get("statPred") or {}
    if sp.get(s) is None:
        return None
    e = sp[s]
    l = (p.get("statPredLow") or {}).get(s, e)
    L, E = int(round(min(l, e))), int(round(e))
    return (s, L, E, f"{L}-{E}" if E > L else f"{E}")


def _headline_stat(p, baselines):
    """The stat to show for a gold pick — the player's ACTUAL gold stat(s) from
    p['statGold'] (what the site flags), NOT a heuristic. If a player is gold for
    more than one stat, show the one with the biggest lift over their season average.
    Falls back to the position-ratio heuristic only if statGold is somehow absent.
    Returns (stat, low, exp, range_str) or None."""
    sp = p.get("statPred") or {}
    gold = [s for s in (p.get("statGold") or {}) if sp.get(s) is not None]
    if gold:
        # most notable gold stat = largest projected lift vs the player's own average
        gold.sort(key=lambda s: (sp[s] / (p.get(s) or sp[s])), reverse=True)
        return _range(p, gold[0])
    # fallback (no statGold): highest projection-vs-positional-average ratio
    lo = p.get("statPredLow") or {}
    elig = [(sp[s] / (baselines.get(p.get("pos"), {}).get(s) or sp[s]), s)
            for s in _GOLD_STAT_MIN if sp.get(s) and sp[s] >= _GOLD_STAT_MIN[s]]
    if not elig:
        elig = [(sp.get(s, 0), s) for s in ("goals", "kicks", "handballs") if sp.get(s)]
        if not elig:
            return None
    elig.sort(reverse=True)
    return _range(p, elig[0][1])


def gold_game_tweets(players, log):
    """One card per finalised game within 24h — top 5 gold picks by SC rank, each
    with their standout stat. Returns [(kind, pid, angle, text, meta)]. Empty until
    teams are named. Skips games already carded this round (dedup via tweeted.json)."""
    import afl_lineups
    cur = _current_round(players)
    lineup = afl_lineups.confirmed_lineup([cur, cur + 1])
    if not lineup:
        return []  # no teams named yet — stay silent
    by_alt = {afl_lineups.alt_key(v["name"], v["team"]): v for v in lineup.values()}
    baselines = _gold_baselines(players)

    games = defaultdict(lambda: {"info": None, "picks": []})
    for p in players:
        if not p.get("hasGold") or not p.get("statPred") or _blocked(p.get("name")):
            continue
        info = (lineup.get(afl_lineups.lineup_key(p.get("name", "")))
                or by_alt.get(afl_lineups.alt_key(p.get("name", ""), p.get("team", ""))))
        if not info or info.get("status") != "named":
            continue  # not in the named 22/23 (emergencies excluded)
        h = afl_lineups.hours_until(info.get("startUtc"))
        if h is None or h <= 0 or h > GOLD_WITHIN_HOURS:
            continue
        games[info["matchId"]]["info"] = info
        games[info["matchId"]]["picks"].append(p)

    carded = {e.get("matchId") for e in (log.get("posted") or [])
              if e.get("angle") == "gold_game"}
    out = []
    for mid, g in games.items():
        if mid in carded:
            continue
        info = g["info"]
        picks = sorted(g["picks"], key=lambda x: x.get("rank") or 999)[:5]
        header = f"{info['home']} v {info['away']}"
        rows, meta_picks = [], []
        for p in picks:
            hs = _headline_stat(p, baselines)
            if not hs:
                continue
            stat, L, E, rng = hs
            rows.append((p["name"], f"{rng} {stat}"))
            meta_picks.append({"name": p["name"], "short": p["name"].split()[-1],
                               "stat": stat, "label": f"{rng} {stat}", "low": L, "exp": E})
        if not meta_picks:
            continue
        tags = _game_tags(info["home"], info["away"])
        hook = "Full predictions \U0001F449 " + SITE_URL + "/#predictions"

        def _compose(rws):
            if len(rws) == 1:   # singular: one standout pick reads as a sentence
                nm, lbl = rws[0]
                head = f"\U0001F947 {nm} is our only gold pick for {header}"
                return head + "\n" + f"We project {lbl}." + "\n\n" + hook + "\n" + tags
            body = "\n".join(f"{nm}: {lbl}" for nm, lbl in rws)
            return f"{header} — our gold picks \U0001F947\n" + body + "\n\n" + hook + "\n" + tags

        text = _compose(rows)
        while len(text) > 278 and len(rows) > 1:   # trim picks to fit (keep the hook)
            rows.pop()
            meta_picks.pop()
            text = _compose(rows)
        out.append(("goldgame", 0, "gold_game", text,
                    {"matchId": mid, "header": header, "round": info.get("round"),
                     "home": info["home"], "away": info["away"], "picks": meta_picks}))
    return out


def gold_followup_tweets(players, log):
    """After the round, quote-tweet each gold card with how the calls landed — but
    only when the card did well (>= half of gradeable picks met their expected mark).
    Returns [(kind, pid, angle, text, meta)] with meta.quoteOf = original tweet id."""
    import afl_lineups
    by_key = {afl_lineups.lineup_key(p.get("name", "")): p for p in players if p.get("name")}
    already = {e.get("quoteOf") for e in (log.get("posted") or [])
              if e.get("angle") == "gold_result"}
    out = []
    for e in (log.get("posted") or []):
        if e.get("angle") != "gold_game" or not e.get("id") or e["id"] in already:
            continue
        rnd = e.get("round")
        picks = e.get("picks") or []
        graded = []
        for pk in picks:
            p = by_key.get(afl_lineups.lineup_key(pk["name"]))
            rr = (p or {}).get("roundResult") or {}
            if rr.get("round") != rnd:
                graded = None     # round not scored for this player yet — wait
                break
            st = (rr.get("stats") or {}).get(pk["stat"])
            if st and st.get("a") is not None:
                graded.append((pk, st["a"]))
        if not graded:            # None (incomplete) or nothing gradeable
            continue
        # "Landed" = actual reached at least the published low (in or above our range).
        def _lo(pk):
            return pk.get("low", pk.get("exp"))
        hits = [(pk, a) for pk, a in graded if a >= _lo(pk)]
        if len(hits) < max(1, (len(graded) + 1) // 2):
            continue              # didn't do well enough — stay quiet
        lines = [f"✅ How our {e['header']} gold calls landed:"]
        for pk, a in graded:
            mark = "✅" if a >= _lo(pk) else "❌"
            lines.append(f"{pk['short']} {pk['label']} → {a} {mark}")
        # Reuse the original card's club tags (fall back to parsing the header).
        home, away = e.get("home"), e.get("away")
        if not (home and away) and " v " in (e.get("header") or ""):
            home, away = e["header"].split(" v ", 1)
        tags = _game_tags(home, away)
        hook = "Full predictions \U0001F449 " + SITE_URL + "/#predictions"
        text = "\n".join(lines) + "\n\n" + hook + "\n" + tags
        while len(text) > 278 and len(lines) > 2:
            lines.pop()
            text = "\n".join(lines) + "\n\n" + hook + "\n" + tags
        out.append(("goldresult", 0, "gold_result", text, {"quoteOf": e["id"], "round": rnd}))
    return out


def _gold_throttle(log):
    """Own posting gate for gold cards/follow-ups: 6am-11pm AEST, GOLD_DAILY_CAP/day,
    >=GOLD_MIN_GAP_MIN apart. Separate from the varied-tweet quota."""
    now = aest_now()
    if not (6 <= now.hour < 23):
        return False, f"outside posting window (AEST {now:%H:%M})"
    today = now.strftime("%Y-%m-%d")
    todays = [e for e in log.get("posted", [])
              if e.get("angle") in ("gold_game", "gold_result")
              and e.get("at_aest", "")[:10] == today]
    if len(todays) >= GOLD_DAILY_CAP:
        return False, f"gold cap reached ({len(todays)}/{GOLD_DAILY_CAP})"
    if todays:
        last = max(e.get("at_aest", "") for e in todays)
        try:
            if (now - datetime.fromisoformat(last)).total_seconds() / 60 < GOLD_MIN_GAP_MIN:
                return False, f"gold spacing (<{GOLD_MIN_GAP_MIN}m since last)"
        except Exception:
            pass
    return True, f"gold clear ({len(todays)}/{GOLD_DAILY_CAP} today)"


def run_gold(do_post):
    """Generate (and optionally post) gold pre-game cards + success follow-ups.
    Follow-ups are prioritised over new cards. In --gold-auto mode, posts at most one
    item per cycle, self-throttled; cards never appear until teams are named."""
    players = _load("players.json", "players")
    _normalise_names(players, None)
    log = load_log()
    items = gold_followup_tweets(players, log) + gold_game_tweets(players, log)

    if not items:
        print("[gold] nothing due (no finalised games within 24h, or all carded)")
        return

    if not do_post:
        print(f"=== {len(items)} gold item(s) (PREVIEW) ===")
        for kind, pid, angle, text, meta in items:
            tag = "quote->" + str(meta.get("quoteOf")) if meta.get("quoteOf") else meta.get("matchId", "")
            print(f"\n[{angle} {tag}] ({len(text)} chars)\n{text}")
        print("\n(preview only — run with --gold-auto to publish)")
        return

    ok, why = _gold_throttle(log)
    print(f"[gold] {why}")
    if not ok:
        return
    env = load_env()
    for cred in ("X_CONSUMER_KEY", "X_CONSUMER_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"):
        if not env.get(cred):
            print(f"Missing {cred} in .env — cannot post.")
            return
    kind, pid, angle, text, meta = items[0]      # one per cycle, follow-ups first
    code, body = post_tweet(text, env, quote_tweet_id=meta.get("quoteOf"))
    if code in (200, 201):
        tid = ""
        try:
            tid = json.loads(body).get("data", {}).get("id", "")
        except Exception:
            pass
        print(f"  [ok] posted ({tid}): {text[:60]}")
        rec = {"pid": pid, "angle": angle, "round": meta.get("round"), "id": tid,
               "at": datetime.now().isoformat(), "at_aest": aest_now().isoformat(),
               "text": text}
        for k in ("matchId", "header", "picks", "quoteOf"):
            if k in meta:
                rec[k] = meta[k]
        posted = log.get("posted", [])
        posted.append(rec)
        log["posted"] = posted
        TWEETED_LOG.write_text(json.dumps(log, indent=2), encoding="utf-8")
    else:
        print(f"  [FAIL] ({code}): {body[:300]}")


def post_tweet(text, env, quote_tweet_id=None):
    from requests_oauthlib import OAuth1Session
    oauth = OAuth1Session(
        env["X_CONSUMER_KEY"], client_secret=env["X_CONSUMER_SECRET"],
        resource_owner_key=env["X_ACCESS_TOKEN"], resource_owner_secret=env["X_ACCESS_TOKEN_SECRET"],
    )
    payload = {"text": text}
    if quote_tweet_id:
        payload["quote_tweet_id"] = str(quote_tweet_id)
    r = oauth.post("https://api.twitter.com/2/tweets", json=payload, timeout=30)
    return r.status_code, r.text


COMMON_NAME_ALIASES = {
    "Zachary Merrett": "Zach Merrett", "Timothy English": "Tim English",
    "Joshua Kelly": "Josh Kelly", "Joshua Weddle": "Josh Weddle",
    "Thomas Liberatore": "Tom Liberatore", "Thomas Stewart": "Tom Stewart",
    "Thomas Sims": "Tom Sims", "Thomas Burton": "Tom Burton",
    "Thomas Matthews": "Tom Matthews", "Samuel Collins": "Sam Collins",
    "Samuel Swadling": "Sam Swadling", "Samuel Grlj": "Sam Grlj",
    "Nicholas Martin": "Nick Martin", "Nicholas Coffield": "Nick Coffield",
    "Nicholas Holman": "Nick Holman", "Mitchell Lewis": "Mitch Lewis",
    "Mitchell Knevitt": "Mitch Knevitt", "Mitchell Hinge": "Mitch Hinge",
    "Mitchell Edwards": "Mitch Edwards", "Matthew Kennedy": "Matt Kennedy",
    "Matthew Roberts": "Matt Roberts", "Matthew Flynn": "Matt Flynn",
    "Matthew Jefferson": "Matt Jefferson", "Matthew LeRay": "Matt LeRay",
    "Cameron Rayner": "Cam Rayner", "Cameron Mackenzie": "Cam Mackenzie",
    "Cameron Zurhaar": "Cam Zurhaar", "Cameron Nairn": "Cam Nairn",
    "Bradley Close": "Brad Close", "Bradley Hill": "Brad Hill",
    "Zachary Williams": "Zac Williams",
}


def _common_name(n):
    """Footy common name (e.g. "Zachary Merrett" -> "Zach Merrett"). Keeps tweets
    reading like a human wrote them rather than echoing the formal data name.
    Accepts a bare name string or a {"pid","name"} tag object (the richer
    news.json `players` format) and normalises in place either way."""
    if isinstance(n, dict):
        if n.get("name"):
            n["name"] = COMMON_NAME_ALIASES.get(n["name"], n["name"])
        return n
    return COMMON_NAME_ALIASES.get(n, n) if n else n


def _normalise_names(players, news):
    for p in players or []:
        if isinstance(p, dict) and p.get("name"):
            p["name"] = _common_name(p["name"])
    for it in news or []:
        if not isinstance(it, dict):
            continue
        if it.get("player"):
            it["player"] = _common_name(it["player"])
        if isinstance(it.get("players"), list):
            it["players"] = [_common_name(x) for x in it["players"]]


def main():
    # Gold pre-game cards run on their own path (separate cadence/cap), so they don't
    # interfere with the varied-tweet rotation. --gold previews; --gold-auto posts.
    if "--gold" in sys.argv or "--gold-auto" in sys.argv:
        run_gold(do_post="--gold-auto" in sys.argv)
        return

    do_post = "--post" in sys.argv
    count = DAILY_TARGET
    for a in sys.argv:
        if a.startswith("--count="):
            try:
                count = int(a.split("=", 1)[1])
            except Exception:
                pass
    players = _load("players.json", "players")
    news = _load("news.json", "news")
    _normalise_names(players, news)
    log = load_log()
    if "--auto" in sys.argv:
        ok, why = should_auto_post(log)
        print(f"[auto] {why}")
        if not ok:
            return
        do_post, count = True, 1
    chosen = pick(players, news, log)[:count]

    print(f"=== {len(chosen)} tweets ({'POSTING' if do_post else 'PREVIEW'}) ===")
    for i, (kind, pid, angle, text) in enumerate(chosen, 1):
        print(f"\n[{i}] {kind} ({len(text)} chars)\n{text}")

    if not do_post:
        print("\n(preview only — run with --post to publish)")
        return

    env = load_env()
    for cred in ("X_CONSUMER_KEY", "X_CONSUMER_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"):
        if not env.get(cred):
            print(f"Missing {cred} in .env — cannot post.")
            return

    posted = log.get("posted", [])
    current_round = _current_round(players)
    for kind, pid, angle, text in chosen:
        code, body = post_tweet(text, env)
        if code in (200, 201):
            tid = ""
            try:
                tid = json.loads(body).get("data", {}).get("id", "")
            except Exception:
                pass
            print(f"  [ok] posted ({tid}): {text[:60]}")
            # `round` lets the next run dedup per-player-per-round and decide
            # whether to expand the same trend with "momentum building" text.
            posted.append({"pid": pid, "angle": angle, "round": current_round,
                           "id": tid,
                           "at": datetime.now().isoformat(),
                           "at_aest": aest_now().isoformat(), "text": text})
        else:
            print(f"  [FAIL] ({code}): {body[:300]}")
            # Stop on auth/credit errors so we don't hammer.
            if code in (401, 402, 403):
                break
    log["posted"] = posted
    TWEETED_LOG.write_text(json.dumps(log, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
