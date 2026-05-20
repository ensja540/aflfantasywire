#!/usr/bin/env python3
"""
AFLFantasyWire — News Scraper
==============================
Sources (in priority order):
  1. Footywire injury list        — structured, reliable, best injury data
  2. Footywire selection changes  — named/omitted/role changes
  3. AFL.com.au RSS               — official confirmations, match reports
  4. AFL.com.au team selections   — official named teams
  5. Twitter/X (no API)           — scrapes nitter.net mirror, free

All items pass through news_filter.py before storage.
Items are matched to players in PLAYERS list by name.

Run standalone:  python news_scraper.py
Or import:       from news_scraper import scrape_all_news

OUTPUT
  news.json  — list of fantasy-relevant news items, newest first
               Drop next to aflfantasywire.html
"""

import json, re, time, logging, sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("Run: pip install requests beautifulsoup4 lxml")
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).parent))
from news_filter import classify_item, is_relevant
from news_history import NewsHistory

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("news")

BASE_DIR    = Path(__file__).parent
OUTPUT_PATH = BASE_DIR.parent / "news.json"

# ── ACCOUNTS TO FOLLOW (Twitter/Nitter) ─────────────────────────────────────
# Mix of high-reliability official sources, top AFL beat reporters who break
# news/scoops, and the established SC/DT fantasy accounts. Reliability is
# weighted into both the in-app trust badge and the source-priority dedupe.
TWITTER_ACCOUNTS = [
    # Official / institutional (highest trust)
    ("aflcomau",         "AFL.com.au",         96),
    ("champdata",        "Champion Data",      98),
    ("aflplayers",       "AFL Players Assoc.", 92),
    ("foxfooty",         "Fox Footy",          90),
    # AFL beat reporters & scoop breakers
    ("tom_morris_",      "Tom Morris",         92),
    ("damianbarrett",    "Damian Barrett",     90),
    ("KaneCallaghan",    "Kane Callaghan",     88),
    ("lachlanblakemore", "Lachlan Blakemore",  85),
    ("MickWarner",       "Mick Warner",        83),
    ("CallumDick7",      "Callum Dick",        82),
    ("samedmund",        "Sam Edmund",         82),
    ("heraldsunfooty",   "Herald Sun Sport",   80),
    # Fantasy-focused (lower reliability — treat as rumour by default)
    ("dttalk",           "DT Talk",            82),
    ("supercoach_dr",    "SuperCoach DR",      80),
    ("scscoop",          "Supercoach Scoop",   78),
    ("warnie",           "Warnie",             80),
    ("fantasyfreako",    "Fantasy Freako",     78),
    ("footywire",        "Footywire",          94),
]

# Hashtag/search pages — useful when specific accounts have nothing fresh.
# Nitter exposes /search?q=... and renders the same tweet-content nodes.
TWITTER_SEARCH_QUERIES = [
    "#AFLFantasy",
    "#SuperCoach",
    "AFL injury",
    "AFL named team",
]

# Nitter instances. The public-mirror ecosystem has thinned since Twitter's
# anti-scraping push; this list is ordered by recent uptime observations.
NITTER_INSTANCES = [
    "https://nitter.privacydev.net",
    "https://xcancel.com",
    "https://nitter.poast.org",
    "https://nitter.tiekoetter.com",
    "https://nitter.adminforge.de",
    "https://nitter.kavin.rocks",
    "https://nitter.net",
]

# Maximum age of a tweet we'll accept into the feed. Anything older than this
# falls out of "recent news" territory and shouldn't fill the rumour mill.
TWITTER_MAX_AGE_HOURS = 48

# ── AFL CLUB NEWS PAGES ──────────────────────────────────────────────────────
# All 18 clubs use the AFL.com.au CMS, so their news lives at afl.com.au/news/{slug}
# Each club page has a JSON API endpoint we can hit directly
AFL_CLUB_SLUGS = {
    "Adelaide":         "adelaide",
    "Brisbane":         "brisbane",
    "Carlton":          "carlton",
    "Collingwood":      "collingwood",
    "Essendon":         "essendon",
    "Fremantle":        "fremantle",
    "Geelong":          "geelong",
    "Gold Coast":       "gold-coast-suns",
    "GWS Giants":       "greater-western-sydney",
    "Hawthorn":         "hawthorn",
    "Melbourne":        "melbourne",
    "North Melbourne":  "north-melbourne",
    "Port Adelaide":    "port-adelaide",
    "Richmond":         "richmond",
    "St Kilda":         "st-kilda",
    "Sydney":           "sydney-swans",
    "West Coast":       "west-coast-eagles",
    "Western Bulldogs": "western-bulldogs",
}

# AFL.com.au content API — returns JSON for each club's news
AFL_CONTENT_API = "https://aflapi.afl.com.au/afl/v2/articles?tagNames=news-{slug}&pageSize=10&tagNames=news-{slug}"

# ── AFL.com.au RSS FEEDS ────────────────────────────────────────────────────
AFL_RSS_FEEDS = [
    # Main AFL news RSS — confirmed working
    ("https://www.afl.com.au/rss",                           "AFL.com.au",   96),
    # AFL Fantasy specific news
    ("https://www.afl.com.au/fantasy/news",                  "AFL Fantasy",  95),
    # ABC Sport AFL — reliable, independent, good injury coverage
    ("https://www.abc.net.au/news/feed/7077144/rss.xml",     "ABC Sport",    88),
    # The Roar AFL — good fantasy commentary
    ("https://www.theroar.com.au/afl/feed/",                 "The Roar",     75),
    # Herald Sun AFL — behind paywall but headlines still in RSS
    ("https://www.heraldsun.com.au/sport/afl/rss",           "Herald Sun",   80),
]

# The AFL injury list is a HTML page, not RSS — scraped separately
AFL_INJURY_PAGE = "https://www.afl.com.au/matches/injury-list"
# AFL team selections page
AFL_TEAMS_PAGE  = "https://www.afl.com.au/matches/team-selection"
# AFL "Medical Room" weekly article — author + ETA + body part per club, table-formatted.
# The article ID and round suffix change weekly; _find_latest_medical_room() probes
# the AFL news listing for the freshest URL and falls back to this seed.
AFL_MEDICAL_ROOM_URL = "https://www.afl.com.au/news/1522891/medical-room-the-full-afl-injury-list-r11"
AFL_NEWS_LIST_URL    = "https://www.afl.com.au/news"

# Footywire pages
FW_BASE        = "https://www.footywire.com/afl/footy"
FW_INJURY_URL  = f"{FW_BASE}/injury_list"
FW_SELECT_URL  = f"{FW_BASE}/selection_changes"
FW_NEWS_URL    = f"{FW_BASE}/afl_news"

# ── HTTP SESSION ─────────────────────────────────────────────────────────────

def make_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en-AU,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT":             "1",
    })
    return s

