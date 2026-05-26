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
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Force UTF-8 stdout/stderr so the checkmark/arrow glyphs in our prints don't
# crash under a cp1252 console (scheduled task / auto_scrape subprocess).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("Run: pip install requests beautifulsoup4 lxml")
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).parent))
from news_filter import classify_item, is_relevant
from news_history import NewsHistory, HISTORY_PATH

# Windows consoles default to cp1252, which can't encode the ✓/⚠/→ glyphs in
# our status prints — that raises UnicodeEncodeError and aborts the run. Force
# UTF-8 (replacing anything unmappable) so a print never kills the scraper.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("news")

BASE_DIR    = Path(__file__).parent
# news.json must sit next to index.html (the repo root = this file's dir), NOT
# one level up. The old BASE_DIR.parent wrote to C:\news.json, outside the repo,
# so the frontend never saw the scraper's output.
OUTPUT_PATH = BASE_DIR / "news.json"

# Aggregator sites with no original fantasy content — items from these are dropped.
BLOCKED_SOURCES = ["news.com.au", "msn.com", "yahoo.com", "google.com/alerts"]

# Twitter/X is scraped via Nitter RSS feeds — see scrape_twitter_rss() and its
# NITTER_RSS_INSTANCES / TWITTER_RSS_ACCOUNTS config further down.

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
# Independent-outlet RSS feeds. AFL.com.au is handled separately via
# AFL_RSS_CANDIDATES (+ /news HTML fallback) in scrape_afl_rss().
AFL_RSS_FEEDS = [
    # ABC Sport AFL — reliable, independent, good injury coverage
    ("https://www.abc.net.au/news/feed/7077144/rss.xml",     "ABC Sport",    88),
    # The Roar AFL — good fantasy commentary
    ("https://www.theroar.com.au/afl/feed/",                 "The Roar",     75),
    # Herald Sun AFL — behind paywall but headlines still in RSS
    ("https://www.heraldsun.com.au/sport/afl/rss",           "Herald Sun",   80),
]

# Google News RSS aggregates dozens of AFL publishers and stays reachable from
# data-centre IPs when Nitter and AFL.com.au are blocked — our most reliable way
# to keep the feed (and rumour mill) populated beyond Footywire.
# (search query, reliability, is_rumour)
GOOGLE_NEWS_QUERIES = [
    ("AFL injury -AFLW -women",                                  78, False),
    ('AFL team selection OR omitted OR "late out" -AFLW -women', 78, False),
    ('"AFL Fantasy" OR SuperCoach -AFLW -women',                 70, False),
    ('AFL "back at training" OR "returned to training" OR "fitness test" OR "in doubt" -AFLW -women', 66, True),
    ('AFL "role change" OR "into the midfield" OR managed OR "injury cloud" OR "racing the clock" -AFLW -women', 62, True),
    ('AFL "set to return" OR recalled OR "pushing for selection" OR "named to return" OR "in the mix" -AFLW -women', 60, True),
    ('SuperCoach OR "AFL Fantasy" trade OR "cash cow" OR "price rise" OR "buy or sell" OR captain -AFLW -women', 58, True),
    ('AFL ("Tom Morris" OR "Jon Ralph" OR "Damian Barrett" OR "Cal Twomey" OR "Mitch Cleary") -AFLW -women', 66, True),
    ('AFL ("Riley Beveridge" OR "Josh Gabelich" OR "Sam Edmund" OR "DT Talk" OR "SuperCoach") news -AFLW -women', 60, True),
]
GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={q}+when:2d&hl=en-AU&gl=AU&ceid=AU:en"

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
FW_SELECT_URL  = f"{FW_BASE}/afl_team_selections"
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
        # Do NOT advertise "br" — brotli isn't decodable in this environment, and
        # AFL.com.au serves brotli when offered, yielding an unparseable body.
        "Accept-Encoding": "gzip, deflate",
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

_PID_NAME = {}
_PLAYERS_IDX = []          # [{pid, full, first, last}]
_SURNAME_FIRSTS = {}       # surname -> set of first names (to detect same-surname clashes)

def build_player_index(players):
    """Build player lookup tables. Returns a name->pid dict for backward
    compatibility, but find_player uses the richer _PLAYERS_IDX below."""
    idx = {}
    _PID_NAME.clear()
    _PLAYERS_IDX.clear()
    _SURNAME_FIRSTS.clear()
    for p in players:
        _PID_NAME[p["id"]] = p["name"]
        name = p["name"].lower().strip()
        idx[name] = p["id"]
        parts = name.split()
        if not parts:
            continue
        first, last = parts[0], parts[-1]
        _PLAYERS_IDX.append({"pid": p["id"], "full": name, "first": first, "last": last})
        _SURNAME_FIRSTS.setdefault(last, set()).add(first)
    return idx

def find_player_strict(name):
    """Match a clean "First Last" name to a tracked player by exact full name
    (or first+last), never by a loose surname hit. Returns (pid, full) or
    (None, None)."""
    parts = re.sub(r"[^a-z ]", " ", (name or "").lower()).split()
    if not parts:
        return None, None
    first, last, full = parts[0], parts[-1], " ".join(parts)
    for pl in _PLAYERS_IDX:
        if pl["full"] == full or (pl["first"] == first and pl["last"] == last):
            return pl["pid"], _PID_NAME[pl["pid"]]
    return None, None

def find_player(text, player_idx=None):
    """Find a player mentioned in text. Returns (player_id, full_name) or
    (None, None).

    Matching is deliberately conservative to avoid mis-attribution (e.g. an
    "Elijah Hewett" story being filed under "George Hewett"):
      1. Prefer a full-name match.
      2. For a surname-only hit, the word in front of the surname MUST equal the
         player's first name or initial. If a different first name precedes it,
         skip — it's a different same-surname player. A bare surname is only
         accepted when no other tracked player shares that surname.
    """
    t = (text or "").lower()
    if not t:
        return None, None

    # 1. Full-name match — longest wins (most specific).
    full = [pl for pl in _PLAYERS_IDX if pl["full"] in t]
    if full:
        best = max(full, key=lambda pl: len(pl["full"]))
        return best["pid"], _PID_NAME[best["pid"]]

    # 2. Surname match with first-name verification.
    for pl in sorted(_PLAYERS_IDX, key=lambda p: -len(p["last"])):
        last = pl["last"]
        if len(last) < 4:
            continue  # short surnames are too ambiguous on their own
        m = re.search(r"(?:([a-z][a-z'.-]*)\s+)?(?<![a-z])" + re.escape(last) + r"(?![a-z])", t)
        if not m:
            continue
        prev = m.group(1) or ""
        if prev:
            if prev == pl["first"] or (1 <= len(prev) <= 2 and prev[0] == pl["first"][0]):
                return pl["pid"], _PID_NAME[pl["pid"]]
            # A different word precedes the surname — could be another player's
            # first name. Don't guess.
            continue
        # Bare surname: only safe when no other tracked player shares it.
        if len(_SURNAME_FIRSTS.get(last, ())) == 1:
            return pl["pid"], _PID_NAME[pl["pid"]]
    return None, None


def find_players_all(text, max_n=4):
    """Like find_player, but return EVERY confidently-matched tracked player in
    the text as [{pid, name}] (up to max_n), so an article can carry tags for
    all the players it mentions. Uses the same conservative rules (full name, or
    surname with first-name verification / unique surname) to avoid
    mis-attribution."""
    t = (text or "").lower()
    if not t:
        return []
    found = {}
    for pl in _PLAYERS_IDX:
        if pl["full"] in t:
            found.setdefault(pl["pid"], _PID_NAME[pl["pid"]])
    # Blank out the full names we've already matched before surname matching, so a
    # matched player's FIRST name can't be misread as another player's surname
    # (e.g. "Brodie Grundy" must not also tag "Will Brodie", nor "Ryan Angwin"
    # tag "Luke Ryan").
    residual = t
    for pl in _PLAYERS_IDX:
        if pl["pid"] in found:
            residual = residual.replace(pl["full"], " ")
    for pl in sorted(_PLAYERS_IDX, key=lambda p: -len(p["last"])):
        if pl["pid"] in found:
            continue
        last = pl["last"]
        if len(last) < 4:
            continue
        m = re.search(r"(?:([a-z][a-z'.-]*)\s+)?(?<![a-z])" + re.escape(last) + r"(?![a-z])", residual)
        if not m:
            continue
        prev = m.group(1) or ""
        if prev:
            if prev == pl["first"] or (1 <= len(prev) <= 2 and prev[0] == pl["first"][0]):
                found.setdefault(pl["pid"], _PID_NAME[pl["pid"]])
        elif len(_SURNAME_FIRSTS.get(last, ())) == 1:
            found.setdefault(pl["pid"], _PID_NAME[pl["pid"]])
    return [{"pid": pid, "name": name} for pid, name in list(found.items())[:max_n]]

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

    # Footywire groups injuries by club. Each club is a
    #   <td class="tbtitle">Adelaide Crows (3 Players)</td>
    # heading, followed by a nested 3-column table:
    #   <td class="lbnorm">Player</td><td class="bnorm">Injury</td>
    #   <td class="bnorm">Returning</td>
    # then one <tr class="darkcolor"|"lightcolor"> per player, where the player
    # name lives in an <a href="/afl/footy/pp-...">. We walk each club heading to
    # its player table and emit ONE item per player row — never per club.
    seen = set()
    titles = soup.find_all("td", class_="tbtitle")
    for title in titles:
        club_raw = title.get_text(" ", strip=True)
        club = re.sub(r"\s*\(\s*\d+\s*Players?\s*\)\s*$", "", club_raw).strip()
        if not club:
            continue

        # Find this club's player table: the nearest table (within the club's
        # wrapper) whose header row says Player / Injury / Returning.
        wrapper = title.find_parent("table")
        player_table = None
        if wrapper:
            for tbl in wrapper.find_all("table"):
                hdr = " ".join(td.get_text(strip=True).lower()
                               for td in tbl.find_all("td", class_=re.compile(r"b?norm")))
                if "player" in hdr and "injury" in hdr:
                    player_table = tbl
                    break
        if player_table is None:
            continue

        for row in player_table.find_all("tr"):
            # Player rows carry the zebra-stripe classes; the header row doesn't.
            row_cls = " ".join(row.get("class", []))
            if "color" not in row_cls:
                continue
            cells = row.find_all("td")
            if len(cells) < 3:
                continue

            link   = cells[0].find("a")
            name   = (link.get_text(" ", strip=True) if link
                      else cells[0].get_text(" ", strip=True)).strip()
            injury = cells[1].get_text(" ", strip=True)
            eta    = cells[2].get_text(" ", strip=True)
            if not name or len(name) < 3 or name.lower() == "player":
                continue

            status, eta_disp = _classify_returning(eta)
            if status == "available":
                continue  # cleared to play — not injury-list news
            body_part = _injury_body_part(injury)
            # The Footywire <a> text is the authoritative full name. find_player
            # is only consulted for the pid (its returned name can be a partial
            # index key like a bare surname, which would truncate the display).
            pid, _pname = find_player_strict(name)
            if not pid:
                continue  # only surface players tracked in players.json (searchable)
            display_name = name

            dedupe_key = (pid or display_name.lower(), body_part or injury.lower())
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)

            cat = "injury_out" if status == "out" else "injury_tbc"
            headline = f"{display_name} — {status.upper()}: {body_part or injury}"
            if eta_disp:
                headline += f" ({eta_disp})"
            body = (f"{display_name} ({club}) injury status: {body_part or injury}. "
                    f"Expected return: {eta_disp or 'unknown'}.")

            items.append({
                "id":          None,
                "type":        "injury",
                "category":    cat,
                "urgent":      status == "out",
                "player":      display_name,
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
                "tags":        [status.upper(), body_part or injury[:30], eta_disp or ""],
                "stats":       [
                    {"l":"Status",  "v": status.upper()},
                    {"l":"Injury",  "v": body_part or injury[:20]},
                    {"l":"ETA",     "v": eta_disp or "Unknown"},
                    {"l":"Club",    "v": club},
                ],
                "relevance":   60,
                "_source":     "footywire_injuries",
            })

    log.info(f"Footywire injuries: {len(items)} player items across clubs")
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

    # Fallback: the team-block class names move around. If nothing matched,
    # scan every li/p/tr in the page and keep rows that name a known player and
    # mention a selection event.
    if not items:
        log.info("Footywire selections: no team blocks matched — scanning li/p/tr fallback")
        for row in soup.find_all(["tr", "li", "p"]):
            text = row.get_text(strip=True)
            if len(text) < 5:
                continue
            pid, pname = find_player(text, player_idx)
            if not pid:
                continue
            tl = text.lower()
            if not any(x in tl for x in ("named", "selected", "in for", "replaces",
                                         "omitted", "dropped", "out for", "replaced by",
                                         "emergenc", "vest", "sub", "recalled")):
                continue
            if "vest" in tl or "sub" in tl:
                cat = "vest_risk"
            elif any(x in tl for x in ("omitted", "dropped", "out for", "replaced by")):
                cat = "dropped"
            elif any(x in tl for x in ("forward pocket", "half forward", "role")):
                cat = "role_change"
            else:
                cat = "named"
            items.append({
                "id": None, "type": "selection", "category": cat,
                "urgent": cat in ("vest_risk", "dropped"),
                "player": pname or "", "pid": pid, "team": None, "pos": None,
                "source": "Footywire", "sourceHandle": "@footywire", "reliability": 94,
                "time": "latest", "timeLabel": "Latest",
                "headline": text[:120], "body": text,
                "signal": "sell" if cat in ("dropped", "vest_risk") else ("hold" if cat == "role_change" else None),
                "signalConf": 70, "tags": [cat.replace("_", " ").title()],
                "stats": [], "relevance": 50, "_source": "footywire_selections",
            })

    log.info(f"Footywire selections: {len(items)} relevant items")
    return items


