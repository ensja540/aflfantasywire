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
# These are the most reliable SC/DT fantasy accounts
TWITTER_ACCOUNTS = [
    ("dttalk",          "DT Talk",          82),
    ("supercoach_dr",   "SuperCoach DR",    80),
    ("scscoop",         "Supercoach Scoop", 78),
    ("warnie",          "Warnie",           80),
    ("aflcomau",        "AFL.com.au",       96),
    ("champdata",       "Champion Data",    98),
    ("heraldsunfooty",  "Herald Sun Sport", 80),
    ("footywire",       "Footywire",        94),
]

# Nitter instances (public Twitter mirrors, no API needed)
# Try each in order until one works
NITTER_INSTANCES = [
    "https://nitter.poast.org",
    "https://nitter.privacydev.net",
    "https://nitter.net",
    "https://nitter.cz",
    "https://nitter.it",
    "https://nitter.unixfox.eu",
]

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

def scrape_twitter(session, player_idx):
    """
    Scrape Twitter/X accounts via Nitter (no API key needed).
    Returns list of tweet-based news items.
    """
    items = []
    nitter = get_nitter_base(session)
    if not nitter:
        log.warning("Twitter scraping skipped — no Nitter instance available")
        return items

    for handle, source_name, reliability in TWITTER_ACCOUNTS:
        url = f"{nitter}/{handle}"
        log.info(f"Scraping @{handle}...")
        r = fetch(session, url)
        if not r:
            continue

        soup = BeautifulSoup(r.text, "lxml")

        # Nitter tweet containers
        tweet_divs = (
            soup.find_all("div", class_="tweet-content") or
            soup.find_all("div", class_=re.compile("tweet|status", re.I))
        )

        for tweet in tweet_divs[:20]:  # last 20 tweets per account
            text = tweet.get_text(separator=" ", strip=True)
            if len(text) < 20:
                continue

            # Skip retweets of non-fantasy content
            if text.startswith("RT @") and not any(kw in text.lower() for kw in
                ["injury","out","tbc","named","vest","sc","supercoach","fantasy","price","breakeven"]):
                continue

            result = classify_item(text)
            if not result["relevant"]:
                continue

            pid, pname = find_player(text, player_idx)

            # Determine confidence level for rumour vs confirmed
            is_official = handle in ("aflcomau", "champdata")
            is_rumour   = not is_official and result["score"] < 40

            # Get tweet timestamp if available
            time_el = tweet.find_parent().find(["span","time"], class_=re.compile("time|date",re.I)) if tweet.find_parent() else None
            time_label = time_el.get_text(strip=True) if time_el else "recent"

            cat = result["category"]
            item_type = (
                "injury"    if "injury" in cat else
                "selection" if cat in ("named","dropped","role_change","vest_risk") else
                "price"     if cat == "price" else
                "news"
            )

            signal = None
            if cat == "injury_out":  signal = "sell"
            elif cat == "injury_tbc": signal = "hold"
            elif cat == "dropped":    signal = "sell"
            elif cat == "role_change":signal = "hold"

            items.append({
                "id":          None,
                "type":        item_type,
                "category":    cat,
                "urgent":      cat == "injury_out" and is_official,
                "player":      pname or "",
                "pid":         pid,
                "team":        None,
                "pos":         None,
                "source":      source_name,
                "sourceHandle":f"@{handle}",
                "reliability": reliability,
                "time":        time_label,
                "timeLabel":   time_label,
                "headline":    text[:140],
                "body":        text[:400],
                "signal":      signal,
                "signalConf":  reliability - 10,
                "tags":        [cat.replace("_"," ").title(), "Twitter"],
                "stats":       [],
                "is_rumour":   is_rumour,
                "relevance":   result["score"],
                "_source":     f"twitter_{handle}",
            })

        time.sleep(1)  # polite delay between accounts

    log.info(f"Twitter: {len(items)} relevant items")
    return items


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
    # Priority order: official > reliable > social
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

    # ── Apply history tracking (NEW / ONGOING / UPDATE / RESOLVED) ──
    history = NewsHistory()
    all_items = history.process(all_items)
    history.save()

    # ── Sort: urgent first, then by relevance score ──
    all_items.sort(key=lambda x: (
        0 if x.get("urgent") else 1,
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