def fetch(session, url, timeout=12):
    try:
        time.sleep(0.8)
        r = session.get(url, timeout=timeout)
        if r.status_code == 200:
            return r
        if r.status_code == 404:
            log.debug(f"HTTP 404 (not found, skipping): {url}")
        elif r.status_code == 403:
            log.warning(f"HTTP 403 (blocked — run from home network): {url}")
        elif r.status_code == 429:
            log.warning(f"HTTP 429 (rate limited — slowing down): {url}")
            time.sleep(10)
        else:
            log.debug(f"HTTP {r.status_code}: {url}")
        return None
    except Exception as e:
        log.warning(f"Fetch failed {url}: {e}")
        return None

# ── PLAYER NAME MATCHING ─────────────────────────────────────────────────────

def build_player_index(players):
    """Build lookup dict for fast player name matching."""
    idx = {}
    for p in players:
        name = p["name"].lower()
        idx[name] = p["id"]
        # Last name only
        last = name.split()[-1]
        if last not in idx:
            idx[last] = p["id"]
        # First initial + last (e.g. "n daicos")
        parts = name.split()
        if len(parts) >= 2:
            short = parts[0][0] + " " + parts[-1]
            idx[short] = p["id"]
    return idx

def find_player(text, player_idx):
    """Find a player mentioned in text. Returns (player_id, player_name) or (None, None)."""
    text_lower = text.lower()
    # Try full names first (longer matches = more specific)
    matches = [(name, pid) for name, pid in player_idx.items() if name in text_lower]
    if not matches:
        return None, None
    # Return the longest matching name (most specific)
    best = max(matches, key=lambda x: len(x[0]))
    return best[1], best[0].title()

# ── FOOTYWIRE INJURY LIST ─────────────────────────────────────────────────────

def scrape_fw_injuries(session, player_idx):
    """
    Scrape Footywire injury list.
    Returns list of news items — one per injured player.
    """
    items = []
    log.info("Scraping Footywire injury list...")
    r = fetch(session, FW_INJURY_URL)
    if not r:
        return items

    soup = BeautifulSoup(r.text, "lxml")

    # Footywire injury table: Player | Club | Injury | Likely Return
    table = None
    for t in soup.find_all("table"):
        headers = [th.get_text(strip=True).lower() for th in t.find_all("th")]
        if any("player" in h or "name" in h for h in headers):
            table = t
            break

    if not table:
        # Try finding any table with injury-looking content
        for t in soup.find_all("table"):
            rows = t.find_all("tr")
            if len(rows) > 5:
                table = t
                break

    if not table:
        log.warning("Footywire injury list: no table found")
        return items

    rows = table.find_all("tr")[1:]  # skip header
    for row in rows:
        cells = [td.get_text(strip=True) for td in row.find_all(["td","th"])]
        if len(cells) < 3:
            continue

        # Best guess at columns: name, club, injury, return
        name    = cells[0] if cells else ""
        club    = cells[1] if len(cells) > 1 else ""
        injury  = cells[2] if len(cells) > 2 else ""
        eta     = cells[3] if len(cells) > 3 else ""

        if not name or len(name) < 3:
            continue

        # Determine status
        combined = (injury + " " + eta).lower()
        if any(x in combined for x in ("indefinite","season","out","omit","test")):
            status = "out"
        elif any(x in combined for x in ("tbc","managed","test","doubtful","uncertain")):
            status = "tbc"
        else:
            continue  # not injured enough to report

        headline = f"{name} — {status.upper()}: {injury}"
        if eta:
            headline += f" ({eta})"

        body = f"{name} ({club}) injury status: {injury}. Expected return: {eta or 'unknown'}."

        result = classify_item(body, headline)
        if not result["relevant"]:
            continue

        pid, pname = find_player(name, player_idx)

        items.append({
            "id":          None,
            "type":        "injury",
            "category":    "injury_out" if status == "out" else "injury_tbc",
            "urgent":      status == "out",
            "player":      name,
            "pid":         pid,
            "team":        club,
            "pos":         None,
            "source":      "Footywire",
            "sourceHandle":"@footywire",
            "reliability": 94,
            "time":        "latest",
            "timeLabel":   "Latest",
            "headline":    headline,
            "body":        body,
            "signal":      "sell" if status == "out" else "hold",
            "signalConf":  85 if status == "out" else 60,
            "tags":        [status.upper(), injury[:30], eta or ""],
            "stats":       [
                {"l":"Status",  "v": status.upper()},
                {"l":"Injury",  "v": injury[:20]},
                {"l":"ETA",     "v": eta or "Unknown"},
                {"l":"Club",    "v": club},
            ],
            "relevance":   result["score"],
            "_source":     "footywire_injuries",
        })

    log.info(f"Footywire injuries: {len(items)} relevant items")
    return items


# ── FOOTYWIRE SELECTION CHANGES ───────────────────────────────────────────────

def scrape_fw_selections(session, player_idx):
    """
    Scrape Footywire selection changes page.
    Returns list of news items for ins, outs, emergencies, role changes.
    """
    items = []
    log.info("Scraping Footywire selection changes...")
    r = fetch(session, FW_SELECT_URL)
    if not r:
        return items

    soup = BeautifulSoup(r.text, "lxml")
    now  = datetime.now(timezone.utc)

    # Footywire selection changes: team by team, lists IN/OUT/EMG
    for team_div in soup.find_all(["div","table","section"], class_=re.compile("team|club|selection", re.I)):
        team_name = ""
        h = team_div.find(["h2","h3","h4","strong"])
        if h:
            team_name = h.get_text(strip=True)

        for row in team_div.find_all(["tr","li","p"]):
            text = row.get_text(strip=True)
            if len(text) < 5:
                continue

            result = classify_item(text)
            if not result["relevant"]:
                continue

            pid, pname = find_player(text, player_idx)

            # Determine change type from text
            tl = text.lower()
            if "vest" in tl or "sub" in tl:
                cat = "vest_risk"
            elif any(x in tl for x in ("forward pocket","half forward","role")):
                cat = "role_change"
            elif any(x in tl for x in ("named","selected","in for","replaces")):
                cat = "named"
            elif any(x in tl for x in ("omitted","dropped","out for","replaced by")):
                cat = "dropped"
            else:
                cat = "selection"

            items.append({
                "id":          None,
                "type":        "selection",
                "category":    cat,
                "urgent":      cat in ("vest_risk","dropped"),
                "player":      pname or "",
                "pid":         pid,
                "team":        team_name,
                "pos":         None,
                "source":      "Footywire",
                "sourceHandle":"@footywire",
                "reliability": 94,
                "time":        "latest",
                "timeLabel":   "Latest",
                "headline":    text[:120],
                "body":        text,
                "signal":      "sell" if cat in ("dropped","vest_risk") else ("hold" if cat == "role_change" else None),
                "signalConf":  70,
                "tags":        [cat.replace("_"," ").title(), team_name],
                "stats":       [],
                "relevance":   result["score"],
                "_source":     "footywire_selections",
            })

    log.info(f"Footywire selections: {len(items)} relevant items")
    return items