# ── AFL.COM.AU RSS ────────────────────────────────────────────────────────────

# AFL.com.au's RSS path moves around and sometimes serves HTML instead of XML.
# Try these in order; if none parse, fall back to scraping the /news page.
AFL_RSS_CANDIDATES = [
    "https://www.afl.com.au/rss",
    "https://www.afl.com.au/news/rss",
    "https://www.afl.com.au/api/cfs/afl/WEB/FEED/NEWS",
]


def _label_from_dt(dt):
    """Human age label from an aware datetime: 'Xm ago' / 'Xh ago' / 'Xd ago',
    or '12 May' for anything older than 6 days. Never 'recent'/'latest'."""
    try:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        secs = (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds()
        mins = int(secs // 60)
        if mins < 0:
            mins = 0
        if mins < 60:
            return f"{max(1, mins)}m ago"
        hrs = mins // 60
        if hrs < 24:
            return f"{hrs}h ago"
        days = hrs // 24
        if days <= 6:
            return f"{days}d ago"
        return dt.astimezone(timezone.utc).strftime("%d %b").lstrip("0")
    except Exception:
        return "1d ago"


def _classify_headline(text):
    """Map a headline/body to (type, category) by keyword priority:
    selection -> injury -> analysis -> news/general. General news is KEPT
    (not dropped) so the feed isn't dominated by injuries."""
    t = (text or "").lower()
    if any(k in t for k in ("named", "selected", " in for ", "omitted", "dropped",
                            "emergenc", "recalled", "ins and outs", "team news",
                            "line-up", "lineup", "squad", "to debut", "late out",
                            "late change", "selection")):
        return "selection", "team_news"
    if any(k in t for k in ("injury", "injured", "ruled out", " out for", "sidelined",
                            "tbc", "hamstring", "knee", "ankle", "calf", "shoulder",
                            "concussion", "groin", "quad", "corked", "achilles",
                            "soreness", "setback", "scan", "surgery", "suspend",
                            "weeks out", "done for the")):
        return "injury", "injury_tbc"
    if any(k in t for k in ("trade", "price", "average", "fantasy", "supercoach",
                            "cash cow", "captain", "draft", "value pick", "breakeven",
                            "break-even")):
        return "analysis", "price"
    return "news", "general"


def _rss_item_to_news(item, source_name, reliability, player_idx):
    """Build a news item dict from one RSS <item> element, or None if not
    relevant. `item` is an xml.etree element."""
    title   = (item.findtext("title")       or "").strip()
    desc    = (item.findtext("description") or "").strip()
    link    = (item.findtext("link")        or "").strip()
    pub     = (item.findtext("pubDate")     or "").strip()
    content = (item.findtext("{http://purl.org/rss/1.0/modules/content/}encoded") or "").strip()

    body_text = BeautifulSoup(content or desc, "lxml").get_text(strip=True)[:500]
    full_text = title + " " + body_text

    result = classify_item(full_text, title)
    pid, pname = find_player(full_text, player_idx)

    # Type/category from the headline (per fix): selection / injury / analysis /
    # news+general. Keep injury sub-category nuance (out vs tbc) when the keyword
    # classifier has it. Secondary outlets still drop clearly non-AFL items.
    item_type, cat = _classify_headline(full_text)
    if item_type == "injury" and (result.get("category") or "").startswith("injury"):
        cat = result["category"]
    if source_name != "AFL.com.au" and not result.get("relevant") and not pid             and not any(k in full_text.lower() for k in ("afl", "football", "footy")):
        return None
    score = result["score"] if result.get("relevant") else 45

    mins = 99999
    pub_iso = None
    try:
        from email.utils import parsedate_to_datetime
        pub_dt = parsedate_to_datetime(pub)
        if pub_dt.tzinfo is None:
            pub_dt = pub_dt.replace(tzinfo=timezone.utc)
        pub_iso = pub_dt.isoformat()
        mins = int((datetime.now(timezone.utc) - pub_dt).total_seconds() / 60)
        time_label = _label_from_dt(pub_dt)
    except Exception:
        time_label = ""

    signal = None
    if cat == "injury_out":    signal = "sell"
    elif cat == "injury_tbc":  signal = "hold"
    elif cat == "dropped":     signal = "sell"
    elif cat == "role_change": signal = "hold"

    return {
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
        "relevance":   score,
        "_source":     f"rss_{source_name.lower().replace(' ','')}",
        "pubISO":      pub_iso,
    }


def _parse_rss_feed(session, feed_url, source_name, reliability, player_idx):
    """Fetch and parse one RSS feed URL into news items. Returns [] on any
    failure (network, non-XML body, parse error)."""
    import xml.etree.ElementTree as ET
    out = []
    r = fetch(session, feed_url)
    if not r:
        return out
    try:
        root = ET.fromstring(r.text)
    except ET.ParseError as e:
        log.warning(f"RSS parse error {feed_url}: {e}")
        return out
    feed_items = root.findall(".//item")
    log.info(f"  {source_name}: {len(feed_items)} raw items ({feed_url})")
    for item in feed_items:
        it = _rss_item_to_news(item, source_name, reliability, player_idx)
        if it:
            out.append(it)
    return out


def _scrape_afl_news_html(session, player_idx):
    """Fallback when AFL.com.au RSS is unavailable: scrape the /news page and
    pull article headlines from anchor links to /news/<id>/<slug>."""
    items = []
    r = fetch(session, AFL_NEWS_LIST_URL)
    if not r:
        return items
    soup = BeautifulSoup(r.text, "lxml")
    seen = set()
    for a in soup.find_all("a", href=re.compile(r"/news/\d+/")):
        title = a.get_text(" ", strip=True)
        if not title or len(title) < 20 or title.lower() in seen:
            continue
        seen.add(title.lower())
        result = classify_item(title)
        pid, pname = find_player(title, player_idx)
        href = a.get("href", "")
        link = href if href.startswith("http") else f"https://www.afl.com.au{href}"
        item_type, cat = _classify_headline(title)
        if item_type == "injury" and (result.get("category") or "").startswith("injury"):
            cat = result["category"]
        items.append({
            "id": None, "type": item_type, "category": cat,
            "urgent": False, "player": pname or "", "pid": pid,
            "team": None, "pos": None, "source": "AFL.com.au",
            "sourceHandle": "@aflcomau", "reliability": 96,
            "time": "recent", "timeLabel": "recent",
            "headline": title[:150], "body": title[:400], "link": link,
            "signal": "sell" if cat == "injury_out" else "hold" if cat == "injury_tbc" else None,
            "signalConf": 80, "tags": [cat.replace("_"," ").title()],
            "stats": [], "relevance": result["score"], "_source": "afl_news_html",
        })
        if len(items) >= 20:
            break
    log.info(f"  AFL.com.au /news HTML fallback: {len(items)} items")
    return items


def scrape_afl_rss(session, player_idx):
    """
    Parse AFL.com.au + independent-outlet RSS feeds. AFL.com.au's feed URL is
    probed across several candidates; if all fail we fall back to scraping the
    /news HTML page so this source is never silently empty.
    """
    items = []

    # AFL.com.au — probe candidate feed URLs, then HTML fallback.
    afl_items = []
    for url in AFL_RSS_CANDIDATES:
        afl_items = _parse_rss_feed(session, url, "AFL.com.au", 96, player_idx)
        if afl_items:
            log.info(f"AFL.com.au RSS: using {url} ({len(afl_items)} items)")
            break
    if not afl_items:
        log.info("AFL.com.au RSS: no working feed — falling back to /news HTML")
        afl_items = _scrape_afl_news_html(session, player_idx)
    items += afl_items

    # Independent outlets (kept as-is; their RSS is generally stable).
    for feed_url, source_name, reliability in AFL_RSS_FEEDS:
        items += _parse_rss_feed(session, feed_url, source_name, reliability, player_idx)

    log.info(f"RSS total: {len(items)} relevant items")
    return items


def scrape_google_news(session, player_idx):
    """Pull AFL injury/selection/fantasy news and trade rumours from Google News
    RSS. This aggregates many publishers and is reachable when Nitter and
    AFL.com.au are blocked, so it's the main defence against a Footywire-only
    feed (and the only working source for the rumour mill)."""
    from urllib.parse import quote
    items = []
    for query, reliability, is_rumour in GOOGLE_NEWS_QUERIES:
        url = GOOGLE_NEWS_RSS.format(q=quote(query))
        r = fetch(session, url)
        if not r:
            continue
        try:
            soup = BeautifulSoup(r.text, "lxml-xml")
        except Exception:
            soup = BeautifulSoup(r.text, "xml")

        kept = 0
        for entry in soup.find_all("item")[:25]:
            title_el = entry.find("title")
            raw_title = title_el.get_text(strip=True) if title_el else ""
            if not raw_title:
                continue
            # Google News titles end with " - Publisher"; split it off.
            src_el = entry.find("source")
            publisher = src_el.get_text(strip=True) if src_el else ""
            headline = raw_title
            if publisher and headline.endswith(" - " + publisher):
                headline = headline[: -(len(publisher) + 3)].strip()
            elif " - " in headline:
                headline, _, pub_tail = headline.rpartition(" - ")
                headline = headline.strip()
                if not publisher:
                    publisher = pub_tail.strip()
            if not publisher:
                publisher = "Google News"

            desc_el = entry.find("description")
            desc = ""
            if desc_el and desc_el.get_text(strip=True):
                desc = BeautifulSoup(desc_el.get_text(), "lxml").get_text(" ", strip=True)
            full_text = headline + " " + desc

            result = classify_item(full_text, headline)
            if is_rumour:
                _ft = full_text.lower()
                if not find_player(full_text, player_idx)[0]:
                    continue
                if not any(w in _ft for w in ("train", "fitness", "return", "role", "midfield", "forward", "ruck", "defend", "injur", "doubt", " test", "manage", "position", "supercoach", "fantasy", "cleared", "recall", "omit", "drop", "named", "select", "concuss")):
                    continue
            elif not result["relevant"]:
                continue

            age_min = 99999
            time_label = ""
            pub_iso = None
            pd_el = entry.find("pubDate") or entry.find("pubdate")
            if pd_el and pd_el.get_text(strip=True):
                try:
                    dt = parsedate_to_datetime(pd_el.get_text(strip=True))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    pub_iso = dt.isoformat()
                    age_min = int((datetime.now(timezone.utc) - dt).total_seconds() / 60)
                    time_label = _label_from_dt(dt)
                except Exception:
                    pass

            pid, pname = find_player(full_text, player_idx)
            link_el = entry.find("link")
            link = link_el.get_text(strip=True) if link_el else ""
            cat = result["category"]
            if is_rumour:
                item_type = "rumour"
            else:
                item_type = (
                    "injury"    if "injury" in cat else
                    "selection" if cat in ("named", "dropped", "role_change", "vest_risk", "team_news") else
                    "price"     if cat == "price" else
                    "news"
                )
            signal = None
            if cat == "injury_out":    signal = "sell"
            elif cat == "injury_tbc":  signal = "hold"
            elif cat == "dropped":     signal = "sell"
            elif cat == "role_change": signal = "hold"

            _clean = re.sub(r"\s+", " ", desc or "").strip()
            # Real article snippet only. Skip clickbait aggregators and any item
            # whose description just echoes the headline / player name or is too
            # short — never publish a generic template.
            if publisher.lower() in ("news.com.au", "msn", "msn.com", "yahoo", "yahoo sport"):
                continue
            if (not _clean or len(_clean) < 30
                    or headline.lower()[:35] in _clean.lower()
                    or _clean.lower() == (pname or "").lower()):
                continue
            body = _clean[:200]
            items.append({
                "id":           None,
                "type":         item_type,
                "category":     cat,
                "urgent":       (not is_rumour) and cat in ("injury_out", "dropped") and age_min < 120,
                "player":       pname or "",
                "pid":          pid,
                "team":         None,
                "pos":          None,
                "source":       publisher,
                "sourceHandle": f"@{publisher.lower().replace(' ', '')}",
                "reliability":  reliability,
                "time":         time_label,
                "timeLabel":    time_label,
                "headline":     headline[:150],
                "body":         body[:400],
                "link":         link,
                "signal":       signal,
                "signalConf":   max(40, reliability - 10),
                "tags":         [cat.replace("_", " ").title()] + (["Rumour"] if is_rumour else []),
                "stats":        [],
                "is_rumour":    is_rumour,
                "relevance":    result["score"],
                "_source":      "google_news",
                "pubISO":       pub_iso,
            })
            kept += 1

        log.info(f"Google News: '{query}' -> {kept} items")
        time.sleep(0.6)

    # Dedupe by (player, headline).
    seen, out = set(), []
    for it in items:
        key = (it.get("player", "").lower(), (it.get("headline") or "")[:80].lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    log.info(f"Google News: {len(out)} items kept (deduped)")
    return out


# ── TWITTER/X VIA NITTER ─────────────────────────────────────────────────────

# RSS-based config (replaces the old HTML-scraping path). Nitter exposes an RSS
# feed per account at {instance}/{handle}/rss — far more reliable and faster to
# parse than the HTML timeline.
NITTER_RSS_INSTANCES = [
    "https://nitter.poast.org",
    "https://xcancel.com",
    "https://nitter.privacyredirect.com",
    "https://nitter.tiekoetter.com",
]

# AFL-fantasy-focused accounts only. (handle, display name, reliability, official)
# Official accounts produce news items when factual; everyone else (and any
# speculation) feeds the rumour mill.
TWITTER_RSS_ACCOUNTS = [
    ("dttalk",         "DT Talk",            82, False),
    ("supercoach_dr",  "SuperCoach Doctor",  80, False),
    ("scscoop",        "Supercoach Scoop",   78, False),
    ("warnie",         "Warnie",             80, False),
    ("aflcomau",       "AFL.com.au",         96, True),
    ("champdata",      "Champion Data",      98, True),
    ("heraldsunfooty", "Herald Sun Sport",   85, True),
]

# Words that mark a post as unconfirmed speculation -> rumour mill.
SPECULATION_WORDS = (
    "looks like", "could", "might", "hearing", "whisper", "whispers", "expect",
)

# A rumour older than 6h is stale. Official news gets a slightly longer window.
RUMOUR_MAX_AGE_HOURS = 6
NEWS_MAX_AGE_HOURS   = 24
# The rumour buffer is a rolling "current whispers" board — drop anything first
# seen more than this many days ago so week-old rumours don't sit forever when
# fresh ones are scarce.
RUMOUR_BUFFER_MAX_DAYS = 4


def _tweet_age_label(age_hours):
    """Format an age (in hours) as a compact relative label, like Nitter does."""
    if age_hours is None: return "recent"
    if age_hours < 1:     return f"{int(age_hours * 60)}m ago"
    if age_hours < 24:    return f"{int(age_hours)}h ago"
    return f"{int(age_hours // 24)}d ago"


def _find_nitter_rss_base(session):
    """Probe Nitter RSS instances in order; return the first that serves valid
    RSS (a 200 whose body contains an <item> element)."""
    for base in NITTER_RSS_INSTANCES:
        try:
            r = session.get(f"{base}/aflcomau/rss", timeout=8)
            if r.status_code == 200 and "<item>" in r.text.lower():
                log.info(f"Twitter RSS: using {base}")
                return base
        except Exception:
            continue
    return None


def scrape_twitter_rss(session, player_idx):
    """
    Pull AFL fantasy chatter from Nitter RSS feeds — more reliable and faster
    than HTML scraping. For each target account we fetch {instance}/{handle}/rss
    and parse each <item> (<title>, <description>, <pubDate>).

    Classification (per spec):
      - Unconfirmed speculation, OR anything from a non-official account ->
        rumour mill: type="rumour", is_rumour=True.
      - Confirmed facts from an official account -> normal news items.
    Rumours older than RUMOUR_MAX_AGE_HOURS (6h) are dropped as stale; official
    news is kept up to NEWS_MAX_AGE_HOURS (24h). pubDate (RFC-822) drives both
    the recency gate and the "5m ago" / "2h ago" timeLabel.
    """
    from email.utils import parsedate_to_datetime

    items = []
    now   = datetime.now(timezone.utc)

    base = _find_nitter_rss_base(session)
    if not base:
        log.warning("Twitter RSS: no working Nitter RSS instance found — skipping")
        return items

    for handle, source_name, reliability, is_official in TWITTER_RSS_ACCOUNTS:
        r = fetch(session, f"{base}/{handle}/rss")
        if not r:
            # Per-account fallback across the other instances.
            for alt in NITTER_RSS_INSTANCES:
                if alt == base:
                    continue
                r = fetch(session, f"{alt}/{handle}/rss")
                if r:
                    break
        if not r:
            log.debug(f"Twitter RSS: {handle} unavailable on all instances")
            continue

        try:
            soup = BeautifulSoup(r.text, "lxml-xml")
        except Exception:
            soup = BeautifulSoup(r.text, "xml")

        kept_here = 0
        for entry in soup.find_all("item")[:25]:
            title_el = entry.find("title")
            title = title_el.get_text(strip=True) if title_el else ""
            desc_el = entry.find("description")
            desc = ""
            if desc_el and desc_el.get_text(strip=True):
                # <description> holds escaped tweet HTML — unwrap to plain text.
                desc = BeautifulSoup(desc_el.get_text(), "lxml").get_text(" ", strip=True)
            text = (title or desc).strip()
            if len(text) < 20:
                continue
            # Skip retweets — Nitter RSS prefixes these "RT by @handle:".
            if text.startswith("RT by ") or text.startswith("RT @"):
                continue

            # pubDate (RFC-822) -> age in hours.
            age = None
            pub_iso = None
            pd_el = entry.find("pubDate") or entry.find("pubdate")
            if pd_el and pd_el.get_text(strip=True):
                try:
                    dt = parsedate_to_datetime(pd_el.get_text(strip=True))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    pub_iso = dt.isoformat()
                    age = (now - dt.astimezone(timezone.utc)).total_seconds() / 3600
                except Exception:
                    age = None

            result = classify_item(text)
            if not result["relevant"]:
                continue

            lt = text.lower()
            is_speculation = any(w in lt for w in SPECULATION_WORDS)
            is_rumour = is_speculation or not is_official

            # Recency gates.
            if age is not None:
                if is_rumour and age > RUMOUR_MAX_AGE_HOURS:
                    continue
                if not is_rumour and age > NEWS_MAX_AGE_HOURS:
                    continue

            pid, pname = find_player(text, player_idx)
            cat = result["category"]
            if is_rumour:
                item_type = "rumour"
            else:
                item_type = (
                    "injury"    if "injury" in cat else
                    "selection" if cat in ("named", "dropped", "role_change", "vest_risk", "team_news") else
                    "price"     if cat == "price" else
                    "news"
                )

            signal = None
            if cat == "injury_out":    signal = "sell"
            elif cat == "injury_tbc":  signal = "hold"
            elif cat == "dropped":     signal = "sell"
            elif cat == "role_change": signal = "hold"

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
                "sourceHandle":f"@{handle}",
                "reliability": reliability,
                "time":        _tweet_age_label(age),
                "timeLabel":   _tweet_age_label(age),
                "age_hours":   age,
                "headline":    text[:140],
                "body":        text[:400],
                "signal":      signal,
                "signalConf":  reliability - 10,
                "tags":        [cat.replace("_"," ").title(), "Twitter"] + (["Rumour"] if is_rumour else []),
                "stats":       [],
                "is_rumour":   is_rumour,
                "relevance":   result["score"],
                "_source":     f"twitter_{handle}",
                "pubISO":      pub_iso,
            })
            kept_here += 1

        log.info(f"Twitter RSS: @{handle} -> {kept_here} items")
        time.sleep(0.5)

    # Dedupe by (player, headline) within this batch.
    seen, deduped = set(), []
    for it in items:
        key = (it.get("player","").lower(), (it.get("headline") or "")[:80].lower())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(it)

    rumours = sum(1 for it in deduped if it.get("is_rumour"))
    log.info(f"Twitter RSS results: {len(deduped)} items from {len(TWITTER_RSS_ACCOUNTS)} accounts ({rumours} rumours)")
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

    # AFL.com.au (PULSE platform) renders the injury list as one plain <table>
    # per club, each with header columns: PLAYER | INJURY | ESTIMATED RETURN.
    # Older markup (class="club"/"player"...) no longer exists, so parse tables.
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        header = [c.get_text(strip=True).lower() for c in rows[0].find_all(["th", "td"])]
        if not (any("player" in h for h in header) and any("injury" in h for h in header)):
            continue

        for row in rows[1:]:
            cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
            if len(cells) < 2 or not cells[0]:
                continue
            name_raw = cells[0]
            injury   = cells[1] if len(cells) > 1 else ""
            eta      = cells[2] if len(cells) > 2 else ""

            pid, pname = find_player_strict(name_raw)
            if not pname:
                continue  # only surface players we track (rank-relevant)

            el = (eta + " " + injury).lower()
            if (re.search(r"\d+\s*-?\s*\d*\s*week", el)
                    or any(x in el for x in ("season", "indefinite", "year", "out for", "month"))):
                cat, signal, status = "injury_out", "sell", "OUT"
            else:
                # "Test", "TBC", "1 week", managed, etc. — uncertain availability.
                cat, signal, status = "injury_tbc", "hold", "TBC"

            headline = f"{pname} — {status}: {injury}" + (f" ({eta})" if eta else "")

            items.append({
                "id":          None,
                "type":        "injury",
                "category":    cat,
                "urgent":      cat == "injury_out",
                "player":      pname,
                "pid":         pid,
                "team":        None,
                "pos":         None,
                "source":      "AFL.com.au",
                "sourceHandle":"@aflcomau",
                "reliability": 96,
                "time":        "latest",
                "timeLabel":   "Latest",
                "headline":    headline,
                "body":        (f"{pname} has been ruled OUT" + (f" with a {injury.lower()}" if injury else "") + (f" Estimated return: {eta}." if eta and eta.lower() not in ("", "tbc") else ".") if cat == "injury_out" else f"{pname} is in doubt" + (f" with a {injury.lower()}" if injury else "") + (f" Estimated return: {eta}." if eta and eta.lower() not in ("", "tbc") else ".")),
                "signal":      signal,
                "signalConf":  88,
                "tags":        [cat.replace("_", " ").title(), injury[:25], eta or ""],
                "stats":       [
                    {"l": "Status", "v": status},
                    {"l": "Injury", "v": injury[:20]},
                    {"l": "ETA",    "v": eta or "Unknown"},
                ],
                "relevance":   100,  # official AFL source — top priority
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
            # The Medical Room table cell is the authoritative full name; use
            # find_player only for the pid (its name can be a partial index key).
            pid, _pname = find_player_strict(name)
            if not pid:
                continue  # only surface players tracked in players.json (searchable)

            dedupe_key = (pid or name.lower(), body_part or injury.lower())
            if dedupe_key in seen: continue
            seen.add(dedupe_key)

            cat = "injury_out" if status == "out" else "injury_tbc" if status == "test" else "injury_available"
            display_name = name
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
                "body":        (f"{display_name} ({club_name}) has been ruled OUT" + (f" with a {(body_part or injury).lower()}" if (body_part or injury) else "") + (f" Estimated return: {eta_disp}." if eta_disp and eta_disp.lower() not in ("", "tbc") else ".") if status == "out" else f"{display_name} ({club_name}) faces a fitness test" + (f" on a {(body_part or injury).lower()}" if (body_part or injury) else "") + "." if status == "test" else f"{display_name} ({club_name}) is managing" + (f" a {(body_part or injury).lower()}" if (body_part or injury) else " a minor issue") + (f" Estimated return: {eta_disp}." if eta_disp and eta_disp.lower() not in ("", "tbc") else ".")),
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


# ── DEDICATED TEAM-ANNOUNCEMENT SCRAPER (Wed/Thu/Fri lineups) ──────────────────

AFL_TEAM_NEWS_PAGE = "https://www.afl.com.au/news/teams"


def _selection_window_active():
    """True during the Wed/Thu/Fri ~6pm-midnight AEST team-announcement window."""
    aest = datetime.now(timezone.utc) + timedelta(hours=10)
    return aest.weekday() in (2, 3, 4) and 18 <= aest.hour < 24


def _guess_round():
    """Best-effort current round number from players.json roundStats keys."""
    try:
        data = json.loads((BASE_DIR / "players.json").read_text(encoding="utf-8"))
        players = data.get("players", []) if isinstance(data, dict) else data
        rounds = set()
        for p in players[:80]:
            for k in (p.get("roundStats") or {}):
                m = re.match(r"[Rr]?(\d+)", str(k))
                if m:
                    rounds.add(int(m.group(1)))
        if rounds:
            return max(rounds)
    except Exception:
        pass
    return None


def _parse_afl_team_page(session, player_idx):
    """Best-effort parse of the AFL team-selection page into
    {club: {"named": [names], "emergencies": [names]}}. The page is often
    JS-rendered, so this returns {} when the lineups aren't in the static HTML."""
    teams = {}
    r = fetch(session, AFL_TEAMS_PAGE)
    if not r:
        return teams
    soup = BeautifulSoup(r.text, "lxml")
    cards = (soup.find_all(["section", "div"],
                           class_=re.compile("team-selection|team-?announcement|match-?selection", re.I))
             or soup.find_all(["section", "div"], class_=re.compile("club|team", re.I)))
    for card in cards:
        club_el = card.find(["h2", "h3", "h4", "span"], class_=re.compile("club|team|name", re.I))
        club = club_el.get_text(strip=True) if club_el else ""
        if not club or len(club) > 40:
            continue
        named, emerg = [], []
        for slot in card.find_all(["li", "div", "span", "a"]):
            text = slot.get_text(" ", strip=True)
            if len(text) < 3 or len(text) > 60:
                continue
            _pid, pname = find_player(text, player_idx)
            if not pname:
                continue
            blob = (text + " " + " ".join(slot.get("class") or [])).lower()
            if "emergenc" in blob:
                if pname not in emerg:
                    emerg.append(pname)
            elif pname not in named:
                named.append(pname)
        if named or emerg:
            book = teams.setdefault(club, {"named": [], "emergencies": []})
            for n in named:
                if n not in book["named"]:
                    book["named"].append(n)
            for e in emerg:
                if e not in book["emergencies"]:
                    book["emergencies"].append(e)
    return teams


def _sel_item(club, category, headline, body, signal, relevance, player=None, player_idx=None):
    """Build a selection news item, resolving the player's pid where possible."""
    pid, pname = None, (player or "")
    if player and player_idx:
        pid, found = find_player(player, player_idx)
        pname = found or player
    return {
        "id": None, "type": "selection", "category": category,
        "urgent": category in ("dropped", "vest_risk"),
        "player": pname, "pid": pid, "team": club, "pos": None,
        "source": "AFL.com.au", "sourceHandle": "@aflcomau", "reliability": 95,
        "time": "latest", "timeLabel": "Latest",
        "headline": headline[:150], "body": body[:400],
        "signal": signal, "signalConf": 80,
        "tags": [category.replace("_", " ").title(), club],
        "stats": [], "relevance": relevance, "_source": "team_announcements",
    }


def scrape_team_announcements(session, player_idx):
    """Capture Wed/Thu/Fri team announcements: build each club's named 22 +
    emergencies from the AFL team-selection page (primary), Footywire ins/outs
    (secondary) and the AFL teams-news page (tertiary), diff against last week's
    lineup stored in news_history.json, and emit a selection item per change
    (IN / OUT / emergency) plus a one-off full-team summary when a club's team is
    announced for the first time this round."""
    items = []
    if _selection_window_active():
        log.info("Selection window active — checking team news")

    round_num = _guess_round()
    round_lbl = f"Round {round_num}" if round_num else "this round"

    # PRIMARY — AFL.com.au team-selection page
    teams = _parse_afl_team_page(session, player_idx)

    # SECONDARY — Footywire ins/outs folded in (ins -> named, vest -> emergency)
    try:
        for it in scrape_fw_selections(session, player_idx):
            club, pl, cat = it.get("team") or "", it.get("player") or "", it.get("category")
            if not club or not pl:
                continue
            book = teams.setdefault(club, {"named": [], "emergencies": []})
            if cat in ("named",) and pl not in book["named"]:
                book["named"].append(pl)
            elif cat == "vest_risk" and pl not in book["emergencies"]:
                book["emergencies"].append(pl)
    except Exception as e:
        log.warning(f"Footywire fold-in failed: {e}")

    # TERTIARY — AFL teams-news page: surface "TEAMS:" headline articles
    try:
        r = fetch(session, AFL_TEAM_NEWS_PAGE)
        if r:
            soup = BeautifulSoup(r.text, "lxml")
            seen = set()
            for a in soup.find_all("a", href=True):
                t = a.get_text(" ", strip=True)
                if "teams:" in t.lower() and 12 < len(t) < 140 and t.lower() not in seen:
                    seen.add(t.lower())
                    href = a["href"]
                    link = href if href.startswith("http") else f"https://www.afl.com.au{href}"
                    items.append({
                        "id": None, "type": "selection", "category": "team_news",
                        "urgent": False, "player": "", "pid": None, "team": "",
                        "pos": None, "source": "AFL.com.au", "sourceHandle": "@aflcomau",
                        "reliability": 95, "time": "latest", "timeLabel": "Latest",
                        "headline": t[:150], "body": t[:400], "link": link,
                        "signal": None, "signalConf": 80, "tags": ["Team News"],
                        "stats": [], "relevance": 60, "_source": "team_announcements",
                    })
                    if len(items) >= 10:
                        break
    except Exception:
        pass

    # CHANGE DETECTION against last week's lineup in news_history.json
    try:
        hist = json.loads(HISTORY_PATH.read_text(encoding="utf-8")) if HISTORY_PATH.exists() else {}
    except Exception:
        hist = {}
    snap = hist.get("team_announcements") or {}
    now_iso = datetime.now(timezone.utc).isoformat()
    num_clubs = 0

    for club, cur in teams.items():
        cur_named = cur.get("named", [])
        cur_emerg = cur.get("emergencies", [])
        if not cur_named and not cur_emerg:
            continue
        num_clubs += 1
        prev = snap.get(club) or {}
        prev_named = set(prev.get("named", []))
        prev_emerg = set(prev.get("emergencies", []))

        ins        = [p for p in cur_named if p not in prev_named]
        outs       = [p for p in prev_named if p not in set(cur_named)]
        new_emergs = [p for p in cur_emerg if p not in prev_emerg]

        # First sighting of this club's team for this round -> full-team summary
        if (prev.get("round") != round_num or not prev_named) and cur_named:
            items.append(_sel_item(
                club, "team_news",
                f"{club} name team for {round_lbl}: {len(ins)} in, {len(outs)} out",
                f"{club} have named their {round_lbl} lineup. "
                f"In: {', '.join(ins) or 'no changes'}. Out: {', '.join(outs) or 'none'}. "
                f"Emergencies: {', '.join(cur_emerg) or 'none'}.",
                None, 80))

        for p in ins:
            items.append(_sel_item(club, "named",
                f"{p} IN: Returns for {club} in {round_lbl}",
                f"{p} has been named in {club}'s {round_lbl} lineup after missing last week.",
                None, 78, player=p, player_idx=player_idx))
        for p in outs:
            items.append(_sel_item(club, "dropped",
                f"{p} OUT: Omitted by {club} for {round_lbl}",
                f"{p} has been dropped from {club}'s {round_lbl} lineup.",
                "sell", 78, player=p, player_idx=player_idx))
        for p in new_emergs:
            items.append(_sel_item(club, "vest_risk",
                f"{p} named as emergency for {club}",
                f"{p} is listed as emergency for {club} in {round_lbl} — sub vest risk.",
                "hold", 70, player=p, player_idx=player_idx))

        snap[club] = {"round": round_num, "named": cur_named,
                      "emergencies": cur_emerg, "updated": now_iso}

    # Persist the snapshot (NewsHistory preserves this top-level key on save())
    try:
        hist["team_announcements"] = snap
        HISTORY_PATH.write_text(json.dumps(hist, indent=2))
    except Exception as e:
        log.warning(f"Could not persist team snapshot: {e}")

    log.info(f"Team selections: {len(items)} changes found across {num_clubs} clubs")
    return items


TEAM_ALIASES = {
    "WALYALUP": "Fremantle", "EURO-YROKE": "Sydney Swans", "YARTAPUULTI": "Port Adelaide",
    "WAALITJ MARAWAR": "West Coast", "NARRM": "Melbourne", "GREATER WESTERN SYDNEY": "GWS Giants",
    "KUWARNA": "Adelaide", "NGANGER": "Geelong", "BIGUBADHA": "Brisbane",
}
_TEAM_INJURY_REASONS = ["hamstring", "knee", "calf", "shoulder", "concussion", "ankle", "foot",
                        "hip", "groin", "head", "back", "quad", "illness", "managed", "soreness",
                        "corked", "achilles", "wrist", "finger", " rib", "suspended", "suspension"]


def _team_alias(caps_name):
    n = (caps_name or "").strip()
    return TEAM_ALIASES.get(n.upper(), n.title())


def parse_teams_article(text, url, round_num="", pub_iso=None, player_idx=None):
    """Parse an AFL.com.au team-announcement article into one item per player.

    Articles list each club as an ALL-CAPS line followed by 'In:' / 'Out:' lines,
    e.g.:
        RICHMOND
        In:
        M.Lefau, J.Alger
        Out:
        C.Gray (hamstring), L.Fawcett (omitted)
    Out players carry a bracketed reason; injury reasons -> type=injury, else a
    plain omission -> selection/dropped."""
    rl = f"Round {round_num}" if round_num else "this round"
    clubs, order, current, pending = {}, [], None, None
    for raw in (text or "").split("\n"):
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        if (line == line.upper() and re.match(r"^[A-Z][A-Z'’\-\. ]{2,30}$", line)
                and not low.startswith("in:") and not low.startswith("out:")):
            current = _team_alias(line)
            if current not in clubs:
                clubs[current] = {"in": "", "out": ""}
                order.append(current)
            pending = None
            continue
        if not current:
            continue
        if low.startswith("in:"):
            rest = line.split(":", 1)[1].strip()
            if rest: clubs[current]["in"] = rest
            else:    pending = "in"
            continue
        if low.startswith("out:"):
            rest = line.split(":", 1)[1].strip()
            if rest: clubs[current]["out"] = rest
            else:    pending = "out"
            continue
        if pending == "in":  clubs[current]["in"] = line;  pending = None; continue
        if pending == "out": clubs[current]["out"] = line; pending = None; continue

    def _mk(category, player, club, headline, body, signal, urgent, tags):
        pid = None
        if player_idx:
            pid, found = find_player(player, player_idx)
            player = found or player
        return {
            "id": None, "type": "injury" if category == "injury_out" else "selection",
            "category": category, "urgent": urgent, "player": player, "pid": pid,
            "team": club, "pos": None, "source": "AFL.com.au", "sourceHandle": "@aflcomau",
            "reliability": 96, "time": "latest", "timeLabel": "Latest", "pubISO": pub_iso,
            "headline": headline[:150], "body": body[:400], "link": url,
            "signal": signal, "signalConf": 85, "tags": tags, "stats": [],
            "relevance": 80, "_source": "team_announcements",
        }

    items = []
    for club in order:
        io = clubs[club]
        for player in [p.strip() for p in io["in"].split(",") if p.strip() and p.strip().lower() != "nil"]:
            items.append(_mk("named", player, club,
                             f"{player} IN: Named for {club}",
                             f"{player} has been named in {club}'s {rl} lineup. Coming in this week.",
                             None, False, ["Named", "IN", club]))
        for chunk in io["out"].split(","):
            chunk = chunk.strip()
            if not chunk or chunk.lower() == "nil":
                continue
            m = re.match(r"(.+?)\s*\(([^)]+)\)", chunk)
            if m:
                player, reason = m.group(1).strip(), m.group(2).strip()
            else:
                player, reason = chunk, "omitted"
            if not player:
                continue
            is_inj = any(r in reason.lower() for r in _TEAM_INJURY_REASONS)
            items.append(_mk(
                "injury_out" if is_inj else "dropped", player, club,
                f"{player} OUT: {club} — {reason.title()}",
                f"{player} has been ruled OUT for {club} in {rl}. Reason: {reason}.",
                "sell" if is_inj else None, is_inj,
                ["OUT", reason.title(), club]))
    return items


def scrape_team_news_articles(session, player_idx):
    """Pull AFL.com.au 'TEAMS:' announcement articles from the main RSS feed,
    fetch each article body, and emit one item per player IN/OUT/injury plus a
    summary item for the article. Articles already handled are tracked in
    news_history.json['processed_articles'] so we never reprocess them."""
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime
    items = []
    r = fetch(session, "https://www.afl.com.au/rss")
    if not r:
        log.info("Team news articles: AFL RSS unavailable")
        return items
    try:
        root = ET.fromstring(r.text)
    except Exception as e:
        log.warning(f"Team news RSS parse error: {e}")
        return items

    try:
        hist = json.loads(HISTORY_PATH.read_text(encoding="utf-8")) if HISTORY_PATH.exists() else {}
    except Exception:
        hist = {}
    processed = list(hist.get("processed_articles", []))
    processed_set = set(processed)

    round_num = _guess_round()
    round_lbl = f"Round {round_num}" if round_num else "this round"
    n_articles = 0

    for el in root.findall(".//item"):
        title = (el.findtext("title") or "").strip()
        link  = (el.findtext("link") or "").strip()
        tl = title.lower()
        is_team = ("teams-" in link.lower()) or ("teams:" in tl) or ("make" in tl and "change" in tl)
        if not is_team or not link or link in processed_set:
            continue
        n_articles += 1
        processed_set.add(link)
        processed.append(link)

        # publish time from the RSS <pubDate>
        pub_iso = None
        try:
            pdt = parsedate_to_datetime(el.findtext("pubDate") or "")
            if pdt.tzinfo is None:
                pdt = pdt.replace(tzinfo=timezone.utc)
            pub_iso = pdt.isoformat()
        except Exception:
            pub_iso = None

        # Article body: the page is often JS-rendered, so combine whatever <p>
        # text we can scrape with the RSS <description> summary.
        desc = BeautifulSoup(el.findtext("description") or "", "lxml").get_text(" ", strip=True)
        body_text = desc
        ar = fetch(session, link)
        if ar:
            paras = " ".join(p.get_text(" ", strip=True) for p in BeautifulSoup(ar.text, "lxml").find_all("p"))
            if len(paras) > len(body_text):
                body_text = paras
        body_text = body_text[:4000]

        # Summary item for the whole article
        items.append({
            "id": None, "type": "selection", "category": "team_news",
            "urgent": False, "player": "", "pid": None, "team": "", "pos": None,
            "source": "AFL.com.au", "sourceHandle": "@aflcomau", "reliability": 96,
            "time": "latest", "timeLabel": "Latest", "pubISO": pub_iso,
            "headline": title[:150], "body": (body_text[:200] or title)[:400], "link": link,
            "signal": None, "signalConf": 85, "tags": ["Team News"],
            "stats": [], "relevance": 72, "_source": "team_announcements",
        })

        # Structured "In:/Out:" club blocks (preferred). Build from newline-
        # joined article text so club names land on their own lines.
        struct_text = ""
        if ar:
            struct_text = BeautifulSoup(ar.text, "lxml").get_text("\n", strip=True)
        block_items = parse_teams_article(struct_text, link, round_num=round_num or "",
                                          pub_iso=pub_iso, player_idx=player_idx)
        items += block_items

        # Per-player ins/outs from the article sentences (fallback when the
        # article has no structured In:/Out: blocks)
        seen_players = set()
        for sent in ([] if block_items else re.split(r"(?<=[.!?])\s+", body_text)):
            sl = sent.lower()
            pid, pname = find_player(sent, player_idx)
            if not pname or pname in seen_players:
                continue
            if any(x in sl for x in ("ruled out", " is out", " out for", "won't play", "will miss", "sidelined", "omitted", "dropped")):
                cat, sig, urg, verb = "dropped", "sell", True, "OUT"
            elif "injured" in sl or "injury" in sl:
                cat, sig, urg, verb = "injury_out", "sell", True, "Injured"
            elif any(x in sl for x in (" returns", " is in ", " named", " recalled", " comes in", " back in", " selected")):
                cat, sig, urg, verb = "named", None, False, "IN"
            elif "emergenc" in sl:
                cat, sig, urg, verb = "vest_risk", "hold", False, "EMG"
            else:
                continue
            seen_players.add(pname)
            items.append({
                "id": None, "type": "selection" if cat != "injury_out" else "injury",
                "category": cat, "urgent": urg, "player": pname, "pid": pid,
                "team": "", "pos": None, "source": "AFL.com.au",
                "sourceHandle": "@aflcomau", "reliability": 96,
                "time": "latest", "timeLabel": "Latest", "pubISO": pub_iso,
                "headline": f"{pname} — {verb}: {round_lbl}"[:150],
                "body": sent[:400], "link": link,
                "signal": sig, "signalConf": 85, "tags": [verb],
                "stats": [], "relevance": 78, "_source": "team_announcements",
            })

    # Persist processed-article URLs (keep the most recent 300)
    try:
        hist["processed_articles"] = processed[-300:]
        HISTORY_PATH.write_text(json.dumps(hist, indent=2))
    except Exception as e:
        log.warning(f"Could not persist processed_articles: {e}")

    log.info(f"Team news articles: {len(items)} items from {n_articles} new articles")
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
                        "selection" if cat in ("named","dropped","role_change","vest_risk","team_news") else
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
            item_type = "injury" if "injury" in cat else "selection" if cat in ("named","dropped","role_change","vest_risk","team_news") else "news"
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


# ── BODY TEXT CLEANUP ─────────────────────────────────────────────────────────

def _tidy_body(text, fallback="", max_len=250):
    """Ensure an item has a meaningful body sentence. Falls back to the
    headline when empty, and truncates cleanly at the last full stop before
    max_len (or last word + ellipsis if no sentence break is found)."""
    text = (text or "").strip()
    if not text:
        text = (fallback or "").strip()
    if len(text) <= max_len:
        return text
    cut = text[:max_len]
    dot = cut.rfind(". ")
    if dot == -1:
        dot = cut.rfind(".")
    if dot >= 60:
        return cut[:dot + 1].strip()
    sp = cut.rfind(" ")
    return (cut[:sp].rstrip() if sp > 60 else cut.rstrip()) + "..."


# ── DEDUPLICATION ─────────────────────────────────────────────────────────────

def _coarse_status(cat):
    """Collapse fine categories to a coarse status so the same real event from
    different sources maps to one key."""
    c = (cat or "").lower()
    if "out" in c:                 return "out"
    if "tbc" in c or "test" in c:  return "tbc"
    if "named" in c:               return "named"
    if "drop" in c or "omit" in c: return "dropped"
    if "vest" in c:                return "vest"
    if "role" in c:                return "role"
    return c or "news"


def deduplicate(items):
    """Remove cross-source duplicates: at most one item per player + coarse
    status, keeping the most reliable source (AFL.com.au 96 > Footywire 94 >
    others). This is what stops Footywire re-reporting an AFL.com.au injury as a
    second feed item. Items with no player (general news) are never collapsed."""
    seen = {}
    unique = []
    for item in items:
        pl  = (item.get("player") or "").strip().lower()
        pid = item.get("pid")
        if not pl and not pid:
            # General/no-player news — keep every distinct headline.
            key = "gen|" + (item.get("headline") or str(id(item)))[:80].lower()
        else:
            ident = pl or f"pid:{pid}"
            key = f"{ident}|{_coarse_status(item.get('category'))}"
        if key not in seen:
            seen[key] = item
            unique.append(item)
        else:
            existing = seen[key]
            if (item.get("reliability", 0) or 0) > (existing.get("reliability", 0) or 0):
                unique[unique.index(existing)] = item
                seen[key] = item
    return unique


# ── MAIN ─────────────────────────────────────────────────────────────────────

def _is_aflw(item):
    """True if a news item is about AFLW / women's football — excluded because
    this app tracks the men's AFL competition only."""
    text = f"{item.get('headline','')} {item.get('body','')} {item.get('player','')}".lower()
    if "aflw" in text or "women" in text:
        return True
    # Exclude other sports that share surnames with AFL players.
    return any(w in text for w in ("nrl", "rugby", "cricket", "netball", "a-league", "soccer", "casualty ward"))


RUMOUR_BUFFER_PATH = BASE_DIR / "rumours.json"

def _watch_rumour_text(player, bodypart, out=False, eta=""):
    """Varied, natural watch/rumour phrasing so the summary never just repeats
    the headline. We only know body part + status (not where/how it happened),
    so phrasing stays honest and doesn't invent a cause."""
    bp = (bodypart or "").strip().lower()
    _art = "an" if bp[:1] in "aeiou" else "a"
    # Body parts read as "a foot injury"; conditions that are already a noun
    # (concussion, illness, …) read as-is so we don't say "a concussion injury".
    _conditions = ("concussion", "illness", "virus", "soreness", "suspension",
                   "managed", "rest", "personal", "corked")
    if bp and bp not in ("tbc", "test", "managed", "na", ""):
        bpp = f"{_art} {bp}" if any(c in bp for c in _conditions) else f"{_art} {bp} injury"
    else:
        bpp = "a knock"
    eta = (eta or "").strip()
    idx = sum(ord(c) for c in player) % 4
    if out:
        heads = [f"{player} set to miss", f"{player} ruled out",
                 f"Blow for {player}", f"{player} sidelined"]
        bodies = [
            f"Word is {player} won't take his place after copping {bpp}" + (f"; out {eta}" if eta and eta.lower() not in ("tbc", "test") else "") + ". Likely a trade-out until he's right.",
            f"{player} is on the sidelines with {bpp}; expect a price dip while he's out.",
            f"{player} is set to miss this week with {bpp}; line up cover at selection.",
            f"{player} won't feature, troubled by {bpp}. One to move on from for now.",
        ]
    else:
        heads = [f"Doubt over {player}", f"{player} under an injury cloud",
                 f"Watch: {player}", f"{player} on the watchlist"]
        bodies = [
            f"{player} pulled up sore with {bpp} and is in doubt for this week.",
            f"{player} is in doubt after a {bp or 'minor'} complaint; keep an eye on selection news this week.",
            f"Whispers {player} is racing the clock on {bpp} ahead of the weekend — risky to lock in.",
            f"{player} flagged with {bpp}; wait for the named team before trusting him.",
        ]
    return heads[idx], bodies[idx]

def _apply_rumour_buffer(items, keep=15, min_items=15, reframe_pool=None):
    """Keep a rolling buffer of the most recent rumours (persisted across runs in
    rumours.json) so the mill always has a healthy set — oldest are replaced as
    new ones arrive, and anything older than RUMOUR_BUFFER_MAX_DAYS is dropped.
    A single scrape only yields a few rumours, so when the live sources are dry
    (Nitter is defunct, Google News carries no rumour bodies) we top the mill up
    by reframing real injury-cloud players from `reframe_pool` (the full injury
    list, before the real-time filter strips it) as watch-style rumours."""
    now = datetime.now(timezone.utc).isoformat()
    is_rum = lambda it: (it.get("is_rumour") or it.get("type") == "rumour") and it.get("player")
    fresh = [it for it in items if is_rum(it)]
    for it in fresh:
        it["_seen"] = now
    try:
        buf = json.loads(RUMOUR_BUFFER_PATH.read_text(encoding="utf-8"))
    except Exception:
        buf = []
    # Drop stale rumours so old whispers filter out instead of lingering.
    buf = [it for it in buf if not _too_old(it.get("_seen"), RUMOUR_BUFFER_MAX_DAYS)]
    merged, seen = [], set()
    for it in fresh + buf:
        key = ((it.get("player") or "").lower(), (it.get("headline") or "")[:60].lower())
        if key in seen:
            continue
        seen.add(key)
        merged.append(it)
    merged.sort(key=lambda x: x.get("_seen", ""), reverse=True)
    merged = merged[:keep]
    try:
        RUMOUR_BUFFER_PATH.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    except Exception:
        pass
    log.info(f"Rumour buffer: {len(merged)} live rumours retained (rolling)")

    # Pad to a minimum so the mill always looks populated. Use REAL ongoing
    # injuries reframed as watch-style rumours (never mock/fabricated players).
    if len(merged) < min_items:
        have = {(it.get("player") or "").lower() for it in merged}
        for it in (reframe_pool if reframe_pool is not None else items):
            if len(merged) >= min_items:
                break
            if it.get("type") != "injury" or not it.get("player"):
                continue
            # Confirmed OUT from an official source is NEWS, not a rumour — never
            # reframe it into the mill (this is what made confirmed injuries show
            # up as high-"confidence" rumours).
            if it.get("category") == "injury_out" and (it.get("reliability") or 0) >= 90:
                continue
            pl = it["player"]
            if pl.lower() in have:
                continue
            have.add(pl.lower())
            tags = it.get("tags") or []
            bp = (tags[1] if len(tags) > 1 else "") or ""
            eta = tags[2] if len(tags) > 2 else ""
            out = it.get("category") == "injury_out"
            hd, bd = _watch_rumour_text(pl, bp, out=out, eta=eta)
            merged.append({**it, "type": "rumour", "is_rumour": True, "confirmed": False,
                           "category": "injury_out" if out else "injury_tbc",
                           "signal": "sell" if out else "hold",
                           "headline": hd, "body": _strip_fantasy_advice(bd),
                           "tags": ["Rumour", "Watch"], "_seen": now, "_padded": True})
        log.info(f"Rumour buffer: padded to {len(merged)} with reframed injuries")

    return [it for it in items if not is_rum(it)] + merged

TEAM_SEL_PATH = BASE_DIR / "team_selections.json"

def scrape_team_selections(session, player_idx):
    """Diff the AFL team-selection page week-over-week and surface only the
    CHANGES (newly named = IN, dropped off = OUT). Each run's named players are
    stored in team_selections.json so the next run can diff against it.

    The /matches/team-selection page is only populated on team-announcement days
    (Thu/Fri); when empty/unavailable this returns []."""
    items = []
    r = fetch(session, AFL_TEAMS_PAGE)
    if not r:
        log.info("Team selections: AFL team page unavailable (no announcement yet)")
        return items
    page = BeautifulSoup(r.text, "lxml").get_text(" ", strip=True).lower()
    current = {pl["pid"]: _PID_NAME[pl["pid"]]
               for pl in _PLAYERS_IDX if len(pl["full"]) > 6 and pl["full"] in page}
    if not current:
        log.info("Team selections: no named teams detected on AFL page yet")
        return items
    try:
        prev = json.loads(TEAM_SEL_PATH.read_text(encoding="utf-8"))
    except Exception:
        prev = {}
    prev_pids, cur_pids = set(prev.get("named_pids", [])), set(current.keys())

    def _item(name, pid, kind):
        head = f"IN: {name} returns to the side" if kind == "in" else f"OUT: {name} dropped"
        return {"id": None, "type": "selection",
                "category": "named" if kind == "in" else "dropped", "urgent": False,
                "player": name, "pid": pid, "team": None, "pos": None,
                "source": "AFL.com.au", "sourceHandle": "@aflcomau", "reliability": 95,
                "time": "latest", "timeLabel": "Team news", "headline": head,
                "body": f"{name} {'named' if kind == 'in' else 'omitted'} in this week's team selection.",
                "signal": "buy" if kind == "in" else "sell", "signalConf": 85,
                "tags": ["Selection", "IN" if kind == "in" else "OUT"], "stats": [],
                "relevance": 90, "_source": "team_selections"}

    if prev_pids:  # need a baseline before reporting changes
        for pid in cur_pids - prev_pids:
            items.append(_item(current[pid], pid, "in"))
        for pid in prev_pids - cur_pids:
            items.append(_item(prev.get("names", {}).get(str(pid), "Player"), pid, "out"))
    TEAM_SEL_PATH.write_text(json.dumps(
        {"named_pids": list(cur_pids), "names": {str(k): v for k, v in current.items()}},
        indent=2), encoding="utf-8")
    log.info(f"Team selections: {len(items)} changes from {len(cur_pids)} named players")
    return items

def _eta_text(item):
    tags = item.get("tags") or []
    return " ".join(str(p) for p in (list(tags) + [item.get("body", "") or "", item.get("headline", "") or ""]))


def _injury_eta_weeks(txt):
    """Best-effort weeks-until-return parsed from an injury item's text."""
    t = (txt or "").lower()
    if "season" in t or "indefinite" in t:
        return 99
    m = re.search(r"(\d+)\s*[-to ]+\s*(\d+)\s*week", t)
    if m:
        return int(m.group(2))
    m = re.search(r"(\d+)\s*week", t)
    if m:
        return int(m.group(1))
    if re.search(r"\d+\s*day", t):
        return 0
    return None


def _set_stale_after(item):
    """Tag an item with how many days until it counts as stale news."""
    if item.get("type") != "injury":
        item["stale_after_days"] = 7
        return
    txt = _eta_text(item).lower()
    weeks = _injury_eta_weeks(txt)
    if "season" in txt or weeks == 99:
        item["stale_after_days"] = 1
    elif item.get("category") == "injury_tbc":
        item["stale_after_days"] = 2
    elif weeks is not None and weeks > 4:
        item["stale_after_days"] = 3
    elif weeks is not None and 1 <= weeks <= 4:
        item["stale_after_days"] = 5
    else:
        item["stale_after_days"] = 5


def _is_stale_ongoing(item):
    """An ONGOING injury is stale once older than its staleness budget — unless
    it's urgent, just changed status, or the player returns within ~2 rounds."""
    if item.get("status") != "ongoing" or item.get("type") != "injury":
        return False
    if item.get("urgent") or item.get("status_changed"):
        return False
    weeks = _injury_eta_weeks(_eta_text(item))
    if weeks is not None and weeks <= 2:
        return False
    fs = item.get("first_seen")
    if not fs:
        return False
    try:
        age_days = (datetime.now(timezone.utc)
                    - datetime.fromisoformat(str(fs).replace("Z", "+00:00"))).days
    except Exception:
        return False
    return age_days > item.get("stale_after_days", 7)


def reclassify_item(item):
    """Re-categorise an item by headline/body keywords, correcting classify_item
    mislabels. Sets item['_skip']=True for items that should not appear at all
    (captaincy/coaching/awards/AFLW/etc)."""
    # Structured scrapers already assign accurate, trusted categories
    # (named/dropped/vest_risk/injury_out/etc) — don't let keyword guessing
    # override them (e.g. a 'dropped' headline containing "out:" must NOT
    # become an injury).
    if item.get("_source") in ("team_announcements", "afl_team_selections",
                               "footywire_selections", "footywire_injuries",
                               "afl_medical_room", "afl_injury_page"):
        return item
    headline = (item.get("headline", "") + " " + item.get("body", "")).lower()

    # Injury keywords - highest priority
    if any(x in headline for x in ["out:", "tbc:", "hamstring", "knee", "shoulder", "concussion",
                                   "ankle", "calf", "foot", "hip", "groin", "ruled out", "will miss",
                                   "injury", "injured", "soreness", "sore", "managed",
                                   "test his fitness", "race the clock", "in doubt"]):
        item["type"] = "injury"
        item["category"] = "injury_out" if any(x in headline for x in ["out:", "ruled out", "will miss", "season"]) else "injury_tbc"
        return item

    # Selection/team news keywords
    if any(x in headline for x in ["teams:", "team:", "named", "selected", "recalled", "omitted",
                                   "dropped", "in for", "out for", "emergencies", "late out", "changes",
                                   "makes", "call", "returns", "guns return", "will face", "lineup", "squad"]):
        item["type"] = "selection"
        item["category"] = "team_news"
        return item

    # Captaincy, coaching, AFLW etc — filtered OUT entirely, not shown as General
    if any(x in headline for x in ["captain", "captaincy", "coach", "coaching", "aflw",
                                   "women", "brownlow", "award", "contract", "re-signed", "umpire", "fixture"]):
        item["_skip"] = True
        return item

    # Fantasy/price specific
    if any(x in headline for x in ["supercoach", "fantasy", "break-even", "price", "trade in", "trade out"]):
        item["type"] = "analysis"
        item["category"] = "price"
        return item

    # Default to general
    item["type"] = "news"
    item["category"] = "general"
    return item


def enforce_category(item):
    """Final strict classification pass: block non-fantasy news outright and
    force every surviving item into injury / selection / analysis."""
    headline = (item.get("headline", "") or "").lower()
    body = (item.get("body", "") or "").lower()
    text = headline + " " + body

    block_phrases = [
        "captaincy", "vice-captain", "coach of the year", "brownlow",
        "best and fairest", "aflw", "women's afl", "nab league",
        "umpire", "fixture change", "venue change", "crowd",
        "contract extension", "re-signed", "retirement", "farewell",
        "hannah priest", "inaugural saint", "fagan's message",
        "out-of-form midfield", "get back to basics",
        "hands over", "reins", "captaincy reins",
    ]
    for phrase in block_phrases:
        if phrase in text:
            item["_skip"] = True
            return item

    injury_words = ["out:", "tbc:", "hamstring", "knee", "shoulder", "concussion",
                    "ankle", "calf", "foot injury", "hip", "groin", "ruled out",
                    "will miss", "injured", "soreness", "fracture", "strain",
                    "torn", "managed", "racing the clock", "in doubt", "test his fitness"]
    if any(w in text for w in injury_words):
        item["type"] = "injury"
        item["category"] = "injury_out" if any(w in text for w in ["out:", "ruled out", "will miss", "season", "6-8 weeks", "4-6 weeks"]) else "injury_tbc"
        return item

    selection_words = ["teams:", "in:", "out:", "named", "recalled", "selected",
                       "omitted", "dropped", "emergencies", "late out", "in for",
                       "returns for", "guns return", "will face", "makes changes",
                       "four changes", "unchanged", "lineup"]
    if any(w in text for w in selection_words):
        item["type"] = "selection"
        item["category"] = "team_news"
        return item

    fantasy_words = ["supercoach", "afl fantasy", "break-even", "fantasy relevant",
                     "must trade", "trade target", "waiver", "draft pick"]
    if any(w in text for w in fantasy_words):
        item["type"] = "analysis"
        item["category"] = "price"
        return item

    # Keep general AFL news rather than dropping it. This block was collapsing
    # the feed to ~15 injury items: every story that wasn't an injury/selection/
    # fantasy-keyword match got skipped, even though afl_rss (~48) and club pages
    # (~34) return that many valid AFL items each run. The block_phrases above
    # still filter AFLW / off-topic fluff, so the feed stays AFL-relevant.
    item["type"] = item.get("type") or "news"
    item["category"] = item.get("category") or "general"
    return item


OFFICIAL_NEVER_RUMOUR = ("afl.com.au", "footywire", "champion data")

_ADVICE_RE = re.compile(
    r"(expect a price dip|trade or bench|bench until|trade[- ]out|monitor team news|"
    r"risky to (?:field|lock)|keep an eye on selection|line up cover|"
    r"worth watching at lockout|risky sc|one to move on from|wait for the named team)",
    re.I)

def _strip_fantasy_advice(body):
    """Remove fantasy/financial advice sentences from a news body so injury items
    state only the fact (injury + ETA), not trade advice. Applied to every item
    each run so already-archived bodies get cleaned too."""
    if not body:
        return body
    parts = re.split(r"(?<=[.!?;])\s+|\s+[—–-]\s+", body)
    kept = [p.strip() for p in parts if p and not _ADVICE_RE.search(p)]
    if not kept:
        return body
    out = re.sub(r"\s*[;,]\s*$", "", " ".join(kept).strip()).strip()
    if out and out[-1] not in ".!?":
        out += "."
    return out or body

def enforce_official_not_rumour(items):
    """AFL.com.au / Footywire / Champion Data are confirmed sources — their items
    are NEWS, never rumours. Force is_rumour False and demote any 'rumour' type."""
    for it in items:
        src = (it.get("source") or "").lower()
        if any(o in src for o in OFFICIAL_NEVER_RUMOUR):
            it["is_rumour"] = False
            if it.get("type") == "rumour":
                it["type"] = "selection" if "named" in (it.get("category") or "") \
                    or "drop" in (it.get("category") or "") else "injury"
    return items


def remove_superseded_rumours(all_items):
    """Drop any rumour that duplicates a CONFIRMED news item (same player AND
    category, reliability >= 90) — the confirmed news supersedes the speculation."""
    confirmed = {(i.get("player"), i.get("category"))
                 for i in all_items
                 if not i.get("is_rumour") and (i.get("reliability", 0) or 0) >= 90}
    return [i for i in all_items
            if not i.get("is_rumour")
            or (i.get("player"), i.get("category")) not in confirmed]


def scrape_all_news(players=None):
    """
    Run all scrapers and return merged, filtered, sorted news list.
    players: list of player dicts (from players.json or PLAYERS mock)
             If None, loads from players.json if available.
    """
    if players is None:
        players_path = BASE_DIR / "players.json"
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
    # Each scraper's contribution is captured and logged so it's obvious which
    # sources are returning 0 (the usual cause of a Footywire-only feed).
    source_counts = {}
    def _run(label, fn):
        try:
            got = fn(session, player_idx) or []
        except Exception as e:
            log.exception(f"Source {label}: FAILED ({e})")
            got = []
        source_counts[label] = len(got)
        log.info(f"Source {label}: {len(got)} items")
        return got

    all_items += _run("afl_team_selections", scrape_afl_team_selections); time.sleep(1)
    all_items += _run("team_announcements",  scrape_team_announcements);   time.sleep(1)
    all_items += _run("team_news_articles",  scrape_team_news_articles);   time.sleep(1)
    all_items += _run("afl_medical_room",    scrape_afl_medical_room);    time.sleep(1)
    all_items += _run("afl_injury_page",     scrape_afl_injury_page);     time.sleep(1)
    all_items += _run("footywire_injuries",  scrape_fw_injuries);         time.sleep(1)
    all_items += _run("footywire_selections",scrape_fw_selections);       time.sleep(1)
    all_items += _run("team_selections",      scrape_team_selections);     time.sleep(1)
    all_items += _run("afl_rss",             scrape_afl_rss);             time.sleep(1)
    all_items += _run("google_news",         scrape_google_news);         time.sleep(1)
    all_items += _run("twitter_rss",         scrape_twitter_rss);         time.sleep(1)
    all_items += _run("club_pages",          scrape_club_news)

    log.info("Source contribution summary: "
             + ", ".join(f"{k}={v}" for k, v in source_counts.items()))

    # Snapshot the full injury list NOW, before the real-time/stale filters strip
    # it, so the rumour mill can be topped up with watch-style reframes of real
    # injury-cloud players when the live rumour sources are dry.
    _injury_pool = [it for it in all_items if it.get("type") == "injury" and it.get("player")]

    # ── Per-source diagnostic (which sources returned 0?) ──
    print("--- scraper source counts ---")
    print(f"footywire_injuries:   {source_counts.get('footywire_injuries', 0)} items")
    print(f"footywire_selections: {source_counts.get('footywire_selections', 0)} items")
    print(f"afl_rss:              {source_counts.get('afl_rss', 0)} items")
    print(f"club_pages:           {source_counts.get('club_pages', 0)} items")
    print(f"twitter_rss:          {source_counts.get('twitter_rss', 0)} items")
    print(f"afl_injury_page:      {source_counts.get('afl_injury_page', 0)} items")
    print(f"afl_medical_room:     {source_counts.get('afl_medical_room', 0)} items")
    print(f"afl_team_selections:  {source_counts.get('afl_team_selections', 0)} items")
    print(f"google_news:          {source_counts.get('google_news', 0)} items")
    zero = [k for k, v in source_counts.items() if v == 0]
    if zero:
        print(f"ZERO-ITEM sources: {', '.join(zero)}")
    print("-----------------------------")

    # ── Drop AFLW / women's football items (men's AFL only) ──
    before_aflw = len(all_items)
    all_items = [it for it in all_items if not _is_aflw(it)]
    log.info(f"AFLW filter: kept {len(all_items)}/{before_aflw} items (dropped women's-football)")

    # ── Recency filter (keep last 48h only) ──
    all_items = filter_recent(all_items, max_age_hours=48)

    # ── Re-categorise by headline keywords (corrects classify_item mislabels)
    # and drop irrelevant items (captaincy/coaching/awards/AFLW/etc) ──
    all_items = [reclassify_item(item) for item in all_items]
    all_items = [item for item in all_items if not item.get("_skip")]
    from collections import Counter
    types = Counter(item["type"] for item in all_items)
    log.info(f"Item types after classification: {dict(types)}")

    # ── Deduplicate ──
    all_items = deduplicate(all_items)

    # ── Official sources are NEWS, never rumours ──
    all_items = enforce_official_not_rumour(all_items)

    # ── Rumour-vs-status filter ──
    # Drop rumours about confirmed-OUT players (the rumour is stale), and flag
    # rumours about TBC/managed players as low-confidence rather than dropping.
    all_items = filter_rumours_by_status(all_items, players)

    # ── Apply history tracking (NEW / ONGOING / UPDATE / RESOLVED) ──
    history = NewsHistory()
    all_items = history.process(all_items)

    # Tag staleness budget + drop stale ONGOING injuries (kept if urgent /
    # status changed / returning within 2 rounds) so long-known injuries don't
    # clog the feed.
    for _it in all_items:
        _set_stale_after(_it)
    _before_stale = len(all_items)
    all_items = [i for i in all_items if not _is_stale_ongoing(i)]
    log.info(f"Stale-injury filter: kept {len(all_items)}/{_before_stale} items")

    # ── Real-time-only filter (per spec): drop ongoing items where the player's
    # status and the item content haven't changed since the previous scrape.
    # This is what stops the feed re-emitting the same "Cripps TBC" 24×/day.
    before = len(all_items)
    all_items = history.filter_real_time(all_items)
    log.info(f"Real-time filter: kept {len(all_items)}/{before} items (dropped ongoing/no-change)")

    history.save()

    # ── Body quality gate: discard items whose body is empty, too short, or
    # just repeats the headline / player name. Never publish a generic template. ──
    import difflib
    _clean_items = []
    for it in all_items:
        _src = ((it.get("source") or "") + " " + (it.get("link") or "") + " " + (it.get("sourceHandle") or "")).lower()
        if any(bs in _src for bs in BLOCKED_SOURCES):
            continue
        it["body"] = _strip_fantasy_advice(_tidy_body(it.get("body"), it.get("headline", "")))
        b = (it.get("body") or "").strip()
        h = (it.get("headline") or "").strip()
        pl = (it.get("player") or "").strip()
        if len(b) < 30:
            continue
        if pl and b.lower() == pl.lower():
            continue
        if h and (b.lower() == h.lower() or (b.lower().startswith(h.lower()) and len(b) <= len(h) + 24)):
            continue
        _clean_items.append(it)

    # Dedup by player + near-identical body (>80% similar): keep the most recent.
    _by_player, _deduped = {}, []
    for it in sorted(_clean_items, key=lambda x: x.get("scrapedAt", "") or x.get("time", ""), reverse=True):
        plk = (it.get("player") or "").lower()
        bod = (it.get("body") or "").lower()
        if any(difflib.SequenceMatcher(None, bod, prev).ratio() > 0.8 for prev in _by_player.get(plk, [])):
            continue
        _by_player.setdefault(plk, []).append(bod)
        _deduped.append(it)
    all_items = _deduped
    log.info(f"Body quality + dedup: {len(all_items)} items kept")

    # ── Sort: urgent first, then NEW > UPDATE > RESOLVED, then by relevance ──
    all_items = _apply_rumour_buffer(all_items, reframe_pool=_injury_pool)
    # Drop rumours a confirmed news item already supersedes (same player+category).
    all_items = remove_superseded_rumours(all_items)

    status_rank = {"new": 0, "update": 1, "resolved": 2, "ongoing": 3}
    all_items.sort(key=lambda x: (
        0 if x.get("urgent") else 1,
        status_rank.get(x.get("status",""), 4),
        -x.get("relevance", 0)
    ))

    # ── Assign sequential IDs ──
    # Cap the injury wall so the feed stays a news mix, not a static injury list.
    capped, _inj = [], 0
    for it in all_items:
        if it.get("type") == "injury":
            _inj += 1
            if _inj > 15:
                continue
        capped.append(it)
    all_items = capped

    _scraped_dt = datetime.now(timezone.utc)
    _scraped_iso = _scraped_dt.isoformat()
    for i, item in enumerate(all_items, 1):
        item["id"] = i
        item["scrapedAt"] = _scraped_iso
        # Display timestamp = when the news was FIRST published, NOT this scrape.
        #  - RSS/Twitter: the <pubDate> we captured in pubISO.
        #  - Everything else (Footywire/AFL injury rows): the time NewsHistory
        #    assigned — now_str for NEW/UPDATE, the original first_seen for
        #    ONGOING — so a 2-day-old injury reads "2d ago", not "now".
        _dt = None
        for _src in (item.get("pubISO"), item.get("time"), item.get("first_seen")):
            if isinstance(_src, str) and "T" in _src and ":" in _src:
                try:
                    _dt = datetime.fromisoformat(_src.replace("Z", "+00:00"))
                    if _dt.tzinfo is None:
                        _dt = _dt.replace(tzinfo=timezone.utc)
                    break
                except Exception:
                    _dt = None
        if _dt is None:
            _dt = _scraped_dt
        _iso = _dt.astimezone(timezone.utc).isoformat()
        # first_seen anchors to the best stable source (pubDate for RSS, the
        # original NewsHistory time for injuries) so an item's age is constant
        # across scrapes.
        item["first_seen"] = _iso
        item["time"] = _iso
        item["timeLabel"] = _label_from_dt(_dt)

    log.info(f"Total news items: {len(all_items)}")
    return all_items


NEWS_ARCHIVE_CAP = 500

# Injuries & selections now accumulate in the archive like any other news, but
# age out after this many days so resolved / long-known statuses slowly filter
# out as fresher items arrive (instead of being dropped wholesale every run,
# which made the feed count seesaw). An injury UPDATE re-enters as a fresh item.
INJURY_FEED_MAX_DAYS = 10

def _too_old(iso_or_label, max_days):
    """True if an ISO timestamp is older than max_days. Non-ISO/blank values
    (e.g. 'latest') are treated as fresh so we never drop an item we can't date."""
    s = (iso_or_label or "").strip()
    if "T" not in s:
        return False
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).days > max_days
    except Exception:
        return False

def _balance_feed_order(items, max_run=3):
    """Reorder (never drop) so the feed doesn't open with a wall of injuries.

    Items arrive newest-first. We keep that order but, whenever more than
    `max_run` injuries would appear consecutively, we promote the nearest
    non-injury item to break the run. Every item is preserved — this only
    affects display order, so the archive can still accumulate to the 500 cap.
    """
    queue = list(items)
    out   = []
    run   = 0
    while queue:
        if queue[0].get("type") == "injury" and run >= max_run:
            promote = next((j for j, it in enumerate(queue)
                            if it.get("type") != "injury"), None)
            if promote is not None:
                out.append(queue.pop(promote))
                run = 0
                continue
            # Only injuries remain — flush them in order.
        it = queue.pop(0)
        out.append(it)
        run = run + 1 if it.get("type") == "injury" else 0
    return out


def _merge_news_archive(new_items, cap=NEWS_ARCHIVE_CAP):
    """Accumulate news across scrapes instead of replacing the file each run:
    merge fresh items with the existing news.json, dedupe (fresh wins), keep the
    newest `cap` items by scrape time and drop the oldest beyond that."""
    now = datetime.now(timezone.utc).isoformat()
    for it in new_items:
        it.setdefault("scrapedAt", now)
    try:
        existing = json.loads(OUTPUT_PATH.read_text(encoding="utf-8")).get("news", [])
    except Exception:
        existing = []
    def _ok(it):
        body = (it.get("body") or "").strip()
        src = ((it.get("source") or "") + " " + (it.get("link") or "") + " " + (it.get("sourceHandle") or "")).lower()
        if any(b in src for b in BLOCKED_SOURCES):
            return False
        if any(x in body.lower() for x in ("monitor team news", "before lockout", "report on")):
            return False
        if len(body) < 30:
            return False
        return True
    import difflib
    merged, seen, _sig = [], set(), []
    def _similar(pl, tx):
        for ppl, ptx in _sig:
            if ppl == pl and difflib.SequenceMatcher(None, tx, ptx).ratio() > 0.82:
                return True
        return False
    def _key(it):
        return ((it.get("player") or "").lower(),
                (it.get("headline") or "")[:80].lower(),
                it.get("type", ""))
    # Existing items have earned their slot: keep them ALL (exact-key dedup only)
    # so the archive ACCUMULATES toward the cap instead of collapsing when bodies
    # look similar (e.g. after advice text is stripped). Their signatures are
    # recorded so we don't re-add a near-duplicate as if it were new.
    for it in existing:
        if not _ok(it):
            continue
        # Padded reframe-rumours (injury-cloud watch items) are regenerated fresh
        # every run by _apply_rumour_buffer, so never carry the old ones over —
        # otherwise stale-worded copies pile up in the archive.
        if it.get("_padded"):
            continue
        # Injuries & selections accumulate like any other news item, but age out
        # after INJURY_FEED_MAX_DAYS so resolved / long-known statuses slowly
        # filter out as fresher items arrive (a status change re-enters as a
        # fresh item). They carry a real first_seen from NewsHistory, so they
        # sort and roll off chronologically. The old "pinned forever" bug came
        # from un-enriched items with no category/status that cross-source dedup
        # couldn't collapse; today's injuries are fully enriched, so dedup +
        # this age-out keep the feed clean.
        if it.get("type") in ("injury", "selection"):
            if _too_old(it.get("first_seen") or it.get("scrapedAt"), INJURY_FEED_MAX_DAYS):
                continue
        key = _key(it)
        if key in seen:
            continue
        seen.add(key)
        _sig.append(((it.get("player") or "").lower(),
                     ((it.get("headline") or "") + " " + (it.get("body") or "")).lower()))
        merged.append(it)
    # Add only genuinely-new stories: skip exact-key dups and >82%-similar
    # same-player stories already in the archive.
    for it in new_items:
        if not _ok(it):
            continue
        key = _key(it)
        if key in seen:
            continue
        pl = (it.get("player") or "").lower()
        tx = ((it.get("headline") or "") + " " + (it.get("body") or "")).lower()
        if pl and _similar(pl, tx):
            continue
        seen.add(key)
        _sig.append((pl, tx))
        merged.append(it)
    # Newest first by publication time, then roll off the oldest beyond the cap
    # (500). Once full, each new item displaces the oldest.
    merged.sort(key=lambda x: x.get("first_seen") or x.get("time") or x.get("scrapedAt") or "", reverse=True)
    merged = merged[:cap]
    # Normalise timestamps across fresh + archived items: derive a real age label
    # from each item's ISO `time` (preferred) or its `scrapedAt`. Older archive
    # items that still carry "latest"/"recent" get rewritten here.
    _merge_dt = datetime.now(timezone.utc)
    for i, it in enumerate(merged, 1):
        it["id"] = i
        # Prefer first_seen (original publication) so an item's age is stable
        # across runs; fall back to time, then scrapedAt.
        _dt = None
        for ts in (it.get("first_seen"), it.get("time"), it.get("scrapedAt")):
            if isinstance(ts, str) and "T" in ts and ":" in ts:
                try:
                    _dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    if _dt.tzinfo is None:
                        _dt = _dt.replace(tzinfo=timezone.utc)
                    break
                except Exception:
                    _dt = None
        # Old archive items with no usable timestamp: synthesise one from their
        # position (they sort oldest-last) so nothing ever reads "latest".
        if _dt is None:
            _dt = _merge_dt - timedelta(minutes=(i - 1) * 3)
            it["scrapedAt"] = _dt.isoformat()
        _iso = _dt.astimezone(timezone.utc).isoformat()
        it["first_seen"] = it.get("first_seen") or _iso
        it["time"] = _iso
        it["timeLabel"] = _label_from_dt(_dt)
    log.info(f"News archive: {len(new_items)} new merged with existing -> {len(merged)} kept (cap {cap})")
    return merged


FAST_FILL_RSS = [
    "https://news.google.com/rss/search?q=AFL+football&hl=en-AU&gl=AU&ceid=AU:en",
    "https://news.google.com/rss/search?q=AFL+SuperCoach+fantasy&hl=en-AU&gl=AU&ceid=AU:en",
    "https://news.google.com/rss/search?q=AFL+injury+selection&hl=en-AU&gl=AU&ceid=AU:en",
    "https://news.google.com/rss/search?q=AFL+team+news+2026&hl=en-AU&gl=AU&ceid=AU:en",
    "https://news.google.com/rss/search?q=SuperCoach+2026&hl=en-AU&gl=AU&ceid=AU:en",
    "https://www.afl.com.au/rss",
    "https://www.abc.net.au/news/feed/51120/rss.xml",
]


def fast_fill():
    """Bulk-scrape AFL-related articles with relaxed (non-fantasy-strict) rules
    to grow the archive toward the 500-item cap quickly. AFLW / off-topic items
    are still excluded (AFL-only). Returns a list of news items; writes nothing."""
    from email.utils import parsedate_to_datetime
    session = make_session()
    items = []
    for url in FAST_FILL_RSS:
        try:
            r = session.get(url, timeout=15)
            soup = BeautifulSoup(r.content, "xml")
            for it in soup.find_all("item")[:50]:
                title = it.find("title")
                if not title:
                    continue
                headline = title.get_text(strip=True)
                hl = headline.lower()
                if not any(w in hl for w in ["afl", "football", "footy", "supercoach",
                                             "fantasy", "injury", "named", "selection", "round"]):
                    continue
                desc = it.find("description")
                body = BeautifulSoup(desc.get_text() if desc else "", "lxml").get_text()[:200] if desc else ""
                link = it.find("link")
                pub  = it.find("pubDate")
                ts = datetime.now(timezone.utc).isoformat()
                if pub:
                    try:
                        ts = parsedate_to_datetime(pub.get_text()).isoformat()
                    except Exception:
                        pass
                host = url.split("/")[2]
                item = {
                    "type": "news", "category": "general", "urgent": False,
                    "player": "", "headline": headline,
                    "body": (body or headline)[:200],
                    "source": host,
                    "sourceHandle": "@" + host.replace("www.", "").split(".")[0],
                    "reliability": 65, "time": ts, "first_seen": ts, "timeLabel": "",
                    "link": link.get_text() if link else "",
                    "is_rumour": False, "tags": ["News"], "_source": "fast_fill",
                }
                if _is_aflw(item):   # respect the AFL-only rule
                    continue
                items.append(item)
        except Exception as e:
            log.warning(f"fast_fill {url}: {e}")
    log.info(f"fast_fill: collected {len(items)} raw items")
    return items


AI_ENDPOINT = "https://aflfantasywire.ensor-jack.workers.dev/api/ai"


def fetch_article_text(session, url, cap=9000):
    """Fetch a news article and extract its readable paragraph text (best-effort).
    Returns '' on any failure (paywall/block/network) so the scrape never breaks."""
    if not url or not url.lower().startswith("http"):
        return ""
    try:
        r = session.get(url, timeout=10)
        if not r or r.status_code != 200:
            return ""
        soup = BeautifulSoup(r.text, "lxml")
        for t in soup(["script", "style", "noscript"]):
            t.extract()
        ps = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
        text = "\n".join(x for x in ps if len(x) > 40)
        if len(text) < 400:
            text = soup.get_text(" ", strip=True)
        return re.sub(r"\s+", " ", text).strip()[:cap]
    except Exception:
        return ""


def ai_summarise(headline, body, full=False):
    """AI summary of a news item (fantasy angle). `full` produces a 3-4 sentence
    summary of a whole article; otherwise a 1-2 sentence snippet summary. Returns
    '' on any failure so the scrape never breaks."""
    try:
        if full:
            q = ("Summarise this AFL article for fantasy (SuperCoach/AFL Fantasy) coaches in "
                 "3-4 sentences, leading with selection, injury, role, form and price "
                 f"implications. Headline: {headline}. Article: {body}. Plain text, no preamble.")
        else:
            q = ("In 1-2 punchy sentences, summarise this AFL news for fantasy coaches. "
                 f"Headline: {headline}. Detail: {body}. Plain text, no preamble.")
        r = requests.post(AI_ENDPOINT, json={
            "model": "claude-opus-4-7", "max_tokens": 2000,
            "thinking": {"type": "adaptive"}, "output_config": {"effort": "low"},
            "system": "You are a concise AFL fantasy news editor.",
            "messages": [{"role": "user", "content": q}]}, timeout=30)
        K = r.json()
        if not r.ok or K.get("error"):
            return ""
        return " ".join(b.get("text", "") for b in K.get("content", [])
                        if b.get("type") == "text").strip()
    except Exception as e:
        log.warning(f"ai_summarise failed: {e}")
        return ""


def main():
    print("=" * 60)
    print("  AFLFantasyWire — News Scraper")
    print("=" * 60)
    print(f"  {datetime.now().strftime('%H:%M:%S  %d %b %Y')}\n")

    items = scrape_all_news()

    # ── Strict category enforcement on FRESH items only ──
    # Vet new items (block non-fantasy news, force injury/selection/analysis)
    # BEFORE they enter the archive. Already-archived items keep their slot:
    # re-filtering the whole archive every run and writing back the survivors is
    # what stopped the feed from ever accumulating toward the 500 cap.
    items = [enforce_category(i) for i in items]
    items = [i for i in items if not i.get("_skip")]
    log.info(f"After category enforcement: {len(items)} fresh items vetted")
    from collections import Counter
    print(Counter(i["type"] for i in items))

    # ── Accumulate into the rolling archive (newest 500 kept, oldest roll off) ──
    items = _merge_news_archive(items)

    # ── Cross-source de-dup across the whole archive: one item per player+coarse
    # status, AFL.com.au beating Footywire — kills Footywire regurgitating an
    # AFL.com.au injury that's already in the feed. (General news is preserved.) ──
    _pre = len(items)
    items = deduplicate(items)
    log.info(f"Cross-source dedup: {len(items)}/{_pre} kept")

    # ── Injury items must name a CURRENT player ──
    # The structured injury-list scrapers always set pid (they skip players not
    # in players.json), so a type=="injury" item with no pid can only come from a
    # free-text RSS/news headline whose body tripped an injury keyword — e.g. the
    # Neale Daniher obituaries ("Fight MND … dies aged 65"). Those aren't player
    # injuries: demote them to general news rather than showing them as injury.
    _demoted = 0
    for _it in items:
        if _it.get("type") == "injury" and not _it.get("pid"):
            _it["type"] = "news"
            if (_it.get("category") or "").startswith("injury"):
                _it["category"] = "general"
            _demoted += 1
    if _demoted:
        log.info(f"Demoted {_demoted} player-less injury items to general news")

    # Final type breakdown for visibility.
    _tb = {}
    for i in items:
        _tb[i.get("type", "?")] = _tb.get(i.get("type", "?"), 0) + 1
    print(f"final type breakdown: {_tb}")

    # ── Chronological order: newest first, urgent items pinned to top ──
    def parse_timestamp(item):
        t = item.get("time", "") or item.get("timeLabel", "") or ""
        try:
            if "T" in t and ":" in t:
                return datetime.fromisoformat(t.replace("Z", "+00:00"))
        except Exception:
            pass
        now = datetime.now(timezone.utc)
        t_lower = t.lower().replace(" ago", "").strip()
        try:
            if t_lower.endswith("m"): return now - timedelta(minutes=int(t_lower[:-1]))
            if t_lower.endswith("h"): return now - timedelta(hours=int(t_lower[:-1]))
            if t_lower.endswith("d"): return now - timedelta(days=int(t_lower[:-1]))
        except Exception:
            pass
        return now - timedelta(days=99)

    items.sort(key=lambda x: parse_timestamp(x), reverse=True)
    # Non-destructive variety pass: keep every item but break up long runs of
    # injuries so the top of the feed isn't a wall of them. Nothing is dropped.
    items = _balance_feed_order(items)
    for i, item in enumerate(items, 1):
        item["id"] = i
        # Final pass: strip fantasy/financial advice from every body (incl. items
        # restored from the archive, which never went through the quality gate).
        item["body"] = _strip_fantasy_advice(item.get("body"))
        # Tag every tracked player the article mentions, so the feed can show a
        # chip per player (not just the first match). Falls back to the existing
        # single player/pid when text matching finds none.
        _pls = find_players_all((item.get("headline", "") or "") + " " + (item.get("body", "") or ""))
        if not _pls and item.get("pid") and item.get("player"):
            _pls = [{"pid": item["pid"], "name": item["player"]}]
        if _pls:
            item["players"] = _pls
            if not item.get("pid"):
                item["pid"] = _pls[0]["pid"]
                item["player"] = _pls[0]["name"]

    # ── Degenerate-scrape guard ──
    # When every web source blips out at once (rate limit, network drop) a run
    # can collapse to a handful of items even after the archive merge. Publishing
    # that wreckage is worse than doing nothing: once news.json collapses there's
    # nothing left for the next run's archive merge to restore from, so the feed
    # stays empty until a clean scrape happens to land. So if this run is a
    # catastrophic drop from a previously healthy file, keep the existing file
    # and skip the write. auto_scrape.py then sees no change and won't push.
    try:
        prev_count = len(json.loads(OUTPUT_PATH.read_text(encoding="utf-8")).get("news", []))
    except Exception:
        prev_count = 0
    if prev_count >= 10 and len(items) <= 6 and len(items) < prev_count * 0.4:
        log.error(f"Degenerate scrape: {len(items)} items vs {prev_count} in existing "
                  f"news.json — keeping existing file, NOT overwriting.")
        print(f"\n⚠  Degenerate scrape ({len(items)} vs {prev_count} existing) — "
              f"kept existing news.json, skipped write.")
        return

    # ── AI summaries for NEW news items only (cheap: ~1-2 new/cycle). Items
    # carried from the archive keep their cached summary; never re-summarise. ──
    _asess = requests.Session()
    _asess.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"})
    _ai_budget = 6
    for _it in items:
        if _ai_budget <= 0:
            break
        if _it.get("ai_summary") or _it.get("type") not in ("news", "analysis", "rumour"):
            continue
        _snippet = (_it.get("body") or "")[:400]
        # Pull the full article so the summary reflects the whole piece, not just
        # the RSS snippet. Falls back to the snippet when the source can't be
        # fetched (paywall/block). The richer summary is marked ai_full so the
        # app shows it instantly instead of re-summarising on click.
        _article = fetch_article_text(_asess, _it.get("link", ""))
        _src = _article if len(_article) > len(_snippet) else _snippet
        if not _src:
            continue
        _is_full = bool(_article and len(_article) > 400)
        _summ = ai_summarise(_it.get("headline", ""), _src[:6000], full=_is_full)
        if _summ:
            _it["ai_summary"] = _summ
            if _is_full:
                _it["ai_full"] = True
            _ai_budget -= 1
    log.info(f"AI summaries: {sum(1 for _i in items if _i.get('ai_summary'))} items "
             f"({sum(1 for _i in items if _i.get('ai_full'))} from the full article)")

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