# ── AFL.COM.AU RSS ────────────────────────────────────────────────────────────

def scrape_afl_rss(session, player_idx):
    """
    Parse AFL.com.au RSS feeds.
    These are official and highly reliable.
    """
    import xml.etree.ElementTree as ET
    items = []

    for feed_url, source_name, reliability in AFL_RSS_FEEDS:
        log.info(f"Fetching RSS: {source_name}...")
        r = fetch(session, feed_url)
        if not r:
            continue

        try:
            root = ET.fromstring(r.text)
        except ET.ParseError as e:
            log.warning(f"RSS parse error {feed_url}: {e}")
            continue

        feed_items = root.findall(".//item")
        log.info(f"  {source_name}: {len(feed_items)} raw items")

        for item in feed_items:
            title   = (item.findtext("title")       or "").strip()
            desc    = (item.findtext("description") or "").strip()
            link    = (item.findtext("link")        or "").strip()
            pub     = (item.findtext("pubDate")     or "").strip()
            content = (item.findtext("{http://purl.org/rss/1.0/modules/content/}encoded") or "").strip()

            # Use full content if available, otherwise description
            body_text = BeautifulSoup(content or desc, "lxml").get_text(strip=True)[:500]
            full_text = title + " " + body_text

            result = classify_item(full_text, title)
            if not result["relevant"]:
                continue

            pid, pname = find_player(full_text, player_idx)

            # Parse publish time
            try:
                from email.utils import parsedate_to_datetime
                pub_dt = parsedate_to_datetime(pub)
                delta  = datetime.now(timezone.utc) - pub_dt
                mins   = int(delta.total_seconds() / 60)
                if mins < 60:
                    time_label = f"{mins}m ago"
                elif mins < 1440:
                    time_label = f"{mins//60}h ago"
                else:
                    time_label = f"{mins//1440}d ago"
            except Exception:
                time_label = "recent"

            # Determine type
            cat = result["category"]
            item_type = (
                "injury"    if "injury" in cat else
                "selection" if cat in ("named","dropped","role_change","vest_risk") else
                "price"     if cat == "price" else
                "news"
            )

            # Signal based on category
            signal = None
            if cat == "injury_out":  signal = "sell"
            elif cat == "injury_tbc": signal = "hold"
            elif cat == "named":      signal = None
            elif cat == "dropped":    signal = "sell"
            elif cat == "role_change":signal = "hold"

            items.append({
                "id":          None,
                "type":        item_type,
                "category":    cat,
                "urgent":      cat in ("injury_out","dropped") and mins < 120,
                "player":      pname or "",
                "pid":         pid,
                "team":        None,
                "pos":         None,
                "source":      source_name,
                "sourceHandle":f"@{source_name.lower().replace(' ','')}",
                "reliability": reliability,
                "time":        time_label,
                "timeLabel":   time_label,
                "headline":    title[:150],
                "body":        body_text[:400],
                "link":        link,
                "signal":      signal,
                "signalConf":  80,
                "tags":        [cat.replace("_"," ").title()],
                "stats":       [],
                "relevance":   result["score"],
                "_source":     f"rss_{source_name.lower().replace(' ','')}",
            })

    log.info(f"RSS total: {len(items)} relevant items")
    return items


# ── TWITTER/X VIA NITTER ─────────────────────────────────────────────────────

def get_nitter_base(session):
    """Find a working Nitter instance."""
    for base in NITTER_INSTANCES:
        try:
            r = session.get(f"{base}/aflcomau", timeout=8)
            if r.status_code == 200 and "tweet" in r.text.lower():
                log.info(f"Nitter: using {base}")
                return base
        except Exception:
            continue
    log.warning("Nitter: no working instance found")
    return None

_TWEET_DATE_FMT = "%b %d, %Y · %I:%M %p UTC"  # e.g. "Mar 18, 2026 · 3:45 PM UTC"

def _tweet_age_hours(tweet, now=None):
    """
    Parse the tweet's timestamp out of Nitter's <a class="tweet-date"> anchor
    (its <span title="..."> holds the absolute UTC time). Returns age in
    hours, or None when the timestamp can't be recovered.
    """
    if now is None: now = datetime.now(timezone.utc)
    parent = tweet.find_parent()
    if not parent: return None
    span_with_title = parent.find(["a","span","time"], title=True)
    if not span_with_title:
        # Try walking up one more level — Nitter sometimes wraps the date
        # in a sibling container outside the immediate parent.
        gp = parent.find_parent()
        if gp:
            span_with_title = gp.find(["a","span","time"], title=True)
    if not span_with_title: return None
    title = (span_with_title.get("title") or "").strip()
    if not title: return None
    try:
        dt = datetime.strptime(title, _TWEET_DATE_FMT).replace(tzinfo=timezone.utc)
        return (now - dt).total_seconds() / 3600
    except ValueError:
        return None


def _tweet_age_label(age_hours):
    """Format an age (in hours) as a compact relative label, like Nitter does."""
    if age_hours is None: return "recent"
    if age_hours < 1:     return f"{int(age_hours * 60)}m ago"
    if age_hours < 24:    return f"{int(age_hours)}h ago"
    return f"{int(age_hours // 24)}d ago"


def _scrape_nitter_page(session, nitter_base, url_path, source_name, source_handle, reliability, player_idx, now):
    """
    Helper: fetch one Nitter page (account timeline OR /search), iterate its
    tweet-content nodes, drop anything older than TWITTER_MAX_AGE_HOURS,
    return the matching items. Shared between account scraping and the
    hashtag/keyword search path so the recency/relevance/dedupe logic lives
    in one place.
    """
    items = []
    url = f"{nitter_base}{url_path}"
    log.info(f"Nitter fetch: {source_handle} -> {url_path}")
    r = fetch(session, url)
    if not r:
        return items

    soup = BeautifulSoup(r.text, "lxml")
    tweet_divs = (
        soup.find_all("div", class_="tweet-content")
        or soup.find_all("div", class_=re.compile("tweet|status", re.I))
    )

    fresh = stale = 0
    for tweet in tweet_divs[:30]:
        text = tweet.get_text(separator=" ", strip=True)
        if len(text) < 20:
            continue

        # Recency gate first — cheap and most tweets get dropped here.
        age = _tweet_age_hours(tweet, now=now)
        if age is not None and age > TWITTER_MAX_AGE_HOURS:
            stale += 1
            continue
        fresh += 1

        # Skip junk retweets (RT @... ...) unless they reference AFL fantasy keywords
        if text.startswith("RT @") and not any(kw in text.lower() for kw in
            ("injury", "out", "tbc", "named", "vest", "sc", "supercoach",
             "fantasy", "price", "breakeven", "concussion", "hamstring", "knee")):
            continue

        result = classify_item(text)
        if not result["relevant"]:
            continue

        pid, pname = find_player(text, player_idx)

        # Official sources (AFL.com.au, Champion Data, club accounts, beat reporters
        # at major outlets) get type="news"/"injury"/"selection". Fantasy talk + low
        # confidence get type="rumour" so the rumour mill picks them up.
        is_official = source_handle.lower() in ("@aflcomau", "@champdata", "@foxfooty",
                                                 "@aflplayers", "@heraldsunfooty")
        cat = result["category"]

        if is_official or result["score"] >= 60:
            item_type = (
                "injury"    if "injury" in cat else
                "selection" if cat in ("named", "dropped", "role_change", "vest_risk") else
                "price"     if cat == "price" else
                "news"
            )
        else:
            item_type = "rumour"

        signal = None
        if cat == "injury_out":   signal = "sell"
        elif cat == "injury_tbc": signal = "hold"
        elif cat == "dropped":    signal = "sell"
        elif cat == "role_change":signal = "hold"

        items.append({
            "id":          None,
            "type":        item_type,
            "category":    cat,
            "urgent":      cat == "injury_out" and is_official and (age is None or age <= 2),
            "player":      pname or "",
            "pid":         pid,
            "team":        None,
            "pos":         None,
            "source":      source_name,
            "sourceHandle":source_handle,
            "reliability": reliability,
            "time":        _tweet_age_label(age),
            "timeLabel":   _tweet_age_label(age),
            "age_hours":   age,
            "headline":    text[:140],
            "body":        text[:400],
            "signal":      signal,
            "signalConf":  reliability - 10,
            "tags":        [cat.replace("_"," ").title(), "Twitter"],
            "stats":       [],
            "is_rumour":   item_type == "rumour",
            "relevance":   result["score"],
            "_source":     f"twitter_{source_handle.lstrip('@')}",
        })

    if stale:
        log.debug(f"  {source_handle}: {fresh} fresh, dropped {stale} older than {TWITTER_MAX_AGE_HOURS}h")
    return items


def scrape_twitter(session, player_idx):
    """
    Scrape Twitter/X via Nitter for AFL news scoops, beat-reporter posts, and
    fantasy chatter. No API key needed. Items are filtered to the last
    TWITTER_MAX_AGE_HOURS (default 48) using the absolute timestamp in
    Nitter's <a class="tweet-date" title="..."> attribute, NOT the relative
    "2h ago" string (which can be stale-cached on slow mirrors).

    Items from non-official handles or with low classifier scores are tagged
    type="rumour" so the rumour mill picks them up. Official-source breaking
    injuries within 2h of posting are tagged urgent.
    """
    items = []
    nitter = get_nitter_base(session)
    if not nitter:
        log.warning("Twitter scraping skipped — no working Nitter instance")
        return items

    now = datetime.now(timezone.utc)

    # 1. Account timelines
    for handle, source_name, reliability in TWITTER_ACCOUNTS:
        items += _scrape_nitter_page(
            session, nitter, f"/{handle}", source_name, f"@{handle}",
            reliability, player_idx, now,
        )
        time.sleep(0.8)   # polite

    # 2. Hashtag / keyword search — surfaces scoops from accounts we don't follow
    for query in TWITTER_SEARCH_QUERIES:
        from urllib.parse import quote_plus
        path = f"/search?q={quote_plus(query)}&f=tweets"
        items += _scrape_nitter_page(
            session, nitter, path, f"Search: {query}", f"@search/{query}",
            70, player_idx, now,
        )
        time.sleep(0.8)

    # Dedupe by headline within this batch — search pages can repeat tweets
    # we already pulled from individual accounts.
    seen_heads = set()
    deduped = []
    for it in items:
        key = (it.get("player","").lower(), (it.get("headline") or "")[:80].lower())
        if key in seen_heads: continue
        seen_heads.add(key)
        deduped.append(it)

    log.info(f"Twitter: {len(deduped)} items kept (within {TWITTER_MAX_AGE_HOURS}h, deduped)")
    return deduped


def scrape_afl_injury_page(session, player_idx):
    """
    Scrape the official AFL injury list page.
    Much richer than RSS — has every player, injury type, and ETA.
    URL: https://www.afl.com.au/matches/injury-list
    """
    items = []
    log.info("Scraping AFL official injury list page...")
    r = fetch(session, AFL_INJURY_PAGE)
    if not r:
        return items

    soup = BeautifulSoup(r.text, "lxml")
    now  = datetime.now(timezone.utc)

    # The page groups injuries by club
    # Look for club sections with player injury tables
    club_sections = soup.find_all(["section","div"], class_=re.compile("club|team|squad", re.I))
    if not club_sections:
        # Fallback: find any tables with injury-like columns
        club_sections = [soup]

    for section in club_sections:
        # Get club name from heading
        heading = section.find(["h2","h3","h4","strong","span"], class_=re.compile("club|team|name|title", re.I))
        club_name = heading.get_text(strip=True) if heading else ""

        # Find player rows
        for row in section.find_all(["tr","li","div"], class_=re.compile("player|injury|row|item", re.I)):
            cells = [c.get_text(strip=True) for c in row.find_all(["td","span","div","p"]) if c.get_text(strip=True)]
            if len(cells) < 2:
                continue

            text = " ".join(cells)
            result = classify_item(text)
            if not result["relevant"]:
                continue

            pid, pname = find_player(text, player_idx)
            if not pname:
                continue

            # Try to extract injury and ETA
            injury = cells[1] if len(cells) > 1 else ""
            eta    = cells[2] if len(cells) > 2 else ""

            status_lower = (injury + " " + eta).lower()
            if any(x in status_lower for x in ("out","omit","season","indefinite")):
                cat = "injury_out"; signal = "sell"
            elif any(x in status_lower for x in ("tbc","test","managed","doubtful")):
                cat = "injury_tbc"; signal = "hold"
            else:
                continue

            headline = f"{pname} — {cat.replace('_',' ').upper()}: {injury}"
            if eta: headline += f" ({eta})"

            items.append({
                "id":          None,
                "type":        "injury",
                "category":    cat,
                "urgent":      cat == "injury_out",
                "player":      pname,
                "pid":         pid,
                "team":        club_name,
                "pos":         None,
                "source":      "AFL.com.au",
                "sourceHandle":"@aflcomau",
                "reliability": 96,
                "time":        "latest",
                "timeLabel":   "Latest",
                "headline":    headline,
                "body":        f"Official AFL injury update: {pname} ({club_name}). {injury}. Return: {eta or 'unknown'}.",
                "signal":      signal,
                "signalConf":  88,
                "tags":        [cat.replace("_"," ").title(), injury[:25], eta or ""],
                "stats":       [
                    {"l":"Status",  "v": "OUT" if cat=="injury_out" else "TBC"},
                    {"l":"Injury",  "v": injury[:20]},
                    {"l":"ETA",     "v": eta or "Unknown"},
                    {"l":"Club",    "v": club_name},
                ],
                "relevance":   result["score"] + 20,  # boost official AFL source
                "_source":     "afl_injury_page",
            })

    log.info(f"AFL injury page: {len(items)} relevant items")
    return items


# Body parts the AFL Medical Room article commonly uses, listed priority-first
# so "Foot/Achilles" -> "Achilles", "Leg/Calf" -> "Calf".
_INJURY_BODY_PARTS = [
    "achilles","concussion","hamstring","shoulder","collarbone",
    "ankle","knee","groin","quad","calf","thigh","shin","hip",
    "back","ribs","chest","abdomen","elbow","wrist","hand","finger",
    "thumb","foot","toe","leg","arm","neck","head","jaw","nose",
    "eye","face","illness","suspension","personal","managed","rest",
]

# Canonical-name swaps applied after the priority-list match. "Head"
# without further qualification = Concussion in AFL injury parlance; the
# clubs use Jaw/Eye/Nose/Face for non-concussion head/face injuries.
_INJURY_BODY_PART_ALIASES = {
    "Head": "Concussion",
}

def _injury_body_part(text):
    if not text: return ""
    lt = text.lower()
    for bp in _INJURY_BODY_PARTS:
        if bp in lt:
            canon = bp.capitalize()
            return _INJURY_BODY_PART_ALIASES.get(canon, canon)
    tokens = re.split(r"[\s/]+", text.strip())
    tokens = [t for t in tokens if t.lower() not in ("left","right","lower","upper")]
    canon = tokens[-1].capitalize() if tokens else text.strip().capitalize()
    return _INJURY_BODY_PART_ALIASES.get(canon, canon)


def _classify_returning(text):
    """Same shape as fetch_data._classify_injury_returning — returns (status, eta_display).
    status ∈ {out, test, available}; eta is a tidy display string."""
    if not text:
        return "test", "TBC"
    raw   = text.strip()
    lower = raw.lower()
    if lower in ("test","tbc","managed","rested"):
        return "test", "TBC" if lower == "tbc" else raw.title()
    if "season" in lower or "indef" in lower or "career" in lower:
        return "out", "Season"
    if re.search(r"\d+\s*\+?\s*(?:-\s*\d+\s*)?week", lower):
        return "out", re.sub(r"\s+", " ", raw)
    if re.search(r"\d+\s*\+?\s*(?:-\s*\d+\s*)?month", lower):
        return "out", re.sub(r"\s+", " ", raw)
    if re.match(r"round\s*\d+", lower):
        return "out", raw
    if "avail" in lower or "clear" in lower or "fit" in lower:
        return "available", raw
    return "test", raw or "TBC"


def _find_latest_medical_room(session):
    """Discover the most recent AFL Medical Room article URL by scanning the
    AFL news listing for any link containing 'medical-room'. Falls back to
    the hardcoded AFL_MEDICAL_ROOM_URL when discovery fails."""
    try:
        r = fetch(session, AFL_NEWS_LIST_URL)
        if r:
            soup = BeautifulSoup(r.text, "lxml")
            for a in soup.find_all("a", href=re.compile(r"medical-room.*injury-list", re.I)):
                href = a.get("href","")
                if href.startswith("/"):
                    return "https://www.afl.com.au" + href
                if href.startswith("http"):
                    return href
    except Exception as e:
        log.debug(f"Medical Room discovery failed: {e}")
    return AFL_MEDICAL_ROOM_URL


def scrape_afl_medical_room(session, player_idx):
    """
    Scrape AFL.com.au's weekly Medical Room article — the official, hand-curated
    injury list with status, body part, and ETA per player, grouped by club.
    Each club section is a 3-column table:
        PLAYER | INJURY | ESTIMATED RETURN
    The same article also has "In the mix" prose with managed/available updates,
    but we only parse the structured table for now.

    Returns one item per listed player. Source priority is high (96) — second
    only to the official AFL team-selection page.
    """
    items = []
    url = _find_latest_medical_room(session)
    log.info(f"Scraping AFL Medical Room: {url}")
    r = fetch(session, url)
    if not r:
        log.info("AFL Medical Room: article unavailable, skipping")
        return items

    soup = BeautifulSoup(r.text, "lxml")
    seen = set()   # (pid_or_name, bp) — dedupe within the article

    # The article body has exactly one 3-column injury table per club, in
    # alphabetical order. The DOM has no club headings (the visual cue is the
    # promo-image preceding each table), so we identify clubs by table order.
    AFL_CLUB_ORDER = [
        "Adelaide", "Brisbane", "Carlton", "Collingwood", "Essendon",
        "Fremantle", "Geelong", "Gold Coast", "GWS Giants", "Hawthorn",
        "Melbourne", "North Melbourne", "Port Adelaide", "Richmond",
        "St Kilda", "Sydney", "West Coast", "Western Bulldogs",
    ]
    injury_tables = [
        t for t in soup.find_all("table")
        if {"player","injury","return"}.issubset(
            set(" ".join(th.get_text(strip=True).lower()
                         for th in t.find_all("th")).split()
            ))
    ]

    # Also build a fallback map keyed on a leading promo-image filename so we
    # can verify alphabetical order is still intact (Indigenous names appear at
    # specific alphabetical slots — kuwarna=Adelaide, walyalup=Fremantle,
    # narrm=Melbourne, yartapuulti=Port Adelaide, euro-yroke=St Kilda,
    # waalitj-marawar=West Coast).
    IMAGE_TO_CLUB = {
        "kuwarna": "Adelaide", "walyalup": "Fremantle", "narrm": "Melbourne",
        "yartapuulti": "Port Adelaide", "euro-yroke": "St Kilda",
        "waalitj-marawar": "West Coast",
    }
    img_pat = re.compile(r"/photo-resources/[^/]+/[^/]+/(\w[\w\-]+?)\.jpg", re.I)
    def club_from_preceding_image(table):
        cursor = table
        for _ in range(8):
            cursor = cursor.find_previous("section", class_=re.compile("promo-image", re.I))
            if not cursor: break
            blob = cursor.decode() if hasattr(cursor,"decode") else str(cursor)
            for m in img_pat.finditer(blob):
                fn = m.group(1).lower()
                for key, club in IMAGE_TO_CLUB.items():
                    if key in fn:
                        return club
        return ""

    for table_idx, table in enumerate(injury_tables):
        # Primary: position in the alphabetical 18-club sequence. The article
        # body has no club headings, but its tables ARE in alphabetical order
        # (verified against the Indigenous-name promo images at positions
        # 1/6/11/13/15/17, which match Adelaide/Fremantle/Melbourne/
        # Port Adelaide/St Kilda/West Coast).
        club_name = AFL_CLUB_ORDER[table_idx] if table_idx < len(AFL_CLUB_ORDER) else ""

        # Defensive cross-check: if the promo-image filename preceding the
        # table identifies a known club AND it disagrees with our position
        # guess, prefer the image (article reorderings happen).
        img_club = club_from_preceding_image(table)
        if img_club and club_name and img_club != club_name:
            log.debug(f"Medical Room: image says {img_club!r}, "
                      f"position says {club_name!r} (table {table_idx}) — going with image")
            club_name = img_club

        for row in table.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) < 3: continue
            name   = cells[0].get_text(strip=True)
            injury = cells[1].get_text(strip=True)
            eta    = cells[2].get_text(strip=True)
            if not name or len(name) < 3: continue
            if name.lower().startswith("updated"): continue
            if name.lower() == "player": continue   # repeated header row

            status, eta_disp = _classify_returning(eta)
            body_part = _injury_body_part(injury)
            pid, pname = find_player(name, player_idx)

            dedupe_key = (pid or name.lower(), body_part or injury.lower())
            if dedupe_key in seen: continue
            seen.add(dedupe_key)

            cat = "injury_out" if status == "out" else "injury_tbc" if status == "test" else "injury_available"
            display_name = pname or name
            headline = f"{display_name} — {status.upper()}: {body_part or injury}"
            if eta_disp: headline += f" ({eta_disp})"

            items.append({
                "id":          None,
                "type":        "injury",
                "category":    cat,
                "urgent":      status == "out",
                "player":      display_name,
                "pid":         pid,
                "team":        club_name,
                "pos":         None,
                "source":      "AFL.com.au",
                "sourceHandle":"@aflcomau",
                "reliability": 96,
                "time":        "latest",
                "timeLabel":   "Latest",
                "headline":    headline,
                "body":        (f"Official AFL Medical Room update for {club_name}: "
                                f"{display_name} ({body_part or injury}). Estimated return: {eta_disp}."),
                "signal":      "sell" if status == "out" else "hold" if status == "test" else "buy",
                "signalConf":  88,
                "tags":        [status.upper(), body_part or injury, eta_disp],
                "stats":       [
                    {"l":"Status",  "v": status.upper()},
                    {"l":"Injury",  "v": body_part or injury[:20]},
                    {"l":"ETA",     "v": eta_disp or "Unknown"},
                    {"l":"Club",    "v": club_name},
                ],
                "relevance":   80,   # weekly official article = high signal
                "_source":     "afl_medical_room",
            })

    log.info(f"AFL Medical Room: {len(items)} player items across clubs")
    return items


def scrape_afl_team_selections(session, player_idx):
    """
    Scrape the official AFL.com.au team-selection page for the current round.
    This is the source of truth for "named" / "omitted" / "late out" events,
    so it leads the scraper pipeline per source-priority spec #4.

    Emits one item per player whose selection state we can identify on the page
    (named in starting 22, named on extended bench, omitted, late out, sub).
    Downstream NewsHistory.filter_real_time then drops items that haven't
    changed since the previous scrape — so we don't re-emit "Daicos named"
    every 15 minutes.
    """
    items = []
    log.info("Scraping AFL.com.au team selection page...")
    r = fetch(session, AFL_TEAMS_PAGE)
    if not r:
        log.info("AFL team selections: page unavailable, skipping")
        return items

    soup = BeautifulSoup(r.text, "lxml")

    # AFL.com.au groups each match into a card with two club sections, each
    # listing named/emergency/omitted players. The DOM uses kebab-case classes
    # that have rotated over the years, so probe a few patterns.
    match_cards = (
        soup.find_all(["section","div"], class_=re.compile("team-selection|team-?announcement|match-?selection", re.I)) or
        soup.find_all(["section","div"], class_=re.compile("matches?-team|round-?team", re.I))
    )
    if not match_cards:
        # Fallback: parse all club blocks on the page
        match_cards = soup.find_all(["section","div"], class_=re.compile("club|team", re.I))

    seen_keys = set()
    for card in match_cards:
        club_el = card.find(["h2","h3","h4","span","div"], class_=re.compile("club|team|squad|name", re.I))
        club_name = club_el.get_text(strip=True) if club_el else ""

        # Each player block in the lineup
        for slot in card.find_all(["li","div","tr"], class_=re.compile("player|lineup|name", re.I)):
            text = slot.get_text(" ", strip=True)
            if len(text) < 3 or len(text) > 200:
                continue

            pid, pname = find_player(text, player_idx)
            if not pname:
                continue

            # Figure out the selection event from the slot context
            slot_text = text.lower()
            parent_cls = " ".join((slot.get("class") or [])).lower()
            section_cls = " ".join((card.get("class") or [])).lower()
            blob = slot_text + " " + parent_cls + " " + section_cls

            if any(k in blob for k in ("late out","late-out","late withdrawal","withdrawn")):
                cat, signal, urgent, label = "dropped", "sell", True,  "Late out"
            elif "sub" in blob or "vest" in blob or "medsub" in blob or "medical sub" in blob:
                cat, signal, urgent, label = "vest_risk", "hold", False, "Medical sub"
            elif any(k in blob for k in ("emergenc",)):
                cat, signal, urgent, label = "named", None, False, "Emergency"
            elif any(k in blob for k in ("omit","dropped","out of side","not selected")):
                cat, signal, urgent, label = "dropped", "sell", False, "Omitted"
            elif any(k in blob for k in ("named","selected","in for","replaces","starting","interchange")):
                cat, signal, urgent, label = "named", None, False, "Named"
            else:
                continue   # not a recognisable selection event

            # Dedupe within this page (player can appear in multiple list slots)
            key = f"{pid}|{cat}"
            if key in seen_keys: continue
            seen_keys.add(key)

            headline = f"{pname} — {label} ({club_name})" if club_name else f"{pname} — {label}"
            items.append({
                "id":          None,
                "type":        "selection",
                "category":    cat,
                "urgent":      urgent,
                "player":      pname,
                "pid":         pid,
                "team":        club_name,
                "pos":         None,
                "source":      "AFL.com.au",
                "sourceHandle":"@aflcomau",
                "reliability": 96,
                "time":        "latest",
                "timeLabel":   "Latest",
                "headline":    headline,
                "body":        f"Official AFL.com.au team selection: {pname} {label.lower()} for {club_name}.",
                "signal":      signal,
                "signalConf":  85,
                "tags":        [label, club_name],
                "stats":       [],
                "relevance":   75,   # selection events are high-value
                "_source":     "afl_team_selections",
            })

    log.info(f"AFL team selections: {len(items)} candidate items (history filter will drop unchanged)")
    return items


def scrape_club_news(session, player_idx):
    """
    Scrape news from all 18 AFL club pages via AFL.com.au.
    Each club's news is hosted on the AFL CMS at afl.com.au/news/{club-slug}.
    Uses the AFL content API which returns JSON — much cleaner than HTML parsing.
    """
    import xml.etree.ElementTree as ET
    items = []
    log.info("Scraping AFL club news pages (all 18 clubs)...")

    for team_name, slug in AFL_CLUB_SLUGS.items():
        # Try the AFL content API first (JSON)
        api_url = f"https://aflapi.afl.com.au/afl/v2/articles?tagNames=news-{slug}&pageSize=8"
        r = fetch(session, api_url)

        if r and r.status_code == 200:
            try:
                data = r.json()
                articles = data.get("articles", data.get("data", []))
                for article in articles[:8]:
                    title   = article.get("title","") or article.get("heading","")
                    summary = article.get("summary","") or article.get("description","") or article.get("subtitle","")
                    pub     = article.get("publishedDate","") or article.get("published","")

                    full_text = title + " " + summary
                    result = classify_item(full_text, title)
                    if not result["relevant"]:
                        continue

                    pid, pname = find_player(full_text, player_idx)

                    # Parse time
                    try:
                        from email.utils import parsedate_to_datetime
                        from datetime import timezone
                        pub_dt = parsedate_to_datetime(pub) if pub else None
                        if pub_dt:
                            delta = datetime.now(timezone.utc) - pub_dt
                            mins  = int(delta.total_seconds() / 60)
                            time_label = f"{mins}m ago" if mins < 60 else f"{mins//60}h ago" if mins < 1440 else f"{mins//1440}d ago"
                        else:
                            time_label = "recent"
                    except Exception:
                        time_label = "recent"

                    cat = result["category"]
                    item_type = (
                        "injury"    if "injury" in cat else
                        "selection" if cat in ("named","dropped","role_change","vest_risk") else
                        "price"     if cat == "price" else
                        "news"
                    )
                    signal = None
                    if cat == "injury_out":   signal = "sell"
                    elif cat == "injury_tbc": signal = "hold"
                    elif cat == "dropped":    signal = "sell"
                    elif cat == "role_change":signal = "hold"

                    items.append({
                        "id":          None,
                        "type":        item_type,
                        "category":    cat,
                        "urgent":      cat == "injury_out",
                        "player":      pname or "",
                        "pid":         pid,
                        "team":        team_name,
                        "pos":         None,
                        "source":      f"{team_name} FC",
                        "sourceHandle":f"@{slug.replace('-','')}",
                        "reliability": 90,
                        "time":        time_label,
                        "timeLabel":   time_label,
                        "headline":    title[:150],
                        "body":        summary[:400],
                        "signal":      signal,
                        "signalConf":  80,
                        "tags":        [cat.replace("_"," ").title(), team_name],
                        "stats":       [],
                        "relevance":   result["score"],
                        "_source":     f"club_{slug}",
                    })
                continue  # success via API, skip HTML fallback

            except (ValueError, KeyError):
                pass  # fall through to HTML scrape

        # Fallback: scrape the HTML club news page
        html_url = f"https://www.afl.com.au/news/{slug}"
        r2 = fetch(session, html_url)
        if not r2:
            continue

        soup = BeautifulSoup(r2.text, "lxml")

        # AFL.com.au news cards — look for article headlines
        for card in soup.find_all(["article","div"], class_=re.compile("news|article|card|item", re.I))[:10]:
            headline_el = card.find(["h1","h2","h3","h4"])
            if not headline_el:
                continue

            title = headline_el.get_text(strip=True)
            summary_el = card.find(["p","span"], class_=re.compile("summary|desc|lead|excerpt", re.I))
            summary = summary_el.get_text(strip=True) if summary_el else ""

            full_text = title + " " + summary
            result = classify_item(full_text, title)
            if not result["relevant"]:
                continue

            pid, pname = find_player(full_text, player_idx)
            cat = result["category"]
            item_type = "injury" if "injury" in cat else "selection" if cat in ("named","dropped","role_change","vest_risk") else "news"
            signal = "sell" if cat=="injury_out" else "hold" if cat in ("injury_tbc","role_change") else None

            items.append({
                "id":          None,
                "type":        item_type,
                "category":    cat,
                "urgent":      cat == "injury_out",
                "player":      pname or "",
                "pid":         pid,
                "team":        team_name,
                "pos":         None,
                "source":      f"{team_name} FC",
                "sourceHandle":f"@{slug.replace('-','')}",
                "reliability": 90,
                "time":        "recent",
                "timeLabel":   "recent",
                "headline":    title[:150],
                "body":        summary[:400],
                "signal":      signal,
                "signalConf":  75,
                "tags":        [cat.replace("_"," ").title(), team_name],
                "stats":       [],
                "relevance":   result["score"],
                "_source":     f"club_{slug}",
            })

        time.sleep(0.5)  # polite delay between clubs

    log.info(f"Club news: {len(items)} relevant items from {len(AFL_CLUB_SLUGS)} clubs")
    return items


# ── RECENCY FILTER ────────────────────────────────────────────────────────────

def filter_recent(items, max_age_hours=48):
    """
    Only keep items published within max_age_hours.
    Items without a parseable timestamp are kept (assumed recent).
    Footywire injury list items are always kept as they are structural data.
    """
    from datetime import timezone
    now = datetime.now(timezone.utc)
    recent = []
    for item in items:
        # Always keep structural injury/selection data from official sources
        if item.get("_source") in ("footywire_injuries","afl_injury_page","footywire_selections"):
            # But cap at 7 days — older than that it's stale
            time_label = item.get("timeLabel","latest").lower()
            if "latest" in time_label or "recent" in time_label:
                recent.append(item)
                continue

        # Parse time label for other sources
        time_label = item.get("timeLabel","") or item.get("time","")
        try:
            # Try to parse "Xm ago", "Xh ago", "Xd ago"
            tl = time_label.lower().replace(" ago","").strip()
            if tl.endswith("m"):
                age_hrs = int(tl[:-1]) / 60
            elif tl.endswith("h"):
                age_hrs = int(tl[:-1])
            elif tl.endswith("d"):
                age_hrs = int(tl[:-1]) * 24
            elif "latest" in tl or "recent" in tl:
                age_hrs = 0
            else:
                age_hrs = 0  # unknown — keep it
            
            if age_hrs <= max_age_hours:
                recent.append(item)
        except Exception:
            recent.append(item)  # keep if can't parse

    log.info(f"Recency filter: {len(recent)}/{len(items)} items within {max_age_hours}h")
    return recent


# ── RUMOUR FILTER BY PLAYER STATUS ───────────────────────────────────────────

def filter_rumours_by_status(items, players):
    """
    Drop rumour-mill items about players who are already confirmed OUT — those
    rumours are stale and add noise. For TBC/managed players, keep the rumour
    but flag it as low confidence so the UI can de-emphasise it.

    Applies only to items the rumour mill picks up (type == "rumour" or
    is_rumour == True or type == "twitter" with reliability < 90). Official
    news/injury/selection items are untouched.
    """
    if not players:
        return items

    out_ids   = {p["id"] for p in players if p.get("injuryStatus") == "out"}
    out_names = {(p.get("name") or "").lower() for p in players
                 if p.get("injuryStatus") == "out"}
    tbc_ids   = {p["id"] for p in players if p.get("injuryStatus") in ("tbc", "test")}
    tbc_names = {(p.get("name") or "").lower() for p in players
                 if p.get("injuryStatus") in ("tbc", "test")}

    kept, dropped_out, flagged_tbc = [], 0, 0
    for item in items:
        is_rumour = (
            item.get("type") == "rumour"
            or item.get("is_rumour") is True
            or (item.get("type") == "twitter" and (item.get("reliability") or 0) < 90)
        )
        if not is_rumour:
            kept.append(item)
            continue

        pid    = item.get("pid")
        pname  = (item.get("player") or "").lower()

        if (pid and pid in out_ids) or (pname and pname in out_names):
            dropped_out += 1
            continue  # confirmed OUT — drop rumour entirely

        if (pid and pid in tbc_ids) or (pname and pname in tbc_names):
            item = dict(item)
            item["low_confidence"] = True
            item["reliability"] = max(0, (item.get("reliability") or 60) - 20)
            item["signalConf"]  = max(0, (item.get("signalConf")  or 50) - 20)
            tags = list(item.get("tags") or [])
            if "Low Confidence" not in tags:
                tags.append("Low Confidence")
            item["tags"] = tags
            flagged_tbc += 1
        kept.append(item)

    if dropped_out or flagged_tbc:
        log.info(f"Rumour filter: dropped {dropped_out} (player OUT), "
                 f"flagged {flagged_tbc} as low-confidence (player TBC)")
    return kept


# ── DEDUPLICATION ─────────────────────────────────────────────────────────────

def deduplicate(items):
    """
    Remove near-duplicate items.
    Two items are duplicates if they're about the same player
    and have the same category within 2 hours.
    """
    seen = {}
    unique = []
    for item in items:
        key = f"{item.get('pid','none')}_{item.get('category','none')}"
        if key not in seen:
            seen[key] = item
            unique.append(item)
        else:
            # Keep the one with higher reliability
            existing = seen[key]
            if item["reliability"] > existing["reliability"]:
                idx = unique.index(existing)
                unique[idx] = item
                seen[key] = item
    return unique


# ── MAIN ─────────────────────────────────────────────────────────────────────

def scrape_all_news(players=None):
    """
    Run all scrapers and return merged, filtered, sorted news list.
    players: list of player dicts (from players.json or PLAYERS mock)
             If None, loads from players.json if available.
    """
    if players is None:
        players_path = BASE_DIR.parent / "players.json"
        if players_path.exists():
            data = json.loads(players_path.read_text())
            players = data.get("players", []) if isinstance(data, dict) else data
        else:
            players = []
            log.warning("No players.json found — player name matching disabled")

    player_idx = build_player_index(players)
    session    = make_session()
    all_items  = []

    # ── Run all scrapers ──
    # Source priority for real-time info (per spec #4):
    #   AFL.com.au official > Footywire > RSS > Twitter
    # AFL.com.au team selections lead because they are the source of truth
    # for named/omitted/late-out events.
    all_items += scrape_afl_team_selections(session, player_idx)
    time.sleep(1)
    all_items += scrape_afl_medical_room(session, player_idx)
    time.sleep(1)
    all_items += scrape_afl_injury_page(session, player_idx)
    time.sleep(1)
    all_items += scrape_fw_injuries(session, player_idx)
    time.sleep(1)
    all_items += scrape_fw_selections(session, player_idx)
    time.sleep(1)
    all_items += scrape_afl_rss(session, player_idx)
    time.sleep(1)
    all_items += scrape_twitter(session, player_idx)
    time.sleep(1)
    all_items += scrape_club_news(session, player_idx)

    # ── Recency filter (keep last 48h only) ──
    all_items = filter_recent(all_items, max_age_hours=48)

    # ── Deduplicate ──
    all_items = deduplicate(all_items)

    # ── Rumour-vs-status filter ──
    # Drop rumours about confirmed-OUT players (the rumour is stale), and flag
    # rumours about TBC/managed players as low-confidence rather than dropping.
    all_items = filter_rumours_by_status(all_items, players)

    # ── Apply history tracking (NEW / ONGOING / UPDATE / RESOLVED) ──
    history = NewsHistory()
    all_items = history.process(all_items)

    # ── Real-time-only filter (per spec): drop ongoing items where the player's
    # status and the item content haven't changed since the previous scrape.
    # This is what stops the feed re-emitting the same "Cripps TBC" 24×/day.
    before = len(all_items)
    all_items = history.filter_real_time(all_items)
    log.info(f"Real-time filter: kept {len(all_items)}/{before} items (dropped ongoing/no-change)")

    history.save()

    # ── Sort: urgent first, then NEW > UPDATE > RESOLVED, then by relevance ──
    status_rank = {"new": 0, "update": 1, "resolved": 2, "ongoing": 3}
    all_items.sort(key=lambda x: (
        0 if x.get("urgent") else 1,
        status_rank.get(x.get("status",""), 4),
        -x.get("relevance", 0)
    ))

    # ── Assign sequential IDs ──
    for i, item in enumerate(all_items, 1):
        item["id"] = i

    log.info(f"Total news items: {len(all_items)}")
    return all_items


def main():
    print("=" * 60)
    print("  AFLFantasyWire — News Scraper")
    print("=" * 60)
    print(f"  {datetime.now().strftime('%H:%M:%S  %d %b %Y')}\n")

    items = scrape_all_news()

    output = {
        "scraped_at":  datetime.now().isoformat(),
        "item_count":  len(items),
        "news":        items,
    }

    with open(OUTPUT_PATH, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\n✓ Wrote {len(items)} news items → {OUTPUT_PATH}")

    # Print summary
    by_type = {}
    for item in items:
        t = item["type"]
        by_type[t] = by_type.get(t, 0) + 1
    for t, count in sorted(by_type.items()):
        print(f"   {t}: {count}")

    urgent = [i for i in items if i.get("urgent")]
    if urgent:
        print(f"\n⚠  {len(urgent)} URGENT items:")
        for item in urgent[:5]:
            print(f"   [{item['source']}] {item['headline'][:70]}")
    print()


if __name__ == "__main__":
    main()
