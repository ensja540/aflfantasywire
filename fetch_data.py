#!/usr/bin/env python3
"""
AFLFantasyWire — Data Fetcher
==============================
DATA SOURCES
  - Footywire       → SC/DT prices, averages, break-evens, last scores,
                      round-by-round scores, disposals, injury status
  - AFL.com.au      → Official team selections, confirmed injuries
  - SuperCoach API  → Ownership %, real-time price changes (optional)
  - AFL Fantasy API → DT-specific ownership (optional)

HOW TO RUN
  pip install requests beautifulsoup4 lxml
  python fetch_data.py

  This MUST run from a home/office machine — Footywire blocks cloud IPs.

SCHEDULE (Mac/Linux crontab)
  crontab -e
  0,15,30,45 * * * * cd /path/to/this/folder && python fetch_data.py >> fetch.log 2>&1

OUTPUT
  players.json — drop this next to aflfantasywire.html and reload the app
"""

import json, re, time, logging, sys, traceback, os, math, hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urljoin

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
    print("Run:  pip install requests beautifulsoup4 lxml")
    sys.exit(1)

# Windows consoles default to cp1252, which can't encode the ✓/→ glyphs in our
# status prints — that raises UnicodeEncodeError and aborts the run *after*
# players.json is written, so auto_scrape sees a non-zero exit. Force UTF-8.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("afw")

BASE_DIR    = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
OUTPUT_PATH = BASE_DIR / "players.json"

# ── FOOTYWIRE URLS ──────────────────────────────────────────────────────────

FW = "https://www.footywire.com/afl/footy"

URLS = {
    "sc_stats":       f"{FW}/supercoach_season",
    "sc_breakevens":  f"{FW}/supercoach_breakevens",
    "dt_stats":       f"{FW}/dream_team_season",
    "dt_breakevens":  f"{FW}/dream_team_breakevens",
    "sc_prices":      f"{FW}/supercoach_prices",
    "dt_prices":      f"{FW}/dream_team_prices",
    "injury_list":    f"{FW}/injury_list",
    "selection":      f"{FW}/afl_team_selections",
    "afl_selections": "https://www.afl.com.au/news/team-selection",
    # AFL Fantasy Classic — public JSON, gives Classic ownership %
    "afl_classic":    "https://fantasy.afl.com.au/data/afl/players.json",
}

# ── SESSION ─────────────────────────────────────────────────────────────────

def _load_env():
    """Read repo-root .env into a dict (same format tweet_bot uses)."""
    env = {}
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return env


def make_session():
    """Browser-like session that passes Footywire's bot checks.

    Since 2026-07-03 Footywire gates /afl/footy/* behind a Cloudflare
    Turnstile challenge, so a plain request gets a 503 challenge page (or a
    dropped connection). To get through: solve the challenge once in a real
    browser ON THIS MACHINE, then copy that browser's cookies for
    www.footywire.com into .env as
        FOOTYWIRE_COOKIE=JSESSIONID=...; other=...
        FOOTYWIRE_UA=<the same browser's full User-Agent string>
    The clearance is tied to the verified session, so the cookie AND the
    matching User-Agent must be sent together. Re-solve + re-paste when it
    expires (fetch_data will start failing again with 503s).
    """
    env = _load_env()
    s = requests.Session()
    cookie = env.get("FOOTYWIRE_COOKIE")
    if cookie:
        for pair in cookie.split(";"):
            if "=" in pair:
                k, v = pair.strip().split("=", 1)
                s.cookies.set(k.strip(), v.strip(), domain="www.footywire.com")
    s.headers.update({
        "User-Agent": env.get("FOOTYWIRE_UA") or (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-AU,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "DNT": "1",
    })
    return s

def get(session, url, retries=3, delay=2, timeout=15):
    """Fetch URL with retries and rate limiting."""
    for attempt in range(retries):
        try:
            time.sleep(0.8)  # polite delay between requests
            r = session.get(url, timeout=timeout)
            if r.status_code == 200:
                return r
            elif r.status_code == 403:
                log.error(f"403 Forbidden: {url}")
                log.error("  → Footywire is blocking this request.")
                log.error("  → Make sure you're running this on a home/office machine, not a server.")
                return None
            elif r.status_code == 503 and "turnstile" in (r.text or "").lower():
                log.error(f"503 challenge page: {url}")
                log.error("  → Footywire is serving its Cloudflare Turnstile CAPTCHA.")
                log.error("  → Solve it once in a browser on this machine, then put that")
                log.error("    browser's cookies + user-agent in .env as FOOTYWIRE_COOKIE /")
                log.error("    FOOTYWIRE_UA (see make_session docstring).")
                return None
            elif r.status_code == 429:
                log.warning(f"Rate limited. Waiting {delay*3}s...")
                time.sleep(delay * 3)
            else:
                log.warning(f"HTTP {r.status_code}: {url}")
        except requests.exceptions.Timeout:
            log.warning(f"Timeout on attempt {attempt+1}: {url}")
        except Exception as e:
            log.error(f"Request failed: {e}")
        if attempt < retries - 1:
            time.sleep(delay)
    return None

# ── FOOTYWIRE PARSERS ────────────────────────────────────────────────────────

def _fw_table_headers_and_rows(soup):
    """
    Footywire ranks/breakevens tables don't use <th>. Headers are in a <tr>
    of <td class="bnorm">/<td class="lbnorm"> cells directly above the first
    data row, and data rows carry id="rowpid_<numeric>". Returns
    (headers_lowercased, data_rows). Both empty if the page shape changed.
    """
    data_rows = soup.find_all("tr", id=re.compile(r"^rowpid_\d+"))
    if not data_rows:
        return [], []

    parent_table = data_rows[0].find_parent("table")
    prev = None
    if parent_table:
        for tr in parent_table.find_all("tr", recursive=False):
            if tr is data_rows[0]: break
            prev = tr
    if prev is None:
        prev = data_rows[0].find_previous_sibling("tr")

    headers = []
    if prev:
        for cell in prev.find_all(["td", "th"], recursive=False):
            txt = re.sub(r"\s+", " ", cell.get_text(" ", strip=True)).lower()
            headers.append(txt)
    return headers, data_rows


def parse_sc_stats(html):
    """
    Parse Footywire SuperCoach season rankings page.

    Actual table columns (as of 2026):
      Rank | Player | Team | Games | Price | Total Score | Average Score | *Value

    Footywire-specific markup:
      - Header cells are <td class="bnorm">/<td class="lbnorm">, NOT <th>
      - Data rows have id="rowpid_<numeric>"
      - The Player <a> href is relative WITHOUT a /afl/footy/ prefix, e.g.
        "pu-sydney-swans--brodie-grundy" — resolve with urljoin()
      - Team is a nickname ("Swans") inside an <a>
      - Status flags ("INJ", "SUS", "TBC", "EMG") appear inline after the name

    Position, Break-Even, Last Score, Ownership, and per-round columns are
    NOT on this page; they are pulled from /supercoach_breakevens and each
    player's profile page and merged in main().
    """
    soup = BeautifulSoup(html, "lxml")
    players = []

    headers, data_rows = _fw_table_headers_and_rows(soup)
    if not data_rows:
        log.warning("SC stats: no rowpid_ rows found")
        return players
    log.info(f"SC stats columns: {headers}")

    def col_idx(*names):
        for name in names:
            for i, h in enumerate(headers):
                if name in h: return i
        return None

    i_rank   = col_idx("rank")
    i_player = col_idx("player","name")
    i_team   = col_idx("team","club")
    i_games  = col_idx("games","gms","played")
    i_price  = col_idx("price","cost")
    i_total  = col_idx("total")
    i_avg    = col_idx("avg","average")

    # Fall back to the documented column order if header detection misses
    if i_rank   is None: i_rank   = 0
    if i_player is None: i_player = 1
    if i_team   is None: i_team   = 2
    if i_games  is None: i_games  = 3
    if i_price  is None: i_price  = 4
    if i_total  is None: i_total  = 5
    if i_avg    is None: i_avg    = 6

    for row in data_rows:
        cells = row.find_all(["td","th"], recursive=False)
        if len(cells) < 6: continue
        txt = [c.get_text(strip=True) for c in cells]

        def v(idx, default=""):
            return txt[idx] if idx is not None and 0 <= idx < len(txt) else default

        def num(idx, default=0):
            try: return float(re.sub(r"[^\d.\-]", "", v(idx) or "0") or 0)
            except: return default

        def money(idx, default=0):
            raw = v(idx, "0").replace("$","").replace(",","").replace(" ","")
            if raw.endswith("k"):
                try: return int(float(raw[:-1]) * 1000)
                except: return default
            try: return int(float(raw))
            except: return default

        player_cell = cells[i_player] if i_player is not None and i_player < len(cells) else cells[1]
        name_link = player_cell.find("a")
        raw_name = (name_link.get_text(strip=True) if name_link else player_cell.get_text(strip=True))
        name = re.sub(r"\s+(INJ|SUS|TBC|EMG)\s*$", "", raw_name).strip()
        if not name or name.lower() in ("player","name",""): continue

        profile_url = ""
        if name_link and name_link.get("href"):
            # Footywire serves relative hrefs without the /afl/footy/ prefix
            # (e.g. "pu-st-kilda-saints--tom-de-koning"), so resolve against
            # the page URL rather than just prepending the host.
            profile_url = urljoin("https://www.footywire.com/afl/footy/",
                                  name_link["href"])

        team_raw = ""
        if i_team is not None and i_team < len(cells):
            team_cell = cells[i_team]
            team_link = team_cell.find("a")
            team_raw = (team_link.get_text(strip=True) if team_link else team_cell.get_text(strip=True))

        rank_val = int(num(i_rank)) if i_rank is not None else (len(players) + 1)

        players.append({
            "name":        name,
            "team":        team_raw,
            "pos":         "",                # filled later from breakevens / profile
            "sc_price":    money(i_price),
            "sc_avg":      num(i_avg),
            "sc_avg3":     num(i_avg),        # placeholder; overwritten when rounds load
            "sc_last":     0,                 # filled later from per-player rounds
            "sc_be":       0,                 # filled later from breakevens
            "sc_owned":    0,                 # not exposed on this page
            "sc_games":    int(num(i_games)),
            "sc_total":    int(num(i_total)),
            "sc_scores":   [],
            "sc_all_scores": [],
            "profile_url": profile_url,
            "sc_rank":     rank_val,
        })

    log.info(f"SC stats: parsed {len(players)} players")
    return players


SC_SCORES_URL = "https://www.footywire.com/afl/footy/supercoach_scores"


def parse_sc_scores(html):
    """Parse Footywire's SuperCoach Scores page — a single page that lists, for
    EVERY player: Average, 3-Rnd Average and Consistency. We use it to fill real
    3-round form (and a consistency %) for players whose per-game log we don't
    fetch (the waiver band, rank > ~50), so the Waiver tab shows real
    DIFF/TREND/consistency instead of flat season-average placeholders.

    Columns: Player | Team | Price | G | Total | Average | 3-Rnd Average |
             $/Average | $/3-Rnd Avg | Consistency

    Returns {name_key: {"avg3": float, "cons_pct": int}}.
    """
    out = {}
    soup = BeautifulSoup(html, "lxml")
    first = soup.find("a", href=re.compile(r"pu-"))
    table = first.find_parent("table") if first else None
    if not table:
        log.warning("SC scores: data table not found")
        return out
    for row in table.find_all("tr"):
        link = row.find("a", href=re.compile(r"pu-"))
        if not link:
            continue
        cells = row.find_all(["td", "th"])
        if len(cells) < 10:
            continue
        name = link.get_text(strip=True)
        if not name:
            continue

        def num(i):
            try:
                return float(re.sub(r"[^0-9.\-]", "", cells[i].get_text(strip=True)) or 0)
            except Exception:
                return 0.0

        avg3 = num(6)
        cons = num(9)   # Footywire variability metric (~1.5-45; LOWER = steadier)
        # Map to a 0-100 consistency % where higher = more consistent.
        cons_pct = max(5, min(99, round(100 - cons * 1.8))) if cons else 0
        out[name_key(name)] = {"avg3": avg3, "cons_pct": cons_pct}
    log.info(f"SC scores: parsed {len(out)} players (3-rnd avg + consistency)")
    return out


def parse_sc_round(html):
    """Parse a Footywire supercoach_round page (every player's score for ONE
    round). Header looks like: Rank | Player | Team | ... | 2026 R11 Score | ...
    Returns (round_num, {name_key: score})."""
    out = {}
    soup = BeautifulSoup(html, "lxml")
    first = soup.find("a", href=re.compile(r"pu-"))
    table = first.find_parent("table") if first else None
    if not table:
        return 0, out
    rows = table.find_all("tr")
    rnd, score_col = 0, None
    for row in rows[:4]:
        cells = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]
        for i, t in enumerate(cells):
            m = re.search(r"R(\d+)\s+Score", t)
            if m:
                rnd, score_col = int(m.group(1)), i
                break
        if score_col is not None:
            break
    if score_col is None:
        score_col = 5
    for row in rows:
        link = row.find("a", href=re.compile(r"pu-"))
        if not link:
            continue
        cells = row.find_all(["td", "th"])
        if len(cells) <= score_col:
            continue
        name = link.get_text(strip=True)
        try:
            sco = int(re.sub(r"[^0-9\-]", "", cells[score_col].get_text(strip=True)) or 0)
        except Exception:
            sco = 0
        if name and sco > 0:
            out[name_key(name)] = sco
    return rnd, out


def parse_sc_breakevens(html):
    """
    Parse Footywire SuperCoach break-evens page.

    Actual table columns:
      Player | Team | Price | G | Avg | Breakeven | Likelihood %

    Footywire-specific markup in the Player cell:
      <td>
        <span class="hiddenspan">Reilly O'Brien</span>  ← full name (hidden, used for sort)
        <a href="pu-...">R. O'Brien</a>                 ← abbreviated visible name
        <span class="playerflag">RUC</span>             ← position (can be "RUC", "MID/FWD")
      </td>
    Position is in playerflag span — NOT in parentheses after the name like
    older Footywire pages had.

    Returns dict keyed by name_key(player) → {name, pos, team, price, games,
    avg, be, likelihood}.
    """
    soup = BeautifulSoup(html, "lxml")
    result = {}

    headers, data_rows = _fw_table_headers_and_rows(soup)
    if not data_rows:
        log.warning("SC breakevens: no rowpid_ rows found")
        return result
    log.info(f"SC breakevens columns: {headers}")

    def col_idx(*names):
        for name in names:
            for i, h in enumerate(headers):
                if name in h: return i
        return None

    i_player = col_idx("player","name")
    i_team   = col_idx("team","club")
    i_price  = col_idx("price")
    i_games  = col_idx("games","gms")
    i_avg    = col_idx("avg","average")
    # Footywire has used several spellings for the break-even header over time.
    i_be     = col_idx("breakeven","break-even","break even","be this week","b/e","b.e","break")
    i_like   = col_idx("likelihood","%")

    # "G" header is a single char that won't match a substring search
    if i_games is None:
        for i, h in enumerate(headers):
            if h.strip() == "g":
                i_games = i
                break

    # "BE"/"B/E" can also be a short standalone header
    if i_be is None:
        for i, h in enumerate(headers):
            if h.strip() in ("be", "b/e", "be.", "b.e", "b.e."):
                i_be = i
                break

    # Fall back to documented column order if headers couldn't be detected
    if i_player is None: i_player = 0
    if i_team   is None: i_team   = 1
    if i_price  is None: i_price  = 2
    if i_games  is None: i_games  = 3
    if i_avg    is None: i_avg    = 4
    if i_be     is None: i_be     = 5
    if i_like   is None: i_like   = 6

    for row in data_rows:
        cells = row.find_all(["td","th"], recursive=False)
        if len(cells) < 5: continue
        txt = [c.get_text(strip=True) for c in cells]

        def v(idx, default=""):
            return txt[idx] if idx is not None and 0 <= idx < len(txt) else default
        def num(idx, default=0):
            try: return float(re.sub(r"[^\d.\-]", "", v(idx) or "0") or 0)
            except: return default
        def money(idx, default=0):
            raw = v(idx, "0").replace("$","").replace(",","")
            try: return int(float(raw))
            except: return default

        player_cell = cells[i_player] if 0 <= i_player < len(cells) else cells[0]

        # Prefer the hiddenspan (full name); fall back to the visible <a> text
        hidden = player_cell.find("span", class_="hiddenspan")
        anchor = player_cell.find("a")
        name = ""
        if hidden:
            name = hidden.get_text(strip=True)
        if not name and anchor:
            name = anchor.get_text(strip=True)
        if not name:
            name = player_cell.get_text(" ", strip=True)
        name = re.sub(r"\s+(INJ|SUS|TBC|EMG)\s*$", "", name).strip()
        if not name or name.lower() in ("player","name",""): continue

        flag = player_cell.find("span", class_="playerflag")
        pos = ""
        positions = []
        if flag:
            pos_raw = flag.get_text(strip=True).upper()
            # Keep ALL listed positions for dual-position players (e.g. MID/FWD);
            # `pos` stays the primary (first) for backward compatibility.
            positions = normalise_pos_list(pos_raw)
            # Primary position = first canonical code (handles "MID, FOR" etc.);
            # fall back to the raw token only if nothing normalised.
            pos = positions[0] if positions else re.split(r"[/,]", pos_raw)[0].strip()

        team_cell = cells[i_team] if 0 <= i_team < len(cells) else None
        team_raw = ""
        if team_cell:
            team_link = team_cell.find("a")
            team_raw = (team_link.get_text(strip=True) if team_link else team_cell.get_text(strip=True))

        result[name_key(name)] = {
            "name":       name,
            "pos":        pos,
            "positions":  positions,
            "team":       team_raw,
            "price":      money(i_price),
            "games":      int(num(i_games)),
            "avg":        num(i_avg),
            "be":         int(num(i_be)),
            "likelihood": num(i_like),
        }

    log.info(f"SC breakevens: parsed {len(result)} players")
    return result


def parse_player_games(html):
    """
    Parse a Footywire player games log (/afl/footy/pg-{team}--{player}) for the
    richer per-game stats line:
      Round | Date | Opponent | Result | K | HB | D | M | G | B | T | HO | GA | I50 | CL | CG | R50 | FF | FA | AF | SC
    where AF = AFL Fantasy score and SC = SuperCoach score.

    Headers are <td class="bnorm">, data rows class="darkcolor"/"lightcolor".
    Trailing None SC entries (the in-progress round) are trimmed so they don't
    skew last-score, 3-round average, or Top Improvers.

    Returns: {pos, sc_rounds, sc_scores, af_scores, disposals, marks, goals,
              tackles, hitouts, clearances} — parallel lists keyed on sc_rounds.
    """
    soup = BeautifulSoup(html, "lxml")
    result = {
        "pos": "",
        "sc_rounds":  [], "sc_scores":  [], "af_scores":  [], "opponents": [],
        "disposals":  [], "marks":      [], "goals":      [], "behinds": [], "kicks": [], "handballs": [],
        "tackles":    [], "hitouts":    [], "clearances": [],
    }

    page_text = soup.get_text(" ", strip=True)
    m = re.search(
        r"Position:\s*([A-Za-z][A-Za-z /]*?)(?=\s+(?:Born|Height|Weight|DOB|Origin|Drafted|Recruited|Club|Games|Career|Debut|$))",
        page_text,
    )
    if m:
        pos_raw = m.group(1).strip().upper()
        result["pos"] = {
            "RUCK":"RUC","RUCK ROVER":"MID","MIDFIELD":"MID","MIDFIELDER":"MID",
            "DEFENDER":"DEF","DEFENCE":"DEF","BACK":"DEF","BACKMAN":"DEF",
            "FORWARD":"FWD","FORWARDS":"FWD","ATTACK":"FWD",
        }.get(pos_raw, pos_raw[:3])

    for table in soup.find_all("table"):
        rows = table.find_all("tr", recursive=False)
        if not rows: continue
        header_cells = [c.get_text(strip=True).lower()
                        for c in rows[0].find_all(["td","th"], recursive=False)]
        # The games-log table's first header is "Description" (cell holds
        # text like "Round 10") and it has a trailing "sc" column.
        if "sc" not in header_cells: continue
        if "description" not in header_cells and "round" not in header_cells:
            continue

        def ci(name):
            try: return header_cells.index(name)
            except ValueError: return None

        # Round number is in the "Description" column ("Round 10") or a
        # dedicated "Round" column on older seasons
        i_round = ci("description")
        if i_round is None: i_round = ci("round")
        i_opp   = ci("opponent")
        i_sc    = ci("sc")
        i_af    = ci("af")
        i_k     = ci("k")
        i_hb    = ci("hb")
        i_d     = ci("d")
        i_m     = ci("m")
        i_g     = ci("g")
        i_b     = ci("b")
        i_t     = ci("t")
        i_ho    = ci("ho")
        i_cl    = ci("cl")

        for tr in rows[1:]:
            cells = [c.get_text(strip=True)
                     for c in tr.find_all(["td","th"], recursive=False)]
            if i_sc is None or len(cells) <= i_sc: continue

            try: rnd = int(re.sub(r"[^\d\-]", "", cells[i_round]) or "-99")
            except: continue
            # Keep Round 0 (Opening Round) for raw-stat calcs; it's excluded from
            # FANTASY scoring downstream (it wasn't a fantasy round).
            if rnd < 0 or rnd > 30: continue

            def parse_int(idx):
                if idx is None or idx >= len(cells): return None
                raw = cells[idx]
                if raw in ("", "-", "DNP", "BYE", "—"): return None
                try: return int(re.sub(r"[^\d\-]", "", raw) or 0)
                except: return None

            opp_raw = cells[i_opp] if (i_opp is not None and i_opp < len(cells)) else ""
            result["opponents"].append(normalise_team(re.sub(r"\s*\(.*?\)\s*$", "", opp_raw).strip()) or None)
            result["sc_rounds"].append(rnd)
            result["sc_scores"].append(parse_int(i_sc))
            result["af_scores"].append(parse_int(i_af))
            result["kicks"].append(parse_int(i_k))
            result["handballs"].append(parse_int(i_hb))
            result["disposals"].append(parse_int(i_d))
            result["marks"].append(parse_int(i_m))
            result["goals"].append(parse_int(i_g))
            result["behinds"].append(parse_int(i_b))
            result["tackles"].append(parse_int(i_t))
            result["hitouts"].append(parse_int(i_ho))
            result["clearances"].append(parse_int(i_cl))

        if result["sc_scores"]: break

    # Footywire lists most-recent first. Reverse every parallel list so the
    # latest round sits at the end, matching the rest of the pipeline (which
    # treats [-1] as "most recent").
    parallel = ("sc_rounds","sc_scores","af_scores","opponents","disposals","marks",
                "goals","behinds","kicks","handballs","tackles","hitouts","clearances")
    for k in parallel:
        result[k].reverse()

    # Drop trailing None SC rounds — the most recent listed round is usually
    # the in-progress one the player hasn't played yet.
    while result["sc_scores"] and result["sc_scores"][-1] is None:
        for k in parallel:
            if result[k]: result[k].pop()

    return result


AFL_API_SEASON_ID = 85  # 2026 AFL Premiership season (aflapi.afl.com.au)

# Canonical-team -> 3-letter code for compact fixture labels on the schedule chart.
_TEAM_ABBR = {
    "Adelaide":"ADE","Brisbane":"BRL","Carlton":"CAR","Collingwood":"COL",
    "Essendon":"ESS","Fremantle":"FRE","Geelong":"GEE","Gold Coast":"GCS",
    "GWS Giants":"GWS","Hawthorn":"HAW","Melbourne":"MEL","North Melbourne":"NTH",
    "Port Adelaide":"PTA","Richmond":"RIC","St Kilda":"STK","Sydney":"SYD",
    "West Coast":"WCE","Western Bulldogs":"WBD",
}

# ── Head-coach changes ────────────────────────────────────────────────────
# A defence's concession patterns belong to its coach, so matchup data from a
# different coach is misleading. For each team (as the OPPONENT conceding), its
# matchup data is valid only from this 2026 round onward; earlier 2026 games AND
# all of 2025 are under the old coach and disregarded for that team.
COACH_CHANGE_2026_ROUND = {"Melbourne": 0, "Carlton": 10, "Essendon": 10}
# 2025 history is disregarded for ANY team whose coach has changed since then.
COACH_CHANGED_TEAMS = set(COACH_CHANGE_2026_ROUND)


def _coach_valid_2026(opp, rnd):
    """True if a 2026 round's conceded data reflects the team's CURRENT coach."""
    r = COACH_CHANGE_2026_ROUND.get(opp)
    if r is None:
        return True
    try:
        return int(rnd) >= r
    except (TypeError, ValueError):
        return True


HIST_LOG_TIME_LIMIT = 540  # seconds budget for the 2025 history pass
_DVP_2025 = {}  # opp -> pos -> stat -> [values]; last-season position-vs-team

# Last season's per-position matchup data never changes, so we scrape it once
# and store the aggregate permanently in the repo ("our gold"). Every run loads
# this file instead of re-fetching ~380 Footywire pages; it is only rebuilt when
# explicitly requested with `--historical` (or `--full`). This is the single
# biggest fetch_data time saver and is what kept the auto run under the 20-min
# subprocess cap. The cache stores raw value lists keyed by the coach-change set
# (so it auto-rebuilds if that set changes).
DVP_2025_CACHE_PATH = BASE_DIR / "dvp_2025_cache.json"
WITH_HISTORY = ("--historical" in sys.argv) or ("--full" in sys.argv)


def load_dvp_2025_cache():
    """Populate _DVP_2025 from the on-disk gold cache. Returns True on success."""
    try:
        c = json.loads(DVP_2025_CACHE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        log.info("2025 history: no cache yet — run `fetch_data.py --historical` to build it")
        return False
    except Exception as _e:
        log.warning(f"2025 DvP cache read failed: {_e}")
        return False
    if c.get("data"):
        _DVP_2025.clear()
        _DVP_2025.update(c["data"])
        log.info(f"2025 history: loaded {len(_DVP_2025)} teams from cache "
                 f"(built {c.get('built_at', '?')})")
        return True
    return False


def save_dvp_2025_cache():
    """Persist _DVP_2025 to the gold cache file (atomic write)."""
    try:
        payload = {
            "built_at": datetime.now().isoformat(timespec="seconds"),
            "sig": ",".join(sorted(COACH_CHANGED_TEAMS)),
            "data": _DVP_2025,
        }
        tmp = str(DVP_2025_CACHE_PATH) + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, str(DVP_2025_CACHE_PATH))
        log.info(f"2025 DvP cached -> {DVP_2025_CACHE_PATH.name} ({len(_DVP_2025)} teams)")
    except Exception as _e:
        log.warning(f"2025 DvP cache write failed: {_e}")


def _parse_year_games(html):
    """Parse a Footywire historical (?year=YYYY) games-log table. Its layout is
    'Description Date Opponent Result K HB D M G B T HO ...' (no AF/SC columns),
    different from the current-season page, so it needs its own column-mapped
    parser. Returns parallel lists of opponents + raw stats."""
    soup = BeautifulSoup(html, "lxml")
    out = {"opponents": [], "disposals": [], "kicks": [], "handballs": [],
           "marks": [], "tackles": [], "goals": [], "behinds": []}
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        hdr = [c.get_text(strip=True).lower() for c in rows[0].find_all(["td", "th"])]
        if "opponent" not in hdr or "d" not in hdr or "k" not in hdr:
            continue
        idx = {h: i for i, h in enumerate(hdr)}
        ci_opp = idx["opponent"]

        def _num(cells, key):
            i = idx.get(key, -1)
            if i < 0 or i >= len(cells):
                return None
            t = cells[i].get_text(strip=True)
            try:
                return float(t)
            except ValueError:
                return None
        for tr in rows[1:]:
            cells = tr.find_all(["td", "th"])
            if len(cells) <= ci_opp:
                continue
            opp = normalise_team(re.sub(r"^(vs|@)\s*", "", cells[ci_opp].get_text(" ", strip=True), flags=re.I))
            if not opp or opp == "Unknown":
                continue
            out["opponents"].append(opp)
            out["disposals"].append(_num(cells, "d"))
            out["kicks"].append(_num(cells, "k"))
            out["handballs"].append(_num(cells, "hb"))
            out["marks"].append(_num(cells, "m"))
            out["tackles"].append(_num(cells, "t"))
            out["goals"].append(_num(cells, "g"))
            out["behinds"].append(_num(cells, "b"))
        break
    return out


def fetch_dvp_2025(session, sc_players, limit=380):
    """Fetch 2025 game logs (Footywire pg-...?year=2025) and aggregate a
    position-vs-team matrix for last season. Teams whose coach has changed since
    2025 (COACH_CHANGED_TEAMS) are skipped — their old-coach data misleads."""
    import time as _time
    _DVP_2025.clear()
    start = _time.time()
    skey = [("disposals", "disposals"), ("kicks", "kicks"), ("handballs", "handballs"),
            ("marks", "marks"), ("tackles", "tackles"), ("goals", "goals")]
    done = 0
    for p in sc_players[:limit]:
        if _time.time() - start > HIST_LOG_TIME_LIMIT:
            log.warning("2025 history pass hit time budget")
            break
        pu = p.get("profile_url", "")
        if not pu:
            continue
        pg = pu.replace("/pu-", "/pg-")
        sep = "&" if "?" in pg else "?"
        r = get(session, pg + sep + "year=2025", retries=1, timeout=8)
        if not r:
            continue
        gh = _parse_year_games(r.text)
        pos = (p.get("pos") or "MID").upper()
        if pos not in ("DEF", "MID", "RUC", "FWD"):
            pos = "MID"
        opps = gh.get("opponents") or []
        if not opps:
            continue
        for idx in range(len(opps)):
            opp = opps[idx]
            if not opp or opp in COACH_CHANGED_TEAMS:
                continue
            d = _DVP_2025.setdefault(opp, {}).setdefault(pos, {})
            for k, full in skey:
                arr = gh.get(k) or []
                v = arr[idx] if idx < len(arr) and arr[idx] is not None else None
                if v is not None:
                    d.setdefault(full, []).append(v)
        done += 1
    log.info(f"2025 history: {done} players -> DvP across {len(_DVP_2025)} teams")
    save_dvp_2025_cache()

def fetch_upcoming_fixture(session, team_last, n=5, season_id=AFL_API_SEASON_ID):
    """Per-team upcoming opponents. ``team_last`` maps our_team_name -> the last
    round that team has PLAYED, so EACH team's opponent list starts at its own
    next unplayed round. A team mid-round therefore still shows its CURRENT-round
    game, not next round (the bug where everyone resolved to max+1). Opponent
    names run through normalise_team to key-match the points-conceded table.
    Returns {team: [opp, ...]} plus _next (team -> round of its next game) and
    _r1 (teams in the earliest upcoming round; teams absent have a bye)."""
    base = min(team_last.values()) if team_last else 1
    out, nextr, by_round = {}, {}, {}
    for rnd in range(base + 1, base + n + 3):   # headroom so byes still fill n games
        r = get(session,
                f"https://aflapi.afl.com.au/afl/v2/matches?compSeasonId={season_id}"
                f"&roundNumber={rnd}&pageSize=20", retries=1, timeout=10)
        if not r:
            continue
        try:
            matches = r.json().get("matches", []) or []
        except Exception:
            continue
        for m in matches:
            h = normalise_team(((m.get("home") or {}).get("team") or {}).get("name") or "")
            a = normalise_team(((m.get("away") or {}).get("team") or {}).get("name") or "")
            if not (h and a and h != "Unknown" and a != "Unknown"):
                continue
            by_round.setdefault(rnd, set()).update((h, a))
            for t, opp in ((h, a), (a, h)):
                if rnd > team_last.get(t, base) and len(out.get(t, [])) < n:
                    out.setdefault(t, []).append(opp)
                    nextr.setdefault(t, rnd)
    # _r1 = every team in the in-progress round's FULL fixture (the lowest next
    # round), so teams that have already played it aren't mistaken for byes.
    in_prog = min(nextr.values()) if nextr else base + 1
    out["_next"] = nextr
    out["_r1"] = by_round.get(in_prog, set())
    return out


def fetch_current_round_fixture(session, start_round, season_id=AFL_API_SEASON_ID):
    """The round currently being PLAYED and its full fixture, for a stable
    'this week's matchups' box. Returns {"round": R, "matchups": [[homeAbbr,
    awayAbbr], ...]} for the lowest round (scanning from start_round) that still
    has an unconcluded match — so the box holds on the round until EVERY game in
    it is final, instead of rolling forward team-by-team as games finish."""
    for rnd in range(max(1, start_round), start_round + 4):
        r = get(session,
                f"https://aflapi.afl.com.au/afl/v2/matches?compSeasonId={season_id}"
                f"&roundNumber={rnd}&pageSize=20", retries=1, timeout=10)
        if not r:
            continue
        try:
            matches = r.json().get("matches", []) or []
        except Exception:
            continue
        if not matches:
            continue
        from datetime import timezone
        _now = datetime.now(timezone.utc)
        fixture, started, any_open = [], [], False
        for m in matches:
            h = normalise_team(((m.get("home") or {}).get("team") or {}).get("name") or "")
            a = normalise_team(((m.get("away") or {}).get("team") or {}).get("name") or "")
            # Has this game kicked off? (start time passed, or status moved on)
            _ko = False
            try:
                _st = (m.get("utcStartTime") or "").replace("Z", "+00:00")
                _ko = bool(_st) and _now >= datetime.fromisoformat(_st)
            except Exception:
                _ko = (m.get("status") or "") not in ("SCHEDULED", "", "UPCOMING")
            if h and a and h != "Unknown" and a != "Unknown":
                fixture.append([_TEAM_ABBR.get(h, h[:3].upper()),
                                _TEAM_ABBR.get(a, a[:3].upper())])
                if _ko:
                    started += [h, a]
            if m.get("status") != "CONCLUDED":
                any_open = True
        if fixture and any_open:
            return {"round": rnd, "matchups": fixture, "started": started}
    return None


def fetch_recent_form(session, cur_round, n=5, season_id=AFL_API_SEASON_ID):
    """Win rate (0-1) over the last ``n`` completed rounds, keyed by our team
    name, from the AFL matches API (CONCLUDED matches with totalScore). Draws
    count as half a win. Returns {} on failure."""
    wins, games = {}, {}
    for k in range(0, n):
        rnd = cur_round - k
        if rnd < 1:
            break
        r = get(session,
                f"https://aflapi.afl.com.au/afl/v2/matches?compSeasonId={season_id}"
                f"&roundNumber={rnd}&pageSize=20", retries=1, timeout=10)
        if not r:
            continue
        try:
            matches = r.json().get("matches", []) or []
        except Exception:
            continue
        for m in matches:
            if m.get("status") != "CONCLUDED":
                continue
            h, a = m.get("home") or {}, m.get("away") or {}
            ht = normalise_team((h.get("team") or {}).get("name") or "")
            at = normalise_team((a.get("team") or {}).get("name") or "")
            hs = (h.get("score") or {}).get("totalScore")
            as_ = (a.get("score") or {}).get("totalScore")
            if ht == "Unknown" or at == "Unknown" or hs is None or as_ is None:
                continue
            games[ht] = games.get(ht, 0) + 1
            games[at] = games.get(at, 0) + 1
            if hs > as_:
                wins[ht] = wins.get(ht, 0) + 1
            elif as_ > hs:
                wins[at] = wins.get(at, 0) + 1
            else:
                wins[ht] = wins.get(ht, 0) + 0.5
                wins[at] = wins.get(at, 0) + 0.5
    return {t: wins.get(t, 0) / g for t, g in games.items() if g}


def fetch_points_conceded(session, cur_round, season_id=AFL_API_SEASON_ID):
    """Average AFL match points conceded per game, keyed by our team name, over
    every CONCLUDED match this season. A leaky defence (concedes more than the
    league average) signals more scoring for the opposition's goal-kickers."""
    conc, games = {}, {}
    for rnd in range(1, cur_round + 1):
        r = get(session,
                f"https://aflapi.afl.com.au/afl/v2/matches?compSeasonId={season_id}"
                f"&roundNumber={rnd}&pageSize=20", retries=1, timeout=10)
        if not r:
            continue
        try:
            matches = r.json().get("matches", []) or []
        except Exception:
            continue
        for m in matches:
            if m.get("status") != "CONCLUDED":
                continue
            h, a = m.get("home") or {}, m.get("away") or {}
            ht = normalise_team((h.get("team") or {}).get("name") or "")
            at = normalise_team((a.get("team") or {}).get("name") or "")
            hs = (h.get("score") or {}).get("totalScore")
            as_ = (a.get("score") or {}).get("totalScore")
            if ht == "Unknown" or at == "Unknown" or hs is None or as_ is None:
                continue
            conc[ht] = conc.get(ht, 0) + as_   # home concedes the away score
            conc[at] = conc.get(at, 0) + hs
            games[ht] = games.get(ht, 0) + 1
            games[at] = games.get(at, 0) + 1
    return {t: conc[t] / g for t, g in games.items() if g}


PREDICTIONS_LOG = BASE_DIR / "predictions_log.json"
# Self-calibration guardrails: feed each completed round's predicted-vs-actual
# error back into the next round's predictions. We require a healthy SAMPLE
# count (CAL_MIN_SAMPLES) and clamp the learned factor (CAL_CLAMP) so it only
# ever nudges, never takes over — those two together stop a single odd round
# (e.g. a wet one) from skewing the model, so we only need ONE graded round to
# start correcting (CAL_MIN_ROUNDS = 1). The factor keeps accumulating across
# every graded round, so the correction sharpens as more rounds land.
CAL_MIN_SAMPLES = 80
CAL_MIN_ROUNDS = 1
CAL_CLAMP = (0.85, 1.15)
# Current-round prediction accuracy ("win %") for the predict tab, set by
# log_predictions and embedded in players.json by write_output.
_ROUND_ACCURACY = None
# Stable "this week's matchups" (the in-progress round's full fixture), so the
# box doesn't roll forward team-by-team as games finish.
_THIS_WEEK_MATCHUPS = None
# Predictions lock at kick-off: the round being predicted and the set of teams
# whose game has already started (their logged prediction is frozen).
_LOCK_ROUND = None
_LOCK_TEAMS = set()

# How many standard deviations below the prediction the low range sits. Smaller
# = a tighter band (a full sigma was too wide; 0.5 still a touch loose, so 0.4).
LOW_RANGE_K = 0.4


def _sigma(vals):
    """Population standard deviation of the values (0 if fewer than 2)."""
    vals = [v for v in vals if v is not None]
    n = len(vals)
    if n < 2:
        return 0.0
    m = sum(vals) / n
    return (sum((v - m) ** 2 for v in vals) / n) ** 0.5


def log_predictions(players, cur_round):
    """Log this round's RAW (pre-calibration) per-stat predictions and score them
    against prior logged rounds now completed (actuals live in each player's
    roundStats). Builds a self-calibrating per-stat correction: the accumulated
    actual/predicted ratio feeds back as a clamped multiplier so the model's
    systematic bias shrinks over time. Returns {stat: factor} to apply downstream.

    Carried-forward players (partial-scrape persistence) are excluded — their
    statPred is a stale, already-calibrated value, not a fresh raw prediction."""
    try:
        plog = json.loads(PREDICTIONS_LOG.read_text(encoding="utf-8"))
    except Exception:
        plog = {"rounds": {}, "accuracy": {}, "calibration": {}}
    # Log each player under THEIR OWN next round (a team mid-round is still
    # predicting its current-round game while teams that have played it predict
    # next round). Lock at kick-off: once a player's next game has started, the
    # prediction we already logged is frozen so it can't drift; teams yet to play
    # keep updating.
    _rounds = plog.setdefault("rounds", {})
    _locked = 0
    for p in players:
        if not (p.get("statPred") and p.get("name") and not p.get("_carried")):
            continue
        nr = p.get("nextRound")
        if not nr:
            continue
        nm = p["name"]
        bucket = _rounds.setdefault(str(nr), {})
        if nr == _LOCK_ROUND and p.get("team") in _LOCK_TEAMS and nm in bucket:
            _locked += 1            # kicked off -> keep the locked prediction
            continue
        bucket[nm] = p["statPred"]
    if _locked:
        log.info(f"Prediction lock: {_locked} player(s) frozen at kick-off")
    by_name = {p.get("name"): p for p in players}
    _SK = (("disposals", "dis"), ("kicks", "k"), ("handballs", "hb"),
           ("marks", "mk"), ("tackles", "tk"), ("goals", "gl"))
    agg = {}   # stat -> {pred,act,abserr,signed,n, rounds:set}
    for rnd_str, preds in list(plog.get("rounds", {}).items()):
        try:
            rnd = int(rnd_str)
        except Exception:
            continue
        if rnd > cur_round:          # not played yet
            continue
        for name, sp in preds.items():
            p = by_name.get(name)
            if not p:
                continue
            rs = next((r for r in (p.get("roundStats") or []) if r.get("r") == rnd), None)
            if not rs:
                continue
            for sk, rk in _SK:
                pred, act = sp.get(sk), rs.get(rk)
                if pred is None or act is None:
                    continue
                a = agg.setdefault(sk, {"pred": 0.0, "act": 0.0, "abserr": 0.0,
                                        "signed": 0.0, "n": 0, "rounds": set()})
                a["pred"] += pred; a["act"] += act
                a["abserr"] += abs(pred - act); a["signed"] += (pred - act)
                a["n"] += 1; a["rounds"].add(rnd)
    plog["accuracy"] = {sk: {"mae": round(a["abserr"] / a["n"], 2),
                             "bias": round(a["signed"] / a["n"], 2),
                             "n": a["n"], "rounds": len(a["rounds"])}
                        for sk, a in agg.items() if a["n"]}
    # Learned per-stat correction = actual/predicted, gated + clamped.
    cal = {}
    for sk, a in agg.items():
        if a["n"] >= CAL_MIN_SAMPLES and len(a["rounds"]) >= CAL_MIN_ROUNDS and a["pred"] > 0:
            f = a["act"] / a["pred"]
            cal[sk] = round(max(CAL_CLAMP[0], min(CAL_CLAMP[1], f)), 3)
    plog["calibration"] = cal
    # Current-round "win %": grade THIS round's predictions against actuals as
    # games close. Updates each scrape, so it climbs/settles game by game.
    global _ROUND_ACCURACY
    cur_preds = plog.get("rounds", {}).get(str(cur_round)) or {}
    _hits = _tot = _pl = 0
    _teams = set()
    # Grade EVERY player who played this round — use the locked logged prediction
    # if we have one, else fall back to their current statPred so late inclusions
    # / unpredicted players still get a shaded result instead of a blank cell.
    for p in players:
        name = p.get("name")
        rs = next((r for r in (p.get("roundStats") or []) if r.get("r") == cur_round), None)
        if not rs:
            continue
        sp = cur_preds.get(name) or p.get("statPred")
        if not sp:
            continue
        _pl += 1
        if p.get("team"):
            _teams.add(p["team"])
        # Per-player LOCKED result: the prediction we made for this round vs the
        # actual, with a per-stat win flag — drives green/red shading + locks the
        # row once the player's game is final.
        _res = {}
        for sk, rk in _SK:
            pred, act = sp.get(sk), rs.get(rk)
            if pred is None or act is None:
                continue
            _tot += 1
            # Deviation band: pick a number (rounded prediction) and a low range
            # one standard deviation below it (from the player's game-to-game
            # spread for this stat). actual >= number -> strong green (beat it);
            # in [low, number) -> light green (held the range); below low -> red.
            # Win% counts beating the number.
            _num = round(pred)
            _vals = [r.get(rk) for r in (p.get("roundStats") or []) if r.get(rk) is not None]
            _dev = LOW_RANGE_K * _sigma(_vals) if len(_vals) >= 3 else max(0.8, pred * 0.15)
            _low = max(0, min(int(math.floor(pred - _dev)), _num - 1))
            if act >= _num:
                _tier = 2
                _hits += 1
            elif act >= _low:
                _tier = 1
            else:
                _tier = 0
            _res[sk] = {"p": _num, "low": _low, "a": act, "tier": _tier}
        if _res:
            p["roundResult"] = {"round": cur_round, "opp": rs.get("opp"), "stats": _res}
    # Win = actual met or beat the predicted number; ~50% for an unbiased model.
    _ROUND_ACCURACY = ({"round": cur_round, "winPct": round(100 * _hits / _tot),
                        "target": 50, "predictions": _tot, "playersGraded": _pl,
                        "gamesIn": len(_teams) // 2} if _tot else None)
    plog["current_round"] = _ROUND_ACCURACY
    PREDICTIONS_LOG.write_text(json.dumps(plog, indent=2), encoding="utf-8")
    log.info(f"Predictions logged (per-team next round); accuracy: {plog['accuracy']}; "
             f"calibration: {cal or '(building history)'}; current-round: {_ROUND_ACCURACY}")
    return cal


def fetch_team_rounds_played(session, cur_round, season_id=AFL_API_SEASON_ID):
    """Count CONCLUDED matches per team across rounds 1..cur_round (Round 0
    excluded), i.e. rounds the team has actually played so far with byes
    excluded. {team: count}. The availability denominator."""
    cnt = {}
    for rnd in range(1, cur_round + 1):
        r = get(session,
                f"https://aflapi.afl.com.au/afl/v2/matches?compSeasonId={season_id}"
                f"&roundNumber={rnd}&pageSize=20", retries=1, timeout=10)
        if not r:
            continue
        try:
            matches = r.json().get("matches", []) or []
        except Exception:
            continue
        for m in matches:
            if m.get("status") != "CONCLUDED":
                continue
            for side in ("home", "away"):
                t = normalise_team(((m.get(side) or {}).get("team") or {}).get("name") or "")
                if t and t != "Unknown":
                    cnt[t] = cnt.get(t, 0) + 1
    return cnt


def fetch_classic_ownership(session):
    """
    Fetch AFL Fantasy Classic player data from the public JSON endpoint.

    Public payload (no auth) includes per-player Classic ownership and projections:
      {
        "first_name", "last_name", "cost", "positions": [int],
        "stats": {
          "owned_by": 53.73,          ← Classic ownership %
          "avg_points": 108.1,
          "last_3_avg": 99,
          "proj_avg": 112.04,
          ...
        }
      }

    Returns dict keyed by name_key(first + last):
      {classic_owned, classic_avg, classic_avg3, classic_proj, classic_price}
    """
    result = {}
    r = get(session, URLS["afl_classic"])
    if not r:
        log.warning("AFL Fantasy Classic: fetch failed")
        return result

    try:
        data = r.json()
    except Exception as e:
        log.warning(f"AFL Fantasy Classic: JSON parse failed: {e}")
        return result

    # The payload is either a bare list or wrapped in a dict — handle both
    players = data if isinstance(data, list) else data.get("players", data)
    if not isinstance(players, list):
        log.warning(f"AFL Fantasy Classic: unexpected payload shape ({type(data).__name__})")
        return result

    # AFL Classic squad_id -> our team name. Derived empirically (not strictly
    # alphabetical because Gold Coast / GWS get IDs in the 1000-range).
    SQUAD_TO_TEAM = {
        10:  "Adelaide",         20:  "Brisbane",
        30:  "Carlton",          40:  "Collingwood",
        50:  "Essendon",         60:  "Fremantle",
        70:  "Geelong",          80:  "Hawthorn",
        90:  "Melbourne",        100: "North Melbourne",
        110: "Port Adelaide",    120: "Richmond",
        130: "St Kilda",         140: "Western Bulldogs",
        150: "West Coast",       160: "Sydney",
        1000: "Gold Coast",      1010: "GWS Giants",
    }

    for p in players:
        first = (p.get("first_name") or "").strip()
        last  = (p.get("last_name")  or "").strip()
        if not first and not last: continue
        name  = f"{first} {last}".strip()
        nk    = name_key(name)
        # Triple key: (first word of first_name, last_name, team). The first
        # word matters because Classic stores e.g. "Bailey J." (West Coast)
        # vs "Bailey" (Bulldogs) — same first word, distinct teams; and ALSO
        # "Jack Williams" also lives on West Coast, so a (last, team) key
        # would silently overwrite Bailey J. with Jack.
        _first_word = first.split()[0] if first.split() else ""
        nk_first_last_team = (name_key(_first_word), name_key(last),
                              SQUAD_TO_TEAM.get(p.get("squad_id")))
        stats = p.get("stats") or {}

        # AFL Classic encodes positions as ints: 1=DEF, 2=MID, 3=RUC, 4=FWD.
        # Convert to our codes here so downstream consumers don't repeat the
        # mapping. A player listed as [2, 4] is dual MID/FWD and SHOULD appear
        # under both position filters on the site.
        _POS_INT = {1: "DEF", 2: "MID", 3: "RUC", 4: "FWD"}
        raw_positions = p.get("positions") or []
        positions = [_POS_INT[int(x)] for x in raw_positions
                     if isinstance(x, (int, float)) and int(x) in _POS_INT]

        entry = {
            "classic_owned": float(stats.get("owned_by") or 0),
            "classic_avg":   float(stats.get("avg_points") or 0),
            "classic_avg3":  float(stats.get("last_3_avg") or 0),
            "classic_proj":  float(stats.get("proj_avg") or 0),
            "classic_price": int(p.get("cost") or 0),
            "classic_positions": positions,
            "_squad_team":   SQUAD_TO_TEAM.get(p.get("squad_id")),
        }
        # Strict key: first+last+nothing-else. ALWAYS use this if both sides
        # have matching first names (no middle initials).
        result[nk] = entry
        # Triple key: (first_word, last_name, team). Handles two collisions
        # at once: Bailey J. vs Bailey (different teams) AND Jack Williams
        # vs Bailey J. Williams on the SAME team. A bare (last_name, team)
        # tuple would silently overwrite same-team same-surname siblings.
        if nk_first_last_team[2]:
            result[nk_first_last_team] = entry

    log.info(f"AFL Fantasy Classic: parsed {len(result)} players (ownership)")
    return result


def parse_dt_stats(html):
    """Parse the AFL Fantasy (Dream Team) season rankings page.

    The rankings table has a <td> header row (not <th>):
      Rank | Player | Team | Games | Price | TotalScore | AverageScore | *Value
    so we locate it by header content and read column names from the first row.
    """
    soup = BeautifulSoup(html, "lxml")
    players = []

    def header_cells(table):
        first = table.find("tr")
        return [c.get_text(strip=True).lower() for c in first.find_all(["td", "th"])] if first else []

    # The real rankings table has DISTINCT short header cells ("player",
    # "price"); wrapper/control tables only have those words inside blob cells.
    table = None
    for t in soup.find_all("table"):
        h = header_cells(t)
        if "player" in h and any(c == "price" for c in h) and len(h) >= 6 and len(t.find_all("tr")) > 20:
            table = t
            break
    if not table:
        log.warning("DT stats: no rankings table found")
        return players

    headers = header_cells(table)
    log.info(f"DT stats headers: {headers}")

    def col_idx(*names):
        for name in names:
            for i, h in enumerate(headers):
                if name in h:
                    return i
        return None

    i_player = col_idx("player", "name")
    i_price  = col_idx("price", "cost")
    i_avg    = col_idx("averagescore", "average", "avg")
    i_total  = col_idx("totalscore", "total")

    for row in table.find_all("tr")[1:]:
        cells = row.find_all(["td", "th"])
        if len(cells) < 5:
            continue
        txt = [c.get_text(strip=True) for c in cells]
        def v(i, d=""):
            return txt[i] if i is not None and i < len(txt) else d
        def num(i, d=0):
            try: return float(re.sub(r"[^\d.]", "", v(i) or "0") or 0)
            except Exception: return d
        def money(i, d=0):
            raw = v(i, "0").replace("$", "").replace(",", "")
            try: return int(float(raw))
            except Exception: return d

        player_cell = cells[i_player] if i_player is not None else cells[1]
        name = (player_cell.find("a") or player_cell).get_text(strip=True)
        # Footywire suffixes a status flag onto the name ("Errol GuldenINJ").
        name = re.sub(r"\s*(INJ|SUSP|TEST|LATE|OUT|DLIST)\s*$", "", name).strip()
        if not name or name.lower() in ("player", "name", ""):
            continue

        avg = num(i_avg)
        players.append({
            "name":     name,
            "dt_price": money(i_price) or 500000,
            "dt_avg":   avg,
            "dt_avg3":  avg,
            "dt_last":  0,
            "dt_be":    0,
            "dt_owned": 0,
            "dt_scores": [],
            "dt_rank":  len(players) + 1,
        })

    log.info(f"DT stats: parsed {len(players)} players")
    return players


def parse_player_detail(html, player_name):
    """
    Parse individual player page for detailed season stats:
    disposals, clearances, tackles, goals, marks per game.
    """
    soup = BeautifulSoup(html, "lxml")
    stats = {"disposals":25.0,"clearances":5.0,"tackles":4.0,"goals":0.5,"marks":5.0}

    # Look for the season stats table
    tables = soup.find_all("table")
    for table in tables:
        headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        # Check if this looks like a stats table
        if not any(k in " ".join(headers) for k in ("disp","disposal","clr","tackle")): 
            continue

        def ci(*names):
            for n in names:
                for i,h in enumerate(headers):
                    if n in h: return i
            return None

        i_d  = ci("disp","de")
        i_cl = ci("clr","clearance")
        i_tk = ci("tkl","tackle")
        i_g  = ci("goal","gls","g")
        i_m  = ci("mark","mks","m")

        # Average the last column of data rows (season totals row or averages row)
        rows = table.find_all("tr")
        data_rows = [r for r in rows[1:] if r.find_all("td")]
        if not data_rows: continue

        # Try to find the "averages" row or use season total
        avg_row = None
        for row in data_rows:
            if "avg" in row.get_text(strip=True).lower():
                avg_row = row; break
        target_row = avg_row or data_rows[-1]
        cells_txt = [td.get_text(strip=True) for td in target_row.find_all("td")]

        def n(i,d=0):
            if i is None or i >= len(cells_txt): return d
            try: return float(re.sub(r"[^\d.]","",cells_txt[i]) or d)
            except: return d

        if i_d:  stats["disposals"]   = n(i_d, 25.0)
        if i_cl: stats["clearances"]  = n(i_cl, 5.0)
        if i_tk: stats["tackles"]     = n(i_tk, 4.0)
        if i_g:  stats["goals"]       = n(i_g, 0.5)
        if i_m:  stats["marks"]       = n(i_m, 5.0)
        break

    return stats


# Body parts listed in priority order — more specific terms first so
# "Achilles" wins over "Foot", "Hamstring" over "Leg", etc.
INJURY_BODY_PARTS = [
    "achilles", "concussion", "hamstring", "shoulder", "collarbone",
    "ankle", "knee", "groin", "quad", "calf", "thigh", "shin",
    "hip", "back", "ribs", "chest", "abdomen", "elbow", "wrist",
    "hand", "finger", "thumb", "foot", "toe", "leg", "arm",
    "neck", "head", "jaw", "nose", "eye", "face",
    "illness", "suspension", "personal", "managed", "rest",
]

# Canonical-name swaps applied AFTER priority-list matching AND after the
# fallback first-token path. In AFL injury parlance "head" listed without
# qualification almost always means concussion — clubs use "Jaw", "Eye",
# "Nose", "Face" for non-concussion head/face injuries.
INJURY_BODY_PART_ALIASES = {
    "Head": "Concussion",
}

def _classify_injury_body_part(injury_text):
    """Pull a canonical body-part keyword out of Footywire's free-text injury cell.
    Examples: 'Hamstring' -> 'Hamstring', 'Leg/Calf' -> 'Calf',
    'Right shoulder' -> 'Shoulder', 'Foot/Achilles' -> 'Achilles',
    'Head' -> 'Concussion' (via INJURY_BODY_PART_ALIASES)."""
    if not injury_text: return ""
    lower = injury_text.lower()
    for part in INJURY_BODY_PARTS:
        if part in lower:
            canon = part.capitalize()
            return INJURY_BODY_PART_ALIASES.get(canon, canon)
    # Fallback: strip side qualifiers and take the last token after a slash
    tokens = re.split(r"[\s/]+", injury_text.strip())
    tokens = [t for t in tokens if t.lower() not in ("left", "right", "lower", "upper")]
    canon = tokens[-1].capitalize() if tokens else injury_text.strip().capitalize()
    return INJURY_BODY_PART_ALIASES.get(canon, canon)


def _classify_injury_returning(returning_text):
    """Map Footywire's 'Returning' cell into (status, eta) where
    status in {'out', 'test', 'available'} and eta is a tidy display string.
    Players on the injury list at all default to 'test' if no harder signal."""
    if not returning_text:
        return "test", "TBC"
    raw = returning_text.strip()
    lower = raw.lower()

    if lower in ("test", "tbc", "managed", "rested"):
        return "test", raw.title() if lower != "tbc" else "TBC"

    if "season" in lower or "indef" in lower or "career" in lower:
        return "out", "Season"

    # "1-2 weeks", "3 weeks", "5+ weeks"
    if re.search(r"\d+\s*\+?\s*(?:-\s*\d+\s*)?week", lower):
        return "out", re.sub(r"\s+", " ", raw)

    # "2 months", "3-4 months"
    if re.search(r"\d+\s*\+?\s*(?:-\s*\d+\s*)?month", lower):
        return "out", re.sub(r"\s+", " ", raw)

    if re.match(r"round\s*\d+", lower):
        return "out", raw

    # Anything else (e.g. "Available", "Cleared") — treat as available
    if "avail" in lower or "clear" in lower or "fit" in lower:
        return "available", raw

    # Unrecognised — assume managed/uncertain
    return "test", raw or "TBC"


def parse_injury_list(html):
    """
    Parse Footywire injury list page.

    Footywire layout: one nested 3-column table per club with header row
      Player | Injury | Returning
    Header cells are <td class="lbnorm"> / <td class="bnorm"> (NOT <th>);
    data rows use class="darkcolor"/"lightcolor" — there's no rowpid_ id.

    Returns dict keyed by name_key(player) → {status, body_part, eta, detail}
    where status is one of {'out', 'test', 'available'}.
    """
    soup = BeautifulSoup(html, "lxml")
    injuries = {}

    # Each per-club table starts with a "<club> (N Players)" title row in its
    # outer table, then a nested 3-column table. Find every nested table whose
    # first row reads "Player | Injury | Returning".
    for table in soup.find_all("table"):
        rows = table.find_all("tr", recursive=False)
        if len(rows) < 2: continue
        header_cells = [c.get_text(strip=True).lower()
                        for c in rows[0].find_all(["td","th"], recursive=False)]
        if not header_cells: continue
        joined = " ".join(header_cells)
        if "player" not in joined or "injury" not in joined or "return" not in joined:
            continue

        try:
            i_player    = header_cells.index("player")
        except ValueError:
            i_player = 0
        try:
            i_injury    = header_cells.index("injury")
        except ValueError:
            i_injury = 1
        try:
            i_returning = next(i for i, h in enumerate(header_cells) if "return" in h)
        except StopIteration:
            i_returning = 2

        # Which club's table is this? The nearest preceding "<Club> (N Players)"
        # title row identifies it — kept so same-named players on different teams
        # (e.g. the two Bailey Williams) don't share one injury record.
        club = ""
        _ct = table.find_previous(string=re.compile(r"\(\s*\d+\s+Players?\s*\)"))
        if _ct:
            _cm = re.match(r"\s*(.+?)\s*\(\s*\d+\s+Players?\s*\)", str(_ct))
            if _cm:
                club = normalise_team(_cm.group(1))

        for tr in rows[1:]:
            cells = tr.find_all(["td","th"], recursive=False)
            if len(cells) <= max(i_player, i_injury, i_returning): continue
            name_cell = cells[i_player]
            name_link = name_cell.find("a")
            name = (name_link.get_text(strip=True) if name_link else name_cell.get_text(strip=True))
            name = name.replace("\xa0", " ").strip()
            if not name: continue

            injury_raw    = cells[i_injury].get_text(strip=True)
            returning_raw = cells[i_returning].get_text(strip=True)

            status, eta  = _classify_injury_returning(returning_raw)
            body_part    = _classify_injury_body_part(injury_raw)

            _rec = {
                "status":    status,        # "out" | "test" | "available"
                "body_part": body_part,     # "Hamstring", "Knee", ...
                "eta":       eta,           # "2 weeks", "Season", "TBC", ...
                "detail":    injury_raw,    # raw injury text (kept for back-compat / titles)
                "returning": returning_raw, # raw returning text
                "team":      club,          # normalised club, for name collisions
            }
            injuries[name_key(name)] = _rec
            if club:
                injuries[name_key(name) + "|" + club] = _rec

    log.info(f"Injuries: found {len(injuries)} players with injury notes")
    return injuries


def parse_selection_changes(html):
    """
    Parse selection changes page for role changes, ins/outs.
    Returns dict of {player_name: {"change": "in|out|emergency|sub", "note": "..."}}
    """
    soup = BeautifulSoup(html, "lxml")
    changes = {}

    for section in soup.find_all(["div","section"], class_=re.compile("selection|change|team", re.I)):
        for row in section.find_all("li"):
            text = row.get_text(strip=True)
            # Try to extract player name (usually first words before -)
            m = re.match(r"([A-Z][a-z]+ [A-Z][a-z]+)\s*[-–]\s*(.+)", text)
            if m:
                name = m.group(1)
                note = m.group(2)
                change = "in" if "in" in note.lower() else ("out" if "out" in note.lower() else "change")
                changes[name.lower()] = {"change": change, "note": note}

    log.info(f"Selections: found {len(changes)} change notes")
    return changes


def _largest_table(soup):
    """Return the largest table in the page by row count."""
    tables = soup.find_all("table")
    if not tables: return None
    return max(tables, key=lambda t: len(t.find_all("tr")))


# ── DATA MERGING ─────────────────────────────────────────────────────────────

TEAM_COLOURS = {
    "Adelaide":{"tc":"#3f86e0","tb":"rgba(63,134,224,0.14)"},
    "Brisbane":{"tc":"#c14b6e","tb":"rgba(193,75,110,0.14)"},
    "Carlton":{"tc":"#2452b0","tb":"rgba(36,82,176,0.14)"},
    "Collingwood":{"tc":"#bcbcbc","tb":"rgba(255,255,255,0.07)"},
    "Essendon":{"tc":"#e0454c","tb":"rgba(224,69,76,0.14)"},
    "Fremantle":{"tc":"#9a5fc8","tb":"rgba(154,95,200,0.14)"},
    "Geelong":{"tc":"#5a93e0","tb":"rgba(90,147,224,0.14)"},
    "Gold Coast":{"tc":"#e0a838","tb":"rgba(224,168,56,0.14)"},
    "GWS Giants":{"tc":"#ef7a2a","tb":"rgba(239,122,42,0.14)"},
    "Hawthorn":{"tc":"#a8743a","tb":"rgba(168,116,58,0.14)"},
    "Melbourne":{"tc":"#d24a5e","tb":"rgba(210,74,94,0.14)"},
    "North Melbourne":{"tc":"#8fb4f0","tb":"rgba(143,180,240,0.14)"},
    "Port Adelaide":{"tc":"#2bb3a8","tb":"rgba(43,179,168,0.14)"},
    "Richmond":{"tc":"#f0c040","tb":"rgba(240,192,64,0.10)"},
    "St Kilda":{"tc":"#e84a58","tb":"rgba(232,74,88,0.14)"},
    "Sydney":{"tc":"#e0443a","tb":"rgba(224,68,58,0.14)"},
    "West Coast":{"tc":"#efb43a","tb":"rgba(239,180,58,0.14)"},
    "Western Bulldogs":{"tc":"#6a78d4","tb":"rgba(106,120,212,0.14)"},
}

TEAM_ALIASES = {
    # Full / partial club names
    "GWS":"GWS Giants","GREATER WESTERN SYDNEY":"GWS Giants",
    "WESTERN BULLDOGS":"Western Bulldogs","DOGS":"Western Bulldogs",
    "PORT":"Port Adelaide","PORT ADELAIDE":"Port Adelaide",
    "NORTH":"North Melbourne","NORTH MELBOURNE":"North Melbourne",
    "GOLD COAST":"Gold Coast",
    "BRISBANE":"Brisbane","BRISBANE LIONS":"Brisbane",
    "ST KILDA":"St Kilda",
    "WEST COAST":"West Coast",
    "SYDNEY":"Sydney","SYDNEY SWANS":"Sydney",
    # Footywire nicknames (as returned by /supercoach_season etc.)
    "SWANS":"Sydney",
    "KANGAROOS":"North Melbourne",
    "CROWS":"Adelaide",
    "DOCKERS":"Fremantle",
    "CATS":"Geelong",
    "SUNS":"Gold Coast",
    "GIANTS":"GWS Giants",
    "HAWKS":"Hawthorn",
    "DEMONS":"Melbourne",
    "POWER":"Port Adelaide",
    "TIGERS":"Richmond",
    "SAINTS":"St Kilda",
    "EAGLES":"West Coast",
    "BULLDOGS":"Western Bulldogs",
    "BOMBERS":"Essendon",
    "BLUES":"Carlton",
    "MAGPIES":"Collingwood",
    "LIONS":"Brisbane",
}

def normalise_team(raw):
    if not raw: return "Unknown"
    clean = raw.strip().upper()
    if clean in TEAM_ALIASES: return TEAM_ALIASES[clean]
    # Try title case match
    for k,v in TEAM_COLOURS.items():
        if k.upper() == clean: return k
    # Partial match
    for k in TEAM_COLOURS:
        if k.upper() in clean or clean in k.upper(): return k
    return raw.strip()

def normalise_pos(raw):
    if not raw: return "MID"
    # Take the first listed code (handles "FOR, RUC" / "MID/FWD") then canonicalise.
    p = re.split(r"[/,]", str(raw).upper())[0].strip()
    return {"DEF":"DEF","MID":"MID","RUC":"RUC","FWD":"FWD","D":"DEF","M":"MID","R":"RUC","F":"FWD",
            "FOR":"FWD","FORWARD":"FWD","RUCK":"RUC","DEFENDER":"DEF","MIDFIELD":"MID",
            "MIDFIELDER":"MID","RUCKMAN":"RUC","BACK":"DEF"}.get(p,"MID")

def normalise_pos_list(raw):
    """Split a multi-position flag (e.g. "MID/FWD", "DEF,MID") into a deduped
    list of canonical codes, preserving order. Used for dual-position players."""
    if not raw: return []
    m = {"DEF":"DEF","MID":"MID","RUC":"RUC","FWD":"FWD","D":"DEF","M":"MID","R":"RUC","F":"FWD",
         # Footywire's SuperCoach playerflag abbreviates forward as "FOR" (and
         # ruck as "RUCK"); without these, every FOR-combo DPP (MID,FOR / DEF,FOR
         # / FOR,RUC) silently dropped its forward leg and looked single-position.
         "FOR":"FWD","FWDS":"FWD","FORWARD":"FWD","RUCK":"RUC","DEFENDER":"DEF",
         "MIDFIELD":"MID","MIDFIELDER":"MID","RUCKMAN":"RUC","BACK":"DEF"}
    out = []
    for part in re.split(r"[/,]", str(raw).upper()):
        part = part.strip()
        if not part: continue
        v = m.get(part)
        if v and v not in out: out.append(v)
    return out

def name_key(name):
    return re.sub(r"[^a-z]","",name.lower())

def stable_pid(name, team=""):
    """Deterministic player id derived from name (+team), STABLE across scrapes.

    The previous id was the player's position in the rank-sorted list (the
    enumerate index), which reshuffled every scrape — so saved My Team /
    watchlist ids silently remapped to different players. A name hash gives each
    player the same id every run, so persisted selections survive a refresh.
    Team is folded in to separate genuine same-name players (e.g. the two Bailey
    Williams); it's stable within a season (trades happen post-season), so it
    doesn't churn ids in normal use. Uses hashlib (not the builtin hash(), which
    is per-process salted) for cross-run determinism."""
    key = name_key(name) + "|" + re.sub(r"[^a-z]", "", (team or "").lower())
    return int(hashlib.md5(key.encode("utf-8")).hexdigest()[:8], 16)

def build_signal(avg3, be, inj, price_delta):
    s = 0
    diff = (avg3 or 0) - (be or 0)
    if diff > 15: s += 25
    elif diff > 5: s += 12
    elif diff < -15: s -= 25
    elif diff < -5: s -= 12
    if (price_delta or 0) > 20000: s += 15
    elif (price_delta or 0) < -20000: s -= 15
    if inj == "out": s -= 30
    elif inj in ("test", "tbc"): s -= 20
    sig = "buy" if s >= 30 else ("sell" if s <= -15 else "hold")
    return sig, min(95, max(40, 50 + abs(s)))

def auto_tags(p):
    t = []
    inj = p.get("injuryStatus","available")
    if inj == "out": t.append("OUT")
    elif inj in ("test", "tbc"): t.append("TEST")
    avg3 = p.get("scAvg3",0) or 0
    be   = p.get("breakeven",0) or 0
    pd   = p.get("priceDelta",0) or 0
    own  = p.get("owned",0) or 0
    sig  = p.get("signal","hold")
    if avg3 >= 120: t.append("Premium")
    elif avg3 >= 108: t.append("Top 30")
    if pd > 15000: t.append("Price rising")
    elif pd < -12000: t.append("Price falling")
    if own < 20 and sig == "buy": t.append("POD")
    elif own > 60: t.append("Popular")
    if avg3 > be + 15: t.append("B/E safe")
    return t[:5]

def estimate_price_history(current_price, avg3, be, num_rounds=7):
    """Estimate a price history sparkline from current price + trajectory."""
    history = []
    # Work backwards: if avg3 > be, price was rising
    diff = (avg3 or 0) - (be or 0)
    weekly_change = round(diff * 800)  # rough $-per-round
    for i in range(num_rounds, 0, -1):
        price = max(100000, round(current_price - (weekly_change * i * 0.7)))
        history.append(price)
    history.append(current_price)
    return history[-7:]

def price_history_from_scores(current_price, played_scores, be, num_rounds=6):
    """Reconstruct a realistic price sparkline from the player's actual recent
    SuperCoach scores. Working backwards from the current price, each round's
    price moved roughly with (score - break_even): a big score above the BE
    pushes the price up, a low score drops it. This gives a line with genuine
    round-to-round variation instead of estimate_price_history's straight ramp
    (which made the chart look like just two points).

    `played_scores` must already exclude byes/zero rounds. Returns a list of
    num_rounds+1 prices ending at current_price, or None if we can't build one.
    """
    if not current_price or not played_scores:
        return None
    MAGIC = 700  # rough $ per SuperCoach point of (score - breakeven)
    pts = [s for s in played_scores if s and s > 0][-num_rounds:]
    if not pts:
        return None
    hist = [int(current_price)]
    # pts[-1] is the most recent round; unwind it first to get the prior price.
    for s in reversed(pts):
        delta = round((s - (be or 0)) * MAGIC)
        hist.insert(0, max(100000, hist[0] - delta))
    return hist

def build_player(sc, dt, injuries, selections, rank):
    """Merge SC stats + DT stats + injury/selection data into the app schema."""

    dt = dt or {}   # downstream .get() calls assume a mapping; lookup may miss

    name  = sc.get("name","") or dt.get("name","")
    team  = normalise_team(sc.get("team","") or dt.get("team",""))
    _pid  = stable_pid(name, team)   # stable id (survives scrapes) for My Team/watchlist
    pos   = normalise_pos(sc.get("pos","") or dt.get("pos",""))
    positions = sc.get("sc_positions") or [pos]
    if pos not in positions: positions = [pos] + positions
    # AFL Fantasy Classic positions (separate list — see merge step). Falls
    # back to SC positions when Classic has no entry for the player.
    aflf_positions = sc.get("aflf_positions") or positions[:]
    col   = TEAM_COLOURS.get(team, {"tc":"#888","tb":"rgba(100,100,100,0.1)"})

    sc_avg   = sc.get("sc_avg", 0) or 0
    sc_avg3  = sc.get("sc_avg3", sc_avg) or sc_avg
    sc_last  = sc.get("sc_last", 0) or 0
    sc_price = sc.get("sc_price", 500000) or 500000
    sc_be    = sc.get("sc_be", 0) or 0
    # Guard against corrupted breakeven parses — Footywire occasionally returns a
    # career-total (or similar) in the BE cell, e.g. Neil Erasmus -> 6802, which then
    # cascades into a nonsense price delta ((avg3-be)*800) and a $22M price sparkline
    # ((score-be)*700). Real AF/SC breakevens sit well within ±500; treat anything
    # beyond that as unknown so it can't poison the "Biggest Fall" widget etc.
    if abs(sc_be) > 500:
        log.warning(f"{name}: implausible breakeven {sc_be} — treating as unknown (0)")
        sc_be = 0
    sc_owned = sc.get("sc_owned", 0) or 0
    sc_scores = sc.get("sc_scores", [sc_last]*7) or [sc_last]*7

    # sc_all_scores is chronological (R1 first, most recent last). The last score
    # must be the most recent round played — fall back to it if sc_last is 0.
    sc_all_scores = sc.get("sc_all_scores") or [s for s in sc_scores if s and s > 0]
    if not sc_last and sc_all_scores:
        _non_zero = [s for s in sc_all_scores if s and s > 0]
        sc_last = _non_zero[-1] if _non_zero else 0

    if "ridley" in name.lower():
        log.info(f"[Ridley check] {name} sc_last={sc_last} sc_avg={sc_avg} "
                 f"sc_avg3={sc_avg3} sc_all_scores={sc_all_scores}")

    # AFL Fantasy Classic ownership (Footywire doesn't expose SC ownership for free,
    # so Classic ownership from fantasy.afl.com.au is the only live ownership signal).
    classic_owned = float(sc.get("classic_owned", 0) or 0)
    classic_avg   = float(sc.get("classic_avg",   0) or 0)
    classic_avg3  = float(sc.get("classic_avg3",  0) or 0)
    classic_proj  = float(sc.get("classic_proj",  0) or 0)
    classic_price = int(sc.get("classic_price", 0) or 0)

    # Prefer real per-round AF data harvested from the pg- page (now stored on
    # the sc dict) over synthesized SC*1.03 fallbacks; only fall back when the
    # pg fetch failed or the player wasn't in the top-N processed set.
    dt_avg   = sc.get("dt_avg")   or (dt.get("dt_avg", round(sc_avg  * 1.03)) if dt else round(sc_avg  * 1.03))
    dt_avg3  = sc.get("dt_avg3")  or (dt.get("dt_avg3",round(sc_avg3 * 1.03)) if dt else round(sc_avg3 * 1.03))
    dt_last  = sc.get("dt_last")  or (dt.get("dt_last",round(sc_last * 1.03)) if dt else round(sc_last * 1.03))
    dt_be    = dt.get("dt_be",  round(sc_be   * 0.97)) if dt else round(sc_be   * 0.97)
    dt_owned = dt.get("dt_owned", sc_owned) if dt else sc_owned
    dt_scores= sc.get("dt_scores") or (dt.get("dt_scores",[dt_last]*7) if dt else [dt_last]*7)

    # Injury status from injury list
    nk = name_key(name)
    inj_data      = injuries.get(nk + "|" + team) or {}
    if not inj_data:
        _no = injuries.get(nk)
        # A name-only match is trusted ONLY when the record's club matches this
        # player's club (or no club was parsed) — stops same-name players on
        # different teams (the two Bailey Williams) from sharing an injury.
        if _no and (not _no.get("team") or _no.get("team") == team):
            inj_data = _no
    sel_data      = selections.get(nk) or {}
    inj_status    = inj_data.get("status","available")
    inj_detail    = inj_data.get("detail","")
    inj_body_part = inj_data.get("body_part","") or inj_detail
    inj_eta       = inj_data.get("eta","")

    # Price delta estimate
    price_delta = round((sc_avg3 - sc_be) * 800) if sc_avg3 and sc_be else 0
    # Prefer a price history reconstructed from the player's real recent scores
    # (varies round-to-round); fall back to the straight-line estimate only when
    # we have no per-round scores for this player yet.
    price_hist  = (price_history_from_scores(sc_price, sc_all_scores, sc_be)
                   or estimate_price_history(sc_price, sc_avg3, sc_be))

    sig, conf = build_signal(sc_avg3, sc_be, inj_status, price_delta)

    # Consistency: % of the LAST 5 rounds within 90% of their 5-round average.
    all_sc = sc.get("sc_all_scores", sc_scores)
    played = [s for s in all_sc if s and s > 0]
    last5 = played[-5:]
    avg5 = (sum(last5) / len(last5)) if last5 else sc_avg
    threshold = avg5 * 0.9
    consistency = round(len([s for s in last5 if s >= threshold]) / len(last5) * 100) if last5 else 75

    # Build tags and reason. sc_owned is always 0 (Footywire doesn't expose it),
    # so tag rules use Classic ownership as the live ownership signal.
    p = {
        "injuryStatus": inj_status,
        "signal": sig,
        "scAvg3": round(sc_avg3, 1),
        "breakeven": sc_be,
        "priceDelta": price_delta,
        "owned": round(classic_owned or sc_owned, 1),
    }
    tag_list = auto_tags(p)

    parts = [f"{name} — {sig.upper()} signal."]
    if sc_avg3 and sc_be:
        diff = round(sc_avg3 - sc_be)
        parts.append(f"3-round avg {round(sc_avg3)} is {abs(diff)} {'above' if diff>0 else 'below'} break-even ({sc_be}).")
    if inj_status == "out":   parts.append(f"OUT — {inj_detail or inj_body_part or 'injury'}.")
    elif inj_status == "test": parts.append(f"TEST — {inj_detail or inj_body_part or 'managed'}.")

    # Build news items from injury/selection data.
    # tags layout for injury items is [STATUS, BODY_PART, ETA] — the frontend's
    # iTags[0]/iTags[1]/iTags[2] chip row reads them positionally.
    news = []
    if inj_status in ("out","test") and (inj_body_part or inj_detail):
        news.append({
            "id":1, "type":"injury", "source":"Footywire",
            "time":"latest", "timeLabel":"latest",
            "pid":  _pid,           # frontend keys on pid to find the player record
            "player": name, "team": team, "pos": pos,
            "title": f"{name} — {inj_status.upper()}: {inj_body_part or inj_detail}",
            "headline": f"{name} — {inj_status.upper()}: {inj_body_part or inj_detail}",
            "body":  f"Status: {inj_status.upper()}. {inj_body_part or inj_detail}. ETA: {inj_eta or 'TBC'}.",
            "tags": [inj_status.upper(), inj_body_part or inj_detail, inj_eta or "TBC"],
        })
    if sel_data.get("note"):
        news.append({
            "id":2, "type":"selection", "source":"Footywire",
            "time":"latest", "timeLabel":"latest",
            "pid": _pid, "player": name, "team": team, "pos": pos,
            "title": f"Selection update: {name}",
            "headline": f"Selection update: {name}",
            "body": sel_data["note"],
            "tags": ["Selection", sel_data.get("change","").title()],
        })

    return {
        "id": _pid,
        "name": name,
        "init": (name.split()[0][0] + name.split()[-1][0]).upper() if len(name.split())>=2 else name[:2].upper(),
        "team": team,
        "pos": pos,
        "positions": positions,
        "aflfPositions": aflf_positions,
        "tc": col["tc"],
        "tb": col["tb"],

        "signal": sig,
        "signalConf": conf,
        "rank": sc.get("sc_rank", rank),
        "afRank": dt.get("dt_rank", rank) if dt else rank,

        "owned": round(classic_owned or sc_owned, 1),
        "ownedDelta": 0,   # requires two fetches to compute delta
        "classicOwned": round(classic_owned, 1),
        "classicAvg":   round(classic_avg,  1),
        "classicAvg3":  round(classic_avg3, 1),
        "classicProj":  round(classic_proj, 1),
        "classicPrice": classic_price,

        "scAvg":   round(sc_avg,  1),
        "scAvg3":  round(sc_avg3, 1),
        "lastScore": sc_last,
        "lastRound": (sc.get("round_stats") or [{}])[-1].get("r", ""),

        "dtAvg":  round(dt_avg,  1),
        "dtAvg3": round(dt_avg3, 1),
        "dtLast": dt_last,

        "price":      sc_price,
        "priceDelta": price_delta,
        "breakeven":  sc_be,
        "dtBe":       dt_be,

        "disposals":  sc.get("disposals",  sc.get("detail",{}).get("disposals",  0)),
        "clearances": sc.get("clearances", sc.get("detail",{}).get("clearances", 0)),
        "tackles":    sc.get("tackles",    sc.get("detail",{}).get("tackles",    0)),
        "goals":      sc.get("goals",      sc.get("detail",{}).get("goals",      0)),
        "marks":      sc.get("marks",      0),
        "hitouts":    sc.get("hitouts",    0),
        "kicks":      sc.get("kicks",      0),
        "handballs":  sc.get("handballs",  0),
        "behinds":    sc.get("behinds",    0),
        "gamesPlayed": sc.get("gamesPlayed", 0),

        "roundStats": sc.get("round_stats", []),
        "scores":   [s or 0 for s in sc_scores[-7:]],
        "dtScores": [s or 0 for s in dt_scores[-7:]],
        "prices":   price_hist,

        "ceiling": round(max(sc_scores[-7:] or [sc_avg*1.2])),
        "floor":   round(min([s for s in sc_scores[-7:] if s and s>0] or [sc_avg*0.75])),
        "consistency": consistency,

        "bshCommunity": {
            "buy":  60 if sig=="buy" else (15 if sig=="sell" else 35),
            "hold": 30 if sig=="hold" else (20 if sig=="buy" else 25),
            "sell": 10 if sig=="buy" else (60 if sig=="sell" else 40),
        },
        "injuryStatus": inj_status,
        "injuryDetail": inj_detail,
        "tags": tag_list,
        "bshReason": " ".join(parts),
        "scheduleRating": [7,7,7,7,7],
        "news": news,
        "_source": "footywire",
        "_scraped_at": datetime.now().isoformat(),
    }


# ── MAIN ─────────────────────────────────────────────────────────────────────

# Holds the most recent fully-built player list so the top-level crash handler
# can still persist it if something blows up after the merge step.
LAST_PLAYERS = []


# Display-name fixes: sources use a formal/short variant; map to the name
# fans use. Extend as needed.
NAME_ALIASES = {
    "Daniel Butler": "Dan Butler",
    "Harry Petty": "Harrison Petty",
    "Lachlan Ash": "Lachie Ash",
    "Cal Wilkie": "Callum Wilkie",
    "Bradley Hill": "Brad Hill",
    "Zachary Williams": "Zac Williams",
    "Zachary Merrett": "Zach Merrett",
    "Timothy English": "Tim English",
    "Joshua Kelly": "Josh Kelly",
    "Joshua Weddle": "Josh Weddle",
    "Thomas Liberatore": "Tom Liberatore",
    "Thomas Stewart": "Tom Stewart",
    "Thomas Sims": "Tom Sims",
    "Thomas Burton": "Tom Burton",
    "Thomas Matthews": "Tom Matthews",
    "Samuel Collins": "Sam Collins",
    "Samuel Swadling": "Sam Swadling",
    "Samuel Grlj": "Sam Grlj",
    "Nicholas Martin": "Nick Martin",
    "Nicholas Coffield": "Nick Coffield",
    "Nicholas Holman": "Nick Holman",
    "Mitchell Lewis": "Mitch Lewis",
    "Mitchell Knevitt": "Mitch Knevitt",
    "Mitchell Hinge": "Mitch Hinge",
    "Mitchell Edwards": "Mitch Edwards",
    "Matthew Kennedy": "Matt Kennedy",
    "Matthew Roberts": "Matt Roberts",
    "Matthew Flynn": "Matt Flynn",
    "Matthew Jefferson": "Matt Jefferson",
    "Matthew LeRay": "Matt LeRay",
    "Cameron Rayner": "Cam Rayner",
    "Cameron Mackenzie": "Cam Mackenzie",
    "Cameron Zurhaar": "Cam Zurhaar",
    "Cameron Nairn": "Cam Nairn",
    "Bradley Close": "Brad Close",
}


# Manual SC position overrides for cases where Footywire's playerflag is wrong
# or stale. Keyed by (name, team) so we don't accidentally affect different
# players sharing a name (e.g. two Bailey Williams: West Coast = RUC/FWD per
# user feedback; Western Bulldogs Bailey Williams stays Footywire-default).
# AFL Fantasy Classic positions live in a separate field (aflfPositions) and
# are NOT touched by these overrides.
SC_POSITION_OVERRIDES = {
    ("Bailey Williams", "West Coast"):       ["RUC", "FWD"],
    ("Bailey Williams", "Western Bulldogs"): ["DEF", "MID"],
    ("Cam Zurhaar", "North Melbourne"):      ["FWD", "DEF"],
}


# Players we want news-tagged even if they aren't on Footywire's active stats
# list (long-term injuries, off-contract rookies, mid-season signings). They get
# placeholder stats so the news extractor's name lookup matches them; they won't
# show up in Rankings/Waiver because their averages are 0. IDs are in a high
# range (9000+) to make them obviously synthetic and avoid colliding with
# Footywire's IDs.
MANUAL_EXTRAS = [
    {"id": 9001, "name": "Will Day",       "team": "Hawthorn",   "pos": "MID", "positions": ["MID"]},
    {"id": 9002, "name": "Marcus Herbert", "team": "West Coast", "pos": "FWD", "positions": ["FWD"]},
]


def _build_extras(existing_names):
    """Return a list of synthetic player records for the manual extras that
    aren't already covered by Footywire's roster."""
    out = []
    for x in MANUAL_EXTRAS:
        if x["name"] in existing_names:
            continue
        team = x["team"]
        colours = TEAM_COLOURS.get(team, {"tc": "#888888", "tb": "rgba(128,128,128,0.14)"})
        parts = x["name"].split()
        init = (parts[0][:1] + parts[-1][:1]).upper() if parts else ""
        out.append({
            "id": x["id"], "name": x["name"], "init": init,
            "team": team, "pos": x["pos"], "positions": x["positions"],
            "tc": colours["tc"], "tb": colours["tb"],
            "signal": None, "signalConf": 0,
            "rank": 999, "afRank": 999, "owned": 0, "classicOwned": 0,
            "classicAvg": 0, "classicAvg3": 0, "classicProj": 0, "classicPrice": 0,
            "scAvg": 0, "scAvg3": 0, "lastScore": 0, "lastRound": "",
            "dtAvg": 0, "dtAvg3": 0, "dtLast": 0,
            "price": 0, "priceDelta": 0, "breakeven": 0, "dtBe": 0,
            "disposals": 0, "clearances": 0, "tackles": 0, "goals": 0, "marks": 0, "hitouts": 0,
            "roundStats": [], "scores": [], "dtScores": [], "prices": [],
            "ceiling": 0, "floor": 0, "consistency": 0,
            "bshCommunity": {"buy": 0, "hold": 0, "sell": 0},
            "injuryStatus": "unknown", "injuryDetail": "",
            "tags": ["Watch"], "bshReason": "",
            "scheduleRating": [], "news": [],
            "_source": "manual_extras",
            "gamesBySeason": [], "injuryRisk": 0, "injuryRiskLabel": "Unknown", "injuryMissed": 0,
        })
    return out


CAREERS_PATH = BASE_DIR / "careers.json"
CAREER_TTL_DAYS = 7          # refresh each player's career data weekly
CAREER_TIME_LIMIT = 120      # seconds per run (incremental — fills over a few runs)


def parse_career_games(html):
    """Parse a Footywire /pu- profile page for games played per season.
    The season table rows look like [year, games, average]. Returns {year:int -> games:int}."""
    soup = BeautifulSoup(html, "lxml")
    for t in soup.find_all("table"):
        out = {}
        for row in t.find_all("tr"):
            cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
            if len(cells) >= 2 and re.fullmatch(r"20\d\d", cells[0]) and re.fullmatch(r"\d{1,2}", cells[1]):
                _avg = 0.0
                if len(cells) >= 3:
                    try:
                        _avg = round(float(cells[2]), 1)
                    except Exception:
                        _avg = 0.0
                out[int(cells[0])] = (int(cells[1]), _avg)
        if len(out) >= 3:
            return out
    return {}


def injury_risk_score(games, injury_status=""):
    """Availability/durability-based injury-risk score (0-100, higher = riskier)
    from games played over the most recent seasons, plus current status."""
    if not games:
        return None, "", None
    yrs = sorted(games)
    last3 = [games[y] for y in yrs[-3:]]
    FULL = 22.0
    # average games MISSED per season over the rolling last 3 seasons
    missed = sum(max(0.0, FULL - g) for g in last3) / len(last3)
    if missed < 2:
        label, risk = "Low", int(round(missed / 2 * 29))
    elif missed < 4:
        label, risk = "Medium", int(round(30 + (missed - 2) / 2 * 29))
    else:
        label, risk = "High", min(100, int(round(60 + (missed - 4) * 10)))
    return risk, label, round(missed, 1)


def fetch_careers(session, players, sc_players):
    """Incrementally fetch each current player's career games-per-season (cached
    weekly in careers.json) and merge gamesBySeason + injuryRisk into players."""
    try:
        cache = json.loads(CAREERS_PATH.read_text(encoding="utf-8"))
    except Exception:
        cache = {}
    url_by_name = {name_key(s["name"]): s.get("profile_url", "")
                   for s in sc_players if s.get("profile_url")}
    now = datetime.now(timezone.utc)

    def _stale(rec):
        try:
            return (now - datetime.fromisoformat(rec.get("ts"))) > timedelta(days=CAREER_TTL_DAYS)
        except Exception:
            return True

    todo = [(name_key(p["name"]), url_by_name.get(name_key(p["name"])))
            for p in players
            if url_by_name.get(name_key(p["name"]))
            and (name_key(p["name"]) not in cache or _stale(cache[name_key(p["name"])]))]
    log.info(f"Career fetch: {len(todo)} players due (cap {CAREER_TIME_LIMIT}s)")
    start, done = time.time(), 0
    for nk, url in todo:
        if time.time() - start > CAREER_TIME_LIMIT:
            log.info(f"Career fetch: time cap hit after {done}")
            break
        try:
            r = get(session, url, retries=1, timeout=8)
            if not r:
                continue
            g = parse_career_games(r.text)
            if g:
                cache[nk] = {"games": {str(y): v[0] for y, v in g.items()},
                             "avgs": {str(y): v[1] for y, v in g.items()},
                             "ts": now.isoformat()}
                done += 1
        except Exception:
            continue
    try:
        CAREERS_PATH.write_text(json.dumps(cache), encoding="utf-8")
    except Exception:
        pass
    merged = 0
    for p in players:
        rec = cache.get(name_key(p["name"]))
        if not rec or not rec.get("games"):
            continue
        g = {int(y): int(v) for y, v in rec["games"].items()}
        av = {int(y): float(v) for y, v in rec.get("avgs", {}).items()}
        yrs = sorted(g)
        p["gamesBySeason"] = [{"y": y, "g": g[y], "a": av.get(y, 0)} for y in yrs[-5:]]
        risk, label, missed = injury_risk_score(g, p.get("injuryStatus", ""))
        if risk is not None:
            p["injuryRisk"], p["injuryRiskLabel"], p["injuryMissed"] = risk, label, missed
        merged += 1
    log.info(f"Career: fetched {done} this run, merged {merged} players (cache {len(cache)})")


def _compute_injury_rating(p, current_round):
    """% of team games played in trailing ~24 months — current season to date
    plus the previous full season (capped at 22 home-and-away games).

    Returns int 0-100, or None for players with no historical games data
    (rookies pre-debut, missing gamesBySeason). A player who debuted only in
    the current season counts against the current-season denominator alone,
    so a rookie playing every game scores 100, not ~35.
    """
    seasons = {row.get("y"): row.get("g", 0)
               for row in (p.get("gamesBySeason") or [])
               if row.get("y") is not None}
    if not seasons:
        return None
    cur_year = max(seasons)
    played = possible = 0.0
    if cur_year in seasons:
        # Denominator = rounds the player's team has actually played (byes
        # excluded), not the raw round number — otherwise a player who only
        # missed his team's bye is dinged to ~97% instead of 100%.
        _trp = p.get("teamRoundsPlayed") or current_round
        possible += _trp
        played   += min(seasons[cur_year], _trp)
    if seasons.get(cur_year - 1, 0) > 0:
        possible += 22
        played   += min(seasons[cur_year - 1], 22)
    if possible == 0:
        return None
    return max(0, min(100, round(100 * played / possible)))


DVP_PATH = BASE_DIR / "dvp.json"
_DVP_STATS = [("sc", "sc"), ("dis", "disposals"), ("k", "kicks"),
              ("hb", "handballs"), ("mk", "marks"), ("tk", "tackles"), ("gl", "goals")]


def build_dvp(players):
    """Position-vs-team matrix from per-round game logs: how players of each
    position actually perform (per stat) against each opponent, vs the league
    average for that position. Powers the prediction breakdown + Matchups view."""
    from collections import defaultdict
    dvp = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    lg = defaultdict(lambda: defaultdict(list))
    pos_set = {"DEF", "MID", "RUC", "FWD"}
    for p in players:
        pos = (p.get("pos") or "MID").upper()
        if pos not in pos_set:
            pos = "MID"
        for r in (p.get("roundStats") or []):
            if (r.get("r") or 0) < 1:   # exclude Opening Round from matchup data
                continue
            opp = r.get("opp")
            if not opp or not _coach_valid_2026(opp, r.get("r")):
                continue
            for sk, full in _DVP_STATS:
                v = r.get(sk)
                if v is None:
                    continue
                dvp[opp][pos][full].append(v)
                lg[pos][full].append(v)
    _mean = lambda x: round(sum(x) / len(x), 1) if x else None
    league = {ps: {full: _mean(lg[ps][full]) for _, full in _DVP_STATS if lg[ps][full]}
              for ps in pos_set}
    teams = {}
    for t in dvp:
        cell_t = {}
        for ps in pos_set:
            cell = {full: {"avg": _mean(dvp[t][ps][full]), "n": len(dvp[t][ps][full])}
                    for _, full in _DVP_STATS if len(dvp[t][ps][full]) >= 4}
            if cell:
                cell_t[ps] = cell
        if cell_t:
            teams[t] = cell_t
    # 2025 historical layer (coach-changed teams already excluded at fetch time)
    hlg = defaultdict(lambda: defaultdict(list))
    hist_teams = {}
    for t, pd in _DVP_2025.items():
        cell_t = {}
        for ps, sd in pd.items():
            cell = {}
            for sk, vals in sd.items():
                if len(vals) >= 4:
                    m = round(sum(vals) / len(vals), 1)
                    cell[sk] = {"avg": m, "n": len(vals)}
                    hlg[ps][sk].append(m)
            if cell:
                cell_t[ps] = cell
        if cell_t:
            hist_teams[t] = cell_t
    hist_league = {ps: {sk: round(sum(v) / len(v), 1) for sk, v in sd.items()} for ps, sd in hlg.items()}
    out = {
        "league": league,
        "teams": teams,
        "teamsHist": hist_teams,
        "leagueHist": hist_league,
        "coachChanged": sorted(COACH_CHANGED_TEAMS),
        "abbr": {t: _TEAM_ABBR.get(t, t[:3].upper()) for t in teams},
        "stats": [f for _, f in _DVP_STATS],
        "positions": sorted(pos_set),
    }
    try:
        with open(DVP_PATH, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
        log.info(f"DvP matrix: {len(teams)} teams written to dvp.json")
    except Exception as _e:
        log.error(f"DvP write failed: {_e}")


ROLE_OVERRIDES = {
    "Colby McKercher": "Half Forward",  # moved forward mid-season; season avg reads as a defender
}

AFL_INJURIES_PATH = BASE_DIR / "afl_injuries.json"


def fetch_afl_injury_list(session, target_round):
    """Auto-update afl_injuries.json from the latest official AFL medical-room
    article. The AFL news listing is JS-rendered, so the article URL is located
    via DuckDuckGo (targeting the current round to skip stale prior-season ones),
    then the per-club tables are parsed straight from the article HTML. On any
    failure the existing snapshot is left intact."""
    import urllib.parse
    url, found = None, None
    for rnd in (target_round, target_round - 1, target_round + 1):
        if rnd < 1:
            continue
        q = "afl.com.au medical room the full afl injury list R%d" % rnd
        r = get(session, "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(q),
                retries=1, timeout=12)
        if not r:
            continue
        cands = re.findall(
            r"https?://www\.afl\.com\.au/news/\d+/medical-room-the-full-afl-injury-list-r"
            + str(rnd) + r"(?![0-9])", urllib.parse.unquote(r.text))
        if cands:
            url, found = cands[0], rnd
            break
    if not url:
        log.warning("AFL medical-room article not found; keeping existing afl_injuries.json")
        return
    r = get(session, url, retries=2, timeout=15)
    if not r:
        log.warning("AFL medical-room fetch failed; keeping existing afl_injuries.json")
        return
    soup = BeautifulSoup(r.text, "lxml")
    clubs = ["Adelaide", "Brisbane", "Carlton", "Collingwood", "Essendon", "Fremantle",
             "Geelong", "Gold Coast", "GWS Giants", "Hawthorn", "Melbourne", "North Melbourne",
             "Port Adelaide", "Richmond", "St Kilda", "Sydney", "West Coast", "Western Bulldogs"]
    players, ci = {}, 0
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        hdr = " ".join(c.get_text(" ", strip=True).lower() for c in rows[0].find_all(["td", "th"]))
        if "player" not in hdr or "injury" not in hdr:
            continue
        club = clubs[ci] if ci < len(clubs) else ""
        ci += 1
        for tr in rows[1:]:
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            if len(cells) < 3:
                continue
            name, injury, eta = cells[0].strip(), cells[1].strip(), cells[2].strip()
            if not name or name.lower() == "player":
                continue
            players[name] = {"club": club, "injury": injury, "eta": eta,
                             "status": "test" if "test" in eta.lower() else "out"}
    if ci != 18:
        log.warning(f"AFL injury list: parsed {ci} club tables (expected 18)")
    if len(players) < 50:
        log.warning(f"AFL injury list parse thin ({len(players)}); keeping existing snapshot")
        return
    out = {"round": found, "source": url,
           "asOf": datetime.now().strftime("%Y-%m-%d"), "players": players}
    with open(AFL_INJURIES_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    log.info(f"AFL injury list R{found}: {len(players)} players -> afl_injuries.json")


def reconcile_injuries(players):
    """Verify injuryStatus against the official AFL medical-room snapshot
    (afl_injuries.json: the weekly afl.com.au list). Players ON the list take its
    status; players NOT on the list are forced 'available' so stale Footywire
    false positives (e.g. a recovered player still flagged) are cleared. Matched
    by club + surname (+ first initial) to survive name-form differences."""
    try:
        afl = json.loads(AFL_INJURIES_PATH.read_text(encoding="utf-8"))
    except Exception as _e:
        log.warning(f"No AFL injury snapshot to reconcile against: {_e}")
        return
    entries = afl.get("players") or {}
    if not entries:
        return
    from collections import defaultdict
    by_nk, by_ln = defaultdict(list), defaultdict(list)
    for p in players:
        tm = normalise_team(p.get("team", ""))
        nm = p.get("name", "")
        if not nm:
            continue
        by_nk[(tm, name_key(nm))].append(p)
        by_ln[(tm, nm.split()[-1].lower())].append(p)
    matched = set()
    for an, info in entries.items():
        club = normalise_team(info.get("club", ""))
        cand = list(by_nk.get((club, name_key(an)), []))
        if not cand and an:
            ln, fi = an.split()[-1].lower(), an[0].lower()
            same_ln = by_ln.get((club, ln), [])
            cand = [p for p in same_ln if p.get("name", "")[:1].lower() == fi] or same_ln
        for p in cand:
            p["injuryStatus"] = info.get("status", "out")
            _det = info.get("injury", "")
            if info.get("eta"):
                _det = (_det + " (" + info["eta"] + ")").strip()
            p["injuryDetail"] = _det
            matched.add(id(p))
    # Additive only: the AFL medical-room list is NOT exhaustive (concussion
    # protocols, late adds are often missing), so we confirm what it lists but
    # never clear injuries other sources (Footywire/news) have caught.
    log.info(f"Injury reconcile vs AFL R{afl.get('round','?')} list: "
             f"{len(matched)} confirmed (additive; other sources' injuries kept)")


_PRED_SK   = ("disposals", "kicks", "handballs", "marks", "tackles", "behinds", "goals")
_PRED_RK   = {"disposals": "dis", "kicks": "k", "handballs": "hb", "marks": "mk",
              "tackles": "tk", "behinds": "b", "goals": "gl"}


def reconcile_predictions(players):
    """Safety net: a served ``statPred`` MUST be reconstructable from the player's
    CURRENT ``roundStats`` via the same chain the predict-tab breakdown shows —
    ``(0.55·last3 + 0.45·season) × matchup × teamFactor × teamWeight``. A
    truncated or crashed run can advance ``roundStats`` (the cheap per-round merge
    touches all players) without re-running the heavier prediction pass, leaving a
    stored prediction that no longer matches its inputs — so the breakdown
    "doesn't add up". Here we recompute each NON-frozen player's prediction from
    current inputs and, per stat, replace any value that has drifted further than
    calibration could explain (>20%, beyond the ±15% calibration clamp). Stored
    values within tolerance are left untouched so the learned calibration nudge is
    preserved. Intentionally-frozen predictions — kick-off lock (`predLocked`),
    round hold (`roundHeld`), bye (`byeNext`) and carried-forward (`_carried`) —
    are skipped; they are MEANT to differ from a fresh recompute. Returns the
    number of players corrected (0 in a healthy run)."""
    fixed = 0
    for p in players:
        if (p.get("predLocked") or p.get("roundHeld")
                or p.get("byeNext") or p.get("_carried")):
            continue
        sp = p.get("statPred")
        if not sp:
            continue
        rstats = p.get("roundStats") or []
        mm_all = p.get("statMatch") or {}
        tf = p.get("teamFactor") or 1
        tag = ((p.get("tagWt") or {}).get("stats")) or {}   # elite-tag per-stat mult
        splow = p.get("statPredLow") or {}
        recomputed, touched = {}, False
        for sk in _PRED_SK:               # behinds before goals (feeds the bonus)
            rk = _PRED_RK[sk]
            savg = p.get(sk) or 0
            gbonus = (recomputed.get("behinds", 0) or 0) / 3.0 if sk == "goals" else 0
            rs3 = [r.get(rk) for r in rstats if r.get(rk) is not None][-3:]
            a3 = sum(rs3) / len(rs3) if rs3 else savg
            val = round((0.55 * a3 + 0.45 * savg) * mm_all.get(sk, 1) * tf + gbonus, 1)
            if tag.get(sk):
                val = round(val * tag[sk], 1)
            recomputed[sk] = val
            cur = sp.get(sk)
            if cur is None:
                continue
            # Drift beyond the calibration clamp's reach (±15%) => the stored value
            # is stale, not merely calibrated. Replace just this stat.
            if abs(cur - val) > max(0.3, 0.2 * max(abs(val), abs(cur))):
                sp[sk] = val
                _dvals = [r.get(rk) for r in rstats if r.get(rk) is not None]
                _dv = LOW_RANGE_K * _sigma(_dvals) if len(_dvals) >= 3 else max(0.8, val * 0.15)
                splow[sk] = max(0, round(val - _dv, 1))
                touched = True
        if touched:
            p["statPredLow"] = splow
            p["predReconciled"] = True
            fixed += 1
    if fixed:
        log.warning(f"Prediction reconcile: recomputed {fixed} player(s) whose statPred "
                    f"had drifted from current roundStats — a truncated/crashed run "
                    f"likely advanced scores without refreshing predictions")
    return fixed


# Gold "high-conviction" flag per stat for the predict tab. A gold pick is one
# whose LOW prediction band clears the season average by a real margin AND whose
# recent form is consistent, trending and facing a favourable matchup — i.e. a
# floor you can bank on, not just "predicted above average". Refined 2026-06-15
# (was a bare low>=avg, which flagged ~50 players) so the predict tab's
# gold-only view is genuinely the highest-conviction set. Stored as
# statGold {stat: true} + a hasGold convenience flag for the UI to read/filter.
GOLD_MARGIN      = 1.08   # low band must clear the season avg by >=8%
GOLD_CONSISTENCY = 0.55   # no game in the last 3 below 55% of the season avg
# Goals are low-count and spiky (blanks are normal), so the accumulation-stat
# floors above (low>=avg*1.08, no-bust>=0.55*avg, pred>=3) can NEVER be met — no
# forward would ever be gold for goals. Goals get their own rule: a genuine
# multi-goal threat, in form, in a favourable matchup, that hasn't blanked lately.
GOAL_GOLD_PRED   = 2.0    # projected 2+ goals
GOAL_GOLD_LOW    = 1.5    # floor of >=1.5 goals (won't blank)
GOAL_GOLD_MATCH  = 1.05   # clearly favourable goal matchup for the position


def compute_gold(players):
    """Set p['statGold'] = {stat: True, ...} and p['hasGold'] for the predict UI.
    All criteria must hold per stat: prediction>=3, >=3 recent games, low band
    >= avg*GOLD_MARGIN, low >= recent-3 avg, recent-3 avg >= season avg (trend),
    no recent game below avg*GOLD_CONSISTENCY (consistency), and a favourable
    matchup (statMatch >= 1)."""
    for p in players:
        sp = p.get("statPred") or {}
        splow = p.get("statPredLow") or {}
        sm = p.get("statMatch") or {}
        rstats = p.get("roundStats") or []
        gold = {}
        for sk, rk in (("disposals", "dis"), ("kicks", "k"), ("handballs", "hb"),
                       ("marks", "mk"), ("tackles", "tk"), ("goals", "gl")):
            avg = p.get(sk) or 0
            pr, lo = sp.get(sk), splow.get(sk)
            if pr is None or lo is None or avg <= 0:
                continue
            rv = [r.get(rk) for r in rstats if r.get(rk) is not None][-3:]
            if len(rv) < 3:
                continue
            rec = sum(rv) / len(rv)
            if sk == "goals":                          # goals use their own rule
                if (pr >= GOAL_GOLD_PRED               # genuine multi-goal threat
                        and lo >= GOAL_GOLD_LOW         # floor won't blank
                        and rec >= avg                  # in form
                        and min(rv) >= 1                # kicked >=1 each of last 3
                        and sm.get(sk, 1) >= GOAL_GOLD_MATCH):  # favourable matchup
                    gold[sk] = True
                continue
            if pr < 3:                                 # accumulation stats: volume floor
                continue
            if (lo >= avg * GOLD_MARGIN              # floor clears avg by a margin
                    and lo >= rec                     # floor >= recent form
                    and rec >= avg                    # form trending up / stable
                    and min(rv) >= avg * GOLD_CONSISTENCY  # no recent bust
                    and sm.get(sk, 1) >= 1.0):         # favourable matchup
                gold[sk] = True
        if gold:
            p["statGold"] = gold
            p["hasGold"] = True
        else:
            p.pop("statGold", None)
            p["hasGold"] = False


def write_output(players, sc_players=None, dt_players=None, injuries=None, selections=None):
    """Write players.json. Safe to call with a partial list (e.g. from the crash
    handler) — source counts fall back sensibly when the extras aren't passed."""
    for _p in players:
        _a = NAME_ALIASES.get(_p.get("name"))
        if _a:
            _p["name"] = _a
    reconcile_injuries(players)
    # Enforce statPred ↔ roundStats consistency on EVERY write (incl. the crash
    # handler's partial write), so a truncated run can never serve a breakdown
    # that doesn't add up.
    try:
        reconcile_predictions(players)
    except Exception as _e:
        log.warning(f"Prediction reconcile skipped: {_e}")
    # Gold high-conviction flags (read by the predict tab's gold-only view).
    try:
        compute_gold(players)
    except Exception as _e:
        log.warning(f"Gold flag computation skipped: {_e}")
    # Backend sub-category (role) for every player, from position + profile:
    #   DEF >15 disposals -> Half Back (else Key Defender)
    #   FWD >15 disposals -> Half Forward (else Key Forward)
    #   MID with extremely low consistency (<=45) -> Winger (else Midfielder)
    for _p in players:
        _pos = (_p.get("pos") or "").upper()
        _P = [x.upper() for x in (_p.get("positions") or [])] or [_pos]
        _d = _p.get("disposals") or 0
        _c = _p.get("consistency")
        _g5 = sum(r.get("gl") or 0 for r in (_p.get("roundStats") or [])[-5:])
        if "RUC" in _P and "FWD" in _P:
            _role = "Ruck/Forward"          # ruck-forwards split time between both
        elif "FWD" in _P and len(_P) > 1:
            # dual incl. forward: >=2 goals in last 5 games => playing forward,
            # otherwise treat as their other position.
            if _g5 >= 2:
                _role = "Half Forward" if _d > 15 else "Key Forward"
            else:
                _other = next((x for x in _P if x != "FWD"), _pos)
                if _other == "DEF":
                    _role = "Half Back" if _d > 18 else "Key Defender"
                elif _other == "MID":
                    _role = "Winger" if (_c is not None and _c <= 45) else "Midfielder"
                elif _other == "RUC":
                    _role = "Ruck"
                else:
                    _role = _other
        elif _pos == "DEF":
            _role = "Half Back" if _d > 18 else "Key Defender"
        elif _pos == "FWD":
            _role = "Half Forward" if _d > 15 else "Key Forward"
        elif _pos == "MID":
            _role = "Winger" if (_c is not None and _c <= 45) else "Midfielder"
        elif _pos == "RUC":
            _role = "Ruck"
        else:
            _role = _pos
        if _role not in ("Ruck", "Ruck/Forward"):
            _hos = [(r.get("ho") or 0) for r in (_p.get("roundStats") or [])]
            if _hos and max(_hos) >= 10:   # 10+ hitouts in a match => spends time in the ruck
                _role = "Ruck/Forward"
        _p["role"] = ROLE_OVERRIDES.get(_p.get("name")) or _role
    # Inject trailing-24-month availability now that gamesBySeason is merged.
    _cur_round = max((_p.get("lastRound") or 0) for _p in players) or 1
    for _p in players:
        _r = _compute_injury_rating(_p, _cur_round)
        if _r is not None:
            _p["injuryRating"] = _r
        else:
            _p.pop("injuryRating", None)
    _cur_rnd = max((p.get("lastRound") or 0) for p in players
                   if isinstance(p.get("lastRound"), int)) if players else 0
    output = {
        "scraped_at":   datetime.now().isoformat(),
        "round":        _cur_rnd or "Current",
        "roundAccuracy": _ROUND_ACCURACY,
        "thisWeekMatchups": _THIS_WEEK_MATCHUPS,
        "season":       2026,
        "player_count": len(players),
        "sources": {
            "sc_players":  len(sc_players) if sc_players is not None else len(players),
            "dt_players":  len(dt_players) if dt_players is not None else 0,
            "injuries":    len(injuries)   if injuries   is not None else 0,
            "selections":  len(selections) if selections is not None else 0,
        },
        "players": players,
    }
    with open(OUTPUT_PATH, "w") as f:
        json.dump(output, f, indent=2)
    try:
        build_dvp(players)
    except Exception as _e:
        log.error(f"build_dvp failed: {_e}")
    return output


def main():
    print("=" * 60)
    print("  AFLFantasyWire — Footywire Data Fetcher")
    print("=" * 60)
    print(f"  {datetime.now().strftime('%H:%M:%S  %d %b %Y')}\n")

    session = make_session()

    # ── 1. Fetch SC stats (primary) ──
    log.info("Fetching SuperCoach stats from Footywire...")
    r = get(session, URLS["sc_stats"])
    if not r:
        log.error("Could not fetch SC stats. Exiting.")
        log.error("Footywire gates its stats pages behind a Turnstile CAPTCHA "
                  "(since 2026-07-03): set FOOTYWIRE_COOKIE / FOOTYWIRE_UA in "
                  ".env from a verified browser session on this machine.")
        sys.exit(1)
    sc_players = parse_sc_stats(r.text)
    if not sc_players:
        log.error("SC stats page parsed 0 players — layout may have changed. Check the URL.")
        sys.exit(1)

    time.sleep(1.5)

    # ── 2. Fetch DT stats (secondary) ──
    log.info("Fetching AFL Fantasy stats from Footywire...")
    r2 = get(session, URLS["dt_stats"])
    dt_players = parse_dt_stats(r2.text) if r2 else []

    # Build DT lookup by name
    dt_lookup = {name_key(p["name"]): p for p in dt_players}
    # Also by last name — but ONLY when that surname is unambiguous. Common
    # surnames (Smith, etc.) are shared by several players; a blind surname
    # fallback made non-playing namesakes (Kaleb/Henry/Logan Smith) inherit a
    # star's AF data (Bailey Smith, afRank 1) and rank #1 in the AF list.
    from collections import Counter as _LnCounter
    _ln_counts = _LnCounter(name_key(p["name"].split()[-1]) for p in dt_players)
    for p in dt_players:
        last = name_key(p["name"].split()[-1])
        if _ln_counts[last] == 1 and last not in dt_lookup:
            dt_lookup[last] = p

    time.sleep(1.5)

    # ── 3. Fetch injury list ──
    log.info("Fetching injury list...")
    r3 = get(session, URLS["injury_list"])
    injuries = parse_injury_list(r3.text) if r3 else {}

    time.sleep(1)

    # ── 4. Fetch selection changes ──
    log.info("Fetching selection changes...")
    r4 = get(session, URLS["selection"])
    selections = parse_selection_changes(r4.text) if r4 else {}

    time.sleep(1)

    # ── 5. Fetch SuperCoach break-evens (separate page, has BE + position) ──
    log.info("Fetching SuperCoach break-evens...")
    r_be = get(session, URLS["sc_breakevens"])
    sc_be_lookup = parse_sc_breakevens(r_be.text) if r_be else {}

    # ── 5b. SuperCoach Scores page: 3-round avg + consistency for ALL players ──
    # One fetch covers every player, so the waiver band (which we don't fetch
    # per-game logs for) still gets real form numbers.
    log.info("Fetching SuperCoach Scores (3-rnd avg + consistency)...")
    r_scs = get(session, SC_SCORES_URL)
    sc_scores_lookup = parse_sc_scores(r_scs.text) if r_scs else {}

    # ── 5c. Per-round SuperCoach scores (last 8 rounds) — a few page fetches
    # cover every player, giving a real score series for 3/5-round form,
    # consistency and sparklines on players we don't fetch game logs for.
    # We pull 8 rounds (not just 5) so a player who's had a bye or missed a
    # game still has the most recent 5 *played* scores — otherwise the UI's
    # 5-round average collapses to the 3-round one (they look identical). ──
    log.info("Fetching per-round SuperCoach scores (last 8 rounds)...")
    SC_ROUND_BASE = "https://www.footywire.com/afl/footy/supercoach_round"
    _yr = datetime.now().year
    r_cur = get(session, SC_ROUND_BASE)
    cur_rnd, _curmap = parse_sc_round(r_cur.text) if r_cur else (0, {})
    _round_scores = {}   # name_key -> {round: score}
    if cur_rnd:
        for nk, sco in _curmap.items():
            _round_scores.setdefault(nk, {})[cur_rnd] = sco
        for rnd in range(max(1, cur_rnd - 7), cur_rnd):
            rr = get(session, f"{SC_ROUND_BASE}?year={_yr}&round={rnd}")
            if not rr:
                continue
            _, m = parse_sc_round(rr.text)
            for nk, sco in m.items():
                _round_scores.setdefault(nk, {})[rnd] = sco
            time.sleep(0.5)
    # Build a fixed last-8-round window per player, using 0 for byes / rounds
    # not played, so the round-by-round bar chart still shows those as 0-height
    # blocks. Averages downstream filter the zeros out, so byes don't drag form.
    _rwin = list(range(max(1, cur_rnd - 7), cur_rnd + 1)) if cur_rnd else []
    sc_round_lookup = {nk: [d.get(r, 0) for r in _rwin] for nk, d in _round_scores.items()}
    log.info(f"Per-round SC scores: {len(sc_round_lookup)} players over last 8 rounds")

    # Merge BE + position into each sc_player by name_key
    for p in sc_players:
        nk = name_key(p["name"])
        be_data = sc_be_lookup.get(nk) or sc_be_lookup.get(name_key(p["name"].split()[-1]))
        if be_data:
            p["sc_be"] = be_data["be"]
            if be_data["pos"]:
                p["pos"] = be_data["pos"]
            if be_data.get("positions"):
                p["sc_positions"] = be_data["positions"]
            # Prefer breakevens price if season price was 0 (rare)
            if not p["sc_price"] and be_data["price"]:
                p["sc_price"] = be_data["price"]

    # Add long-tail players that only appear on the break-even page (not in the
    # season-stats top list) so rankings cover the FULL player pool, not just the
    # top ~600. They carry avg/price/BE/position; detailed game-log stats fill in
    # for any that later make the games-log cap.
    _have = {name_key(p["name"]) for p in sc_players}
    _added = 0
    for _bd in sc_be_lookup.values():
        if not isinstance(_bd, dict) or not _bd.get("name"):
            continue
        if name_key(_bd["name"]) in _have:
            continue
        _have.add(name_key(_bd["name"]))
        sc_players.append({
            "name":        _bd["name"],
            "team":        normalise_team(_bd.get("team", "")),
            "pos":         _bd.get("pos") or (_bd.get("positions") or ["MID"])[0],
            "sc_positions": _bd.get("positions") or [],
            "sc_avg":      _bd.get("avg") or 0,
            "sc_price":    _bd.get("price") or 0,
            "sc_be":       _bd.get("be") or 0,
            "games":       _bd.get("games") or 0,
            "_from_be":    True,
        })
        _added += 1
    log.info(f"Added {_added} break-even-only players (full rankings)")

    # Fallback: if BE couldn't be scraped, approximate from price and average
    # (rough — better than showing 0). be ~= price / (avg * 0.8).
    for p in sc_players:
        if not p.get("sc_be"):
            avg = p.get("sc_avg") or 0
            price = p.get("sc_price") or 0
            p["sc_be"] = round(price / (avg * 0.8)) if avg > 0 and price > 0 else 0

    # Diagnostic: confirm break-evens captured for the first 5 players
    log.info("Break-even check (first 5 players):")
    for p in sc_players[:5]:
        log.info(f"  {p['name']:22} price={p.get('sc_price')} be={p.get('sc_be')} "
                 f"avg={p.get('sc_avg')} avg3={p.get('sc_avg3')}")

    time.sleep(1)

    # ── 6. Fetch per-player game-log pages for round-by-round SC/AF scores ──
    # Footywire's "pg-" page (Player Games) is richer than the "pu-" profile —
    # it gives BOTH the SuperCoach and AFL Fantasy score per round, plus full
    # disposals/marks/goals/tackles/clearances per game.
    # Keep the games-log phase short so fetch_data never hits the 20-min
    # subprocess timeout. Only the top players get per-round form; a hard time
    # cap stops the phase early when Footywire is slow or rate-limiting.
    # Bumped to cover all players from games played in the current round.
    # 4 teams × ~22 players per game = ~88 game participants per fixture day;
    # top-50 missed most of them outside the elite players. 300 covers the
    # tail (e.g. a fringe player who scores 100). 10-min cap keeps the
    # phase from blowing past the 20-min auto_scrape timeout if Footywire
    # is slow. Effective rate is ~2 s/page through the polite delay in
    # `get()`, so 10 min ≈ 300 logs.
    MAX_GAMES_LOG = 420
    GAMES_LOG_TIME_LIMIT = 780  # 13 minutes max
    # Per-stat conceded profiles: _conc_stat[team][POS][stat] = raw stat values
    # (disposals/kicks/etc.) recorded by POS players against that team. Powers
    # per-stat matchup factors (e.g. a team that bleeds disposals).
    _conc_stat = {}
    # Points-conceded accumulators for the schedule rating. _conc_all[team] is
    # every SC score posted against that team; _conc_pos[team][POS] splits it by
    # the scorer's position (DvP). Built from the games-log opponents below.
    _conc_all, _conc_pos = {}, {}
    # Prioritise players who just played so a crawl that hits its time cap still
    # captures the newest game stats FIRST. Two signals, combined:
    #   1) Footywire's per-round page (_curmap) — who already has a fresh score.
    #   2) the AFL fixture API — teams whose latest-round game is CONCLUDED.
    # Signal 2 is the reliable one: Footywire's default per-round page often still
    # shows the last COMPLETE round mid-round, so just-played teams were missing
    # from _curmap, dropped below the cap, and carried forward STALE — the bug
    # where most of a round's players didn't update. Failing the AFL call just
    # falls back to the _curmap signal.
    _played_now = set(_curmap.keys()) if _curmap else set()
    _jp_teams = set()
    try:
        for _r in (max(1, cur_rnd or 1), max(1, cur_rnd or 1) + 1):
            _jr = get(session,
                      f"https://aflapi.afl.com.au/afl/v2/matches?compSeasonId={AFL_API_SEASON_ID}"
                      f"&roundNumber={_r}&pageSize=20", retries=1, timeout=10)
            if not _jr:
                continue
            for _m in (_jr.json().get("matches", []) or []):
                if (_m.get("status") or "").upper() != "CONCLUDED":
                    continue
                for _side in ("home", "away"):
                    _tn = normalise_team(((_m.get(_side) or {}).get("team") or {}).get("name") or "")
                    if _tn and _tn != "Unknown":
                        _jp_teams.add(_tn)
    except Exception as _e:
        log.warning(f"Just-played team detection failed: {_e}")
    if _played_now or _jp_teams:
        def _jp_prio(_p):
            return 0 if (name_key(_p.get("name", "")) in _played_now
                         or normalise_team(_p.get("team", "")) in _jp_teams) else 1
        sc_players.sort(key=_jp_prio)
        log.info(f"Games-log priority: {sum(1 for _p in sc_players[:MAX_GAMES_LOG] if _jp_prio(_p)==0)} "
                 f"just-played players moved to the front ({len(_jp_teams)} concluded teams via AFL fixture)")
    log.info(f"Fetching games log for top {MAX_GAMES_LOG} players (pg- URL)...")
    games_log_start = time.time()
    for i, p in enumerate(sc_players[:MAX_GAMES_LOG]):
        if time.time() - games_log_start > GAMES_LOG_TIME_LIMIT:
            log.warning(f"Games-log fetch exceeded {GAMES_LOG_TIME_LIMIT//60} min — stopping early, using data so far")
            break
        # One player's games-log page failing (network blip, an unexpected table
        # layout, a parse error) must never abort the whole scrape. On failure we
        # log the full traceback and skip just this player's games-log — the SC/DT
        # price, average and break-even already parsed from the main stats page
        # stay intact, so the player still appears in players.json.
        try:
            pu_url = p.get("profile_url", "")
            if not pu_url:
                continue
            # Swap the /pu- prefix for /pg- to hit the games-log page
            pg_url = pu_url.replace("/pu-", "/pg-")
            r5 = get(session, pg_url, retries=1, timeout=8)
            if not r5:
                continue

            games = parse_player_games(r5.text)

            if games["pos"]:
                p["pos"] = games["pos"]

            # Fantasy scores exclude Round 0 (not a fantasy round); raw-stat
            # averages below still use every round.
            _fr = games.get("sc_rounds") or []
            _isF = lambda i: (i >= len(_fr) or _fr[i] >= 1)
            sc_played = [s for i, s in enumerate(games["sc_scores"]) if s is not None and s > 0 and _isF(i)]
            af_played = [s for i, s in enumerate(games["af_scores"]) if s is not None and s > 0 and _isF(i)]
            p["sc_all_scores"] = sc_played
            p["dt_all_scores"] = af_played
            # Fantasy games played this season (R0 + byes/DNPs excluded) —
            # numerator for availability.
            p["gamesPlayed"] = len(sc_played)

            # Full round-by-round line for the player profile.
            rs, gr = [], (games.get("sc_rounds") or [])
            for idx in range(len(games["sc_scores"])):
                sc_s = games["sc_scores"][idx]
                if sc_s is None:
                    continue
                def _g(key, ix=idx):
                    arr = games.get(key) or []
                    return arr[ix] if ix < len(arr) and arr[ix] is not None else 0
                _opps = games.get("opponents") or []
                _o = _opps[idx] if idx < len(_opps) else None
                rs.append({"r": gr[idx] if idx < len(gr) else f"R{idx+1}",
                           "sc": sc_s, "dt": _g("af_scores"), "dis": _g("disposals"),
                           "mk": _g("marks"), "tk": _g("tackles"), "gl": _g("goals"), "b": _g("behinds"),
                           "k": _g("kicks"), "hb": _g("handballs"), "ho": _g("hitouts"), "opp": _o})
                # Attribute this score to the opponent that conceded it (DvP).
                if _o and sc_s and sc_s > 0:
                    _pp = (p.get("pos") or "MID").upper()
                    _rnd0 = gr[idx] if idx < len(gr) else 1
                    # Disregard games where this opponent had a different coach.
                    if _coach_valid_2026(_o, _rnd0):
                        if isinstance(_rnd0, int) and _rnd0 >= 1:  # SC pts = fantasy rounds only
                            _conc_all.setdefault(_o, []).append(sc_s)
                            _conc_pos.setdefault(_o, {}).setdefault(_pp, []).append(sc_s)
                        # Raw-stat conceded profiles include Round 0.
                        for _sk in ("disposals", "kicks", "handballs", "marks", "tackles", "behinds", "goals"):
                            _conc_stat.setdefault(_o, {}).setdefault(_pp, {}).setdefault(_sk, []).append(_g(_sk))
            p["round_stats"] = rs

            # Last 7 SC scores (right-pad with 0s if fewer played games)
            if len(sc_played) >= 7:
                p["sc_scores"] = sc_played[-7:]
            elif sc_played:
                p["sc_scores"] = sc_played + [0] * (7 - len(sc_played))
            else:
                p["sc_scores"] = [0] * 7

            if len(af_played) >= 7:
                p["dt_scores"] = af_played[-7:]
            elif af_played:
                p["dt_scores"] = af_played + [0] * (7 - len(af_played))
            else:
                p["dt_scores"] = [0] * 7

            p["sc_last"] = sc_played[-1] if sc_played else 0
            p["dt_last"] = af_played[-1] if af_played else 0

            sc_last3 = sc_played[-3:]
            p["sc_avg3"] = round(sum(sc_last3) / len(sc_last3), 1) if sc_last3 else p["sc_avg"]
            if af_played:
                p["dt_avg"]  = round(sum(af_played) / len(af_played), 1)
                af_last3 = af_played[-3:]
                p["dt_avg3"] = round(sum(af_last3) / len(af_last3), 1)

            # Per-game stat averages over actual played rounds
            def avg_of(key):
                vals = [v for v in games[key] if v is not None]
                return round(sum(vals) / len(vals), 1) if vals else 0
            p["disposals"]  = avg_of("disposals")
            p["marks"]      = avg_of("marks")
            p["goals"]      = avg_of("goals")
            p["behinds"]    = avg_of("behinds")
            p["kicks"]      = avg_of("kicks")
            p["handballs"]  = avg_of("handballs")
            p["tackles"]    = avg_of("tackles")
            p["hitouts"]    = avg_of("hitouts")
            p["clearances"] = avg_of("clearances")
        except Exception as e:
            log.error(f"Games-log fetch failed for {p.get('name', '?')} "
                      f"({p.get('profile_url', '')}): {e}")
            log.error(traceback.format_exc())
            # Skip this player's games-log; keep the main-stats score data.
            continue
        finally:
            if i % 25 == 24:
                log.info(f"  {i+1}/{MAX_GAMES_LOG} games-log pages fetched")
            time.sleep(0.5)

    # ── 6b. Fetch AFL Fantasy Classic ownership ──
    log.info("Fetching AFL Fantasy Classic ownership...")
    classic_lookup = fetch_classic_ownership(session)
    for p in sc_players:
        nk = name_key(p["name"])
        # Strict first+last match — but only trust it when teams agree, because
        # AFL Classic distinguishes Bailey J. Williams (West Coast) from Bailey
        # Williams (Bulldogs) on the FIRST-name side; the Footywire "Bailey
        # Williams" with no middle initial therefore strict-key-matches the
        # Bulldogs entry regardless of which Eagle/Doggie we're processing.
        # When the strict hit's team disagrees with the Footywire team, fall
        # through to the (last_name, team) tuple key instead.
        ft_team = normalise_team(p.get("team", ""))
        _ft_parts = p["name"].split()
        _ft_first = _ft_parts[0] if _ft_parts else ""
        _ft_last  = _ft_parts[-1] if _ft_parts else ""
        co_strict = classic_lookup.get(nk)
        co_team = classic_lookup.get((name_key(_ft_first),
                                      name_key(_ft_last), ft_team))
        # Prefer team-matched entry. If strict-key entry has a wrong team AND
        # there's no team-keyed sibling on our team, we still take the strict
        # entry's POSITIONS — players who change clubs mid-season (Petracca
        # → Gold Coast 2026 while AFL Classic still lists Melbourne) keep
        # their dual-position eligibility regardless of squad. Ownership /
        # price come from the strict entry too; team is from Footywire.
        co = co_team or co_strict
        if co:
            p["classic_owned"] = co["classic_owned"]
            p["classic_avg"]   = co["classic_avg"]
            p["classic_avg3"]  = co["classic_avg3"]
            p["classic_proj"]  = co["classic_proj"]
            p["classic_price"] = co["classic_price"]
            # Keep AFL Fantasy Classic positions in a SEPARATE field — SC
            # (Footywire) and AFL Fantasy (Classic) disagree on dual
            # eligibility (e.g. Bailey Smith is MID-only in SC but MID/FWD
            # in Classic; Petracca is dual in Classic, MID-only in SC).
            # The site's game toggle picks the right list at filter time.
            if co.get("classic_positions"):
                p["aflf_positions"] = co["classic_positions"]

    # ── 7. Merge and build final player list ──
    log.info("Merging data sources...")
    players = []
    for i, sc in enumerate(sc_players, 1):  # full pool (was capped at 600)
        nk  = name_key(sc["name"])
        nk_last = name_key(sc["name"].split()[-1])
        dt  = dt_lookup.get(nk) or dt_lookup.get(nk_last)
        player = build_player(sc, dt, injuries, selections, i)
        # Apply team-scoped SC position overrides (manual corrections where
        # Footywire's playerflag is wrong/stale).
        ov = SC_POSITION_OVERRIDES.get((player["name"], player["team"]))
        if ov:
            player["positions"] = list(ov)
            player["pos"] = ov[0]
        players.append(player)

    # ── 7a. Schedule rating — hybrid DvP + overall points conceded ──
    # For each player's next 5 fixtures, rate how favourable the matchup is:
    # blend how many SC points the opponent concedes to the player's POSITION
    # (60%, DvP) with how many it concedes overall (40%), normalised league-wide
    # to 1-10 where 10 = easiest matchup (concedes the most → good to own/hold).
    try:
        _cr = max((pp.get("lastRound") or 0) for pp in players) or 1
        # Per-team next round: each team's "next game" starts at ITS OWN last
        # played round + 1, so a team mid-round shows its current-round game
        # rather than next round.
        _team_last = {}
        for pp in players:
            _tt = normalise_team(pp.get("team", ""))
            _lr = pp.get("lastRound") or 0
            if _tt and _lr > _team_last.get(_tt, 0):
                _team_last[_tt] = _lr
        _fx = fetch_upcoming_fixture(session, _team_last, n=5)
        try:
            global _THIS_WEEK_MATCHUPS, _LOCK_ROUND, _LOCK_TEAMS
            _THIS_WEEK_MATCHUPS = fetch_current_round_fixture(session, _cr)
            if _THIS_WEEK_MATCHUPS:
                _LOCK_ROUND = _THIS_WEEK_MATCHUPS.get("round")
                _LOCK_TEAMS = set(_THIS_WEEK_MATCHUPS.get("started") or [])
        except Exception as _e:
            log.warning(f"Current-round fixture failed: {_e}")
        if not _THIS_WEEK_MATCHUPS:
            # Transient AFL-API miss — keep the last known fixture rather than
            # nulling it (which makes the frontend fall back to next round).
            try:
                _pf = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
                _THIS_WEEK_MATCHUPS = _pf.get("thisWeekMatchups") if isinstance(_pf, dict) else None
                if _THIS_WEEK_MATCHUPS:
                    log.info("Current-round fixture: kept last known (API miss this run)")
            except Exception:
                pass
        # Team strength factor for the projected score: blend of team fantasy
        # output (mean of its players' SC averages vs the league) and recent
        # win/loss form. Kept to a gentle ~0.9-1.1 multiplier so it nudges, not
        # dominates (the player's own avg already reflects their team somewhat).
        _form = fetch_recent_form(session, _cr, n=5)
        if WITH_HISTORY:
            # Explicit request: re-scrape last season and refresh the gold cache.
            try:
                fetch_dvp_2025(session, sc_players)
            except Exception as _e:
                log.error(f"2025 DvP fetch failed: {_e}")
        else:
            # Default: load the permanent cache, never hit Footywire for history.
            load_dvp_2025_cache()
        _ptsc = fetch_points_conceded(session, _cr)
        _lg_pts = (sum(_ptsc.values()) / len(_ptsc)) if _ptsc else 0
        _trp = fetch_team_rounds_played(session, _cr)
        _tavg = {}
        for _pp in players:
            _tavg.setdefault(normalise_team(_pp.get("team", "")), []).append(_pp.get("scAvg") or 0)
        _fo = {t: sum(v) / len(v) for t, v in _tavg.items() if v}
        _lfo = (sum(_fo.values()) / len(_fo)) if _fo else 1
        for _pp in players:
            _t = normalise_team(_pp.get("team", ""))
            _foN = (_fo.get(_t, _lfo) / _lfo) if _lfo else 1
            _foF = 1 + max(-0.12, min(0.12, (_foN - 1) * 0.6))
            _wr = _form.get(_t)
            _formF = 1 + (_wr - 0.5) * 0.10 if _wr is not None else 1
            _pp["teamFactor"] = round(max(0.9, min(1.1, 0.5 * _foF + 0.5 * _formF)), 3)
            _rp = _trp.get(_t, 0)
            if _fx.get("_r1"):
                _pp["byeNext"] = _t not in _fx["_r1"]
            _pp["nextRound"] = _fx.get("_next", {}).get(_t)
            _pp["teamRoundsPlayed"] = _rp
            _gp = _pp.get("gamesPlayed")
            if _rp > 0 and _gp is not None:
                _pp["availability"] = round(min(1.0, _gp / _rp), 3)
        log.info(f"Team form: {len(_form)} teams; rounds-played for {len(_trp)} teams; teamFactor on {len(players)} players")
        log.info(f"Schedule: per-team fixture for {len(_fx.get('_next', {}))} teams "
                 f"(next rounds {sorted(set(_fx.get('_next', {}).values()))[:3]}); "
                 f"conceded data for {len(_conc_all)} teams")
        _all_mean = {t: sum(v) / len(v) for t, v in _conc_all.items() if v}
        _pos_mean = {t: {pp: sum(vs) / len(vs) for pp, vs in d.items() if vs}
                     for t, d in _conc_pos.items()}
        _avals = list(_all_mean.values())
        _amin, _amax = (min(_avals), max(_avals)) if _avals else (0, 1)
        _pcoll = {}
        for _t, _d in _pos_mean.items():
            for _pp, _m in _d.items():
                _pcoll.setdefault(_pp, []).append(_m)
        _pspread = {pp: (min(v), max(v)) for pp, v in _pcoll.items() if len(v) > 1}
        # Per-game opponent difficulty (0-100, high = tough) from SC points
        # conceded by the opponent to the player's position vs the position avg.
        _lg_pos = {}
        for _t2, _d2 in _pos_mean.items():
            for _pp2, _m2 in _d2.items():
                _lg_pos.setdefault(_pp2, []).append(_m2)
        _lg_pos = {pp2: sum(v) / len(v) for pp2, v in _lg_pos.items() if v}
        for _pp in players:
            _ppos = (_pp.get("pos") or "MID").upper()
            _lgsc = _lg_pos.get(_ppos)
            if not _lgsc:
                continue
            for _r in (_pp.get("roundStats") or []):
                _o = _r.get("opp")
                _osc = _pos_mean.get(_o, {}).get(_ppos) if _o else None
                if _osc:
                    _r["od"] = max(0, min(100, round((1.15 - _osc / _lgsc) / 0.30 * 100)))
        def _n01(x, lo, hi):
            return 0.5 if hi <= lo else max(0.0, min(1.0, (x - lo) / (hi - lo)))
        # Per-stat opposition profiles: mean raw stat conceded by team -> position
        _cs_mean = {}
        for _t, _pd in _conc_stat.items():
            for _pp2, _sd in _pd.items():
                for _sk, _vals in _sd.items():
                    if _vals:
                        _cs_mean.setdefault(_t, {}).setdefault(_pp2, {})[_sk] = sum(_vals) / len(_vals)
        _lg = {}
        for _t, _pd in _cs_mean.items():
            for _pp2, _sd in _pd.items():
                for _sk, _m in _sd.items():
                    _lg.setdefault(_pp2, {}).setdefault(_sk, []).append(_m)
        _lg_mean = {pp2: {sk: sum(v) / len(v) for sk, v in sd.items()} for pp2, sd in _lg.items()}
        # 2025 historical means for a light matchup blend
        _cs_mean_2025, _cs_n_2025 = {}, {}
        for _t, _pd in _DVP_2025.items():
            for _pp2, _sd in _pd.items():
                _cs_n_2025.setdefault(_t, {})[_pp2] = len(_sd.get("disposals") or [])
                for _sk, _vals in _sd.items():
                    if _vals:
                        _cs_mean_2025.setdefault(_t, {}).setdefault(_pp2, {})[_sk] = sum(_vals) / len(_vals)
        _lg2 = {}
        for _t, _pd in _cs_mean_2025.items():
            for _pp2, _sd in _pd.items():
                for _sk, _m in _sd.items():
                    _lg2.setdefault(_pp2, {}).setdefault(_sk, []).append(_m)
        _lg_mean_2025 = {pp2: {sk: sum(v) / len(v) for sk, v in sd.items()} for pp2, sd in _lg2.items()}
        _STAT_KEYS = ("disposals", "kicks", "handballs", "marks", "tackles", "behinds", "goals")
        _RK = {"disposals": "dis", "kicks": "k", "handballs": "hb", "marks": "mk", "tackles": "tk", "behinds": "b", "goals": "gl"}
        # Elite-tag multipliers from the AFL match feed: a few teams (Geelong/North/
        # Bulldogs) suppress the opposition's best ball-winners ~5-6% beyond normal
        # position defence. Only the TAG table is used — the position-share tables the
        # module also builds were backtested as LESS accurate and are not shipped.
        _TAGS = {}
        try:
            import afl_team_tables as _att
            _pmap = {_att.nkey(_p.get("name", "")): (_p.get("pos") or "MID") for _p in players}
            _TAGS = _att.build_tables(_pmap).get("tags", {})
            log.info(f"Tag table: {len(_TAGS)} opponents (elite-suppression) from AFL feed")
        except Exception as _e:
            log.warning(f"Tag table build failed: {_e}")
        TAG_STATS = ("disposals", "kicks", "handballs")   # tagging hits possessions
        TAG_MIN_AVG = 24                                   # only genuine accumulators
        for pp in players:
            _T = normalise_team(pp.get("team", ""))
            _opps = _fx.get(_T, [])
            _P = (pp.get("pos") or "MID").upper()
            _o0 = _opps[0] if _opps else None
            _sm = {}
            if _o0:
                for _sk in _STAT_KEYS:
                    _oppm = _cs_mean.get(_o0, {}).get(_P, {}).get(_sk)
                    _lgm = _lg_mean.get(_P, {}).get(_sk)
                    if _oppm and _lgm and _lgm > 0:
                        _curf = _oppm / _lgm
                        _hf = None
                        if _o0 not in COACH_CHANGED_TEAMS:
                            _h_o = _cs_mean_2025.get(_o0, {}).get(_P, {}).get(_sk)
                            _h_l = _lg_mean_2025.get(_P, {}).get(_sk)
                            _h_n = _cs_n_2025.get(_o0, {}).get(_P, 0)
                            if _h_o and _h_l and _h_l > 0 and _h_n >= 15:
                                _hf = _h_o / _h_l
                        _bf = (0.8 * _curf + 0.2 * _hf) if _hf is not None else _curf
                        _sm[_sk] = round(max(0.85, min(1.15, _bf)), 3)
                if _sm:
                    pp["statMatch"] = _sm
                # Team-level points-conceded nudge: a leaky defence (concedes
                # more match points than the league avg) lifts the goal/behind
                # forecast for the player it is up against next round.
                if _lg_pts > 0 and _ptsc.get(_o0):
                    _leak = max(0.85, min(1.20, _ptsc[_o0] / _lg_pts))
                    for _gs in ("goals", "behinds"):
                        _bm = _sm.get(_gs, 1.0)
                        _sm[_gs] = round(max(0.8, min(1.28, 0.4 * _bm + 0.6 * _leak)), 3)
                    pp["statMatch"] = _sm
                    pp["oppPtsConceded"] = round(_ptsc[_o0], 1)
                    pp["oppPtsLeak"] = round(_leak, 3)
            # Per-stat predicted next round: recent-weighted base x per-stat matchup x team
            _sp = {}
            _tf = pp.get("teamFactor") or 1
            for _sk in _STAT_KEYS:  # behinds before goals so its forecast feeds goals
                _savg = pp.get(_sk) or 0
                # Goals gets a bonus from the (already-forecast) behinds at 3 behinds = 1 goal.
                _gbonus = (_sp.get("behinds", 0) or 0) / 3.0 if _sk == "goals" else 0
                if not _savg and not _gbonus:
                    continue
                _rs3 = [r.get(_RK[_sk]) for r in (pp.get("roundStats") or []) if r.get(_RK[_sk]) is not None][-3:]
                _a3 = sum(_rs3) / len(_rs3) if _rs3 else _savg
                _base = 0.55 * _a3 + 0.45 * _savg
                _sp[_sk] = round(_base * _sm.get(_sk, 1) * _tf + _gbonus, 1)
            if _sp:
                pp["statPred"] = _sp
                pp.pop("teamWt", None)   # legacy redistribution field — no longer used
                # Low range per stat: one standard deviation below the prediction
                # (from the player's game-to-game spread), so the predict UI can
                # shade a band — light green within range, strong green above.
                _splow = {}
                for _sk, _v in _sp.items():
                    _dvals = [r.get(_RK[_sk]) for r in (pp.get("roundStats") or []) if r.get(_RK[_sk]) is not None]
                    _dv = LOW_RANGE_K * _sigma(_dvals) if len(_dvals) >= 3 else max(0.8, _v * 0.15)
                    _splow[_sk] = max(0, round(_v - _dv, 1))
                pp["statPredLow"] = _splow
                # Elite-tag downgrade: top accumulators (disposal avg >= TAG_MIN_AVG)
                # lose a few % of possessions vs teams that tag, on disposals/kicks/
                # handballs only. Stored as tagWt so reconcile_predictions reproduces it.
                _tg = (_TAGS.get(_o0) or {}).get("disposals") if _o0 else None
                if _tg and _tg < 1 and (pp.get("disposals") or 0) >= TAG_MIN_AVG:
                    _tw = {}
                    for _ts in TAG_STATS:
                        if _sp.get(_ts) is not None:
                            _sp[_ts] = round(_sp[_ts] * _tg, 1)
                            _tw[_ts] = _tg
                        if _splow.get(_ts) is not None:
                            _splow[_ts] = round(_splow[_ts] * _tg, 1)
                    if _tw:
                        pp["tagWt"] = {"mult": _tg, "opp": _o0, "stats": _tw}
                # Value pick: the projected SuperCoach floor (proj minus
                # LOW_RANGE_K sigma of the player's SC scores) sits at or above
                # their season average — a high, reliable floor.
                _scvals = [r.get("sc") for r in (pp.get("roundStats") or []) if r.get("sc")]
                _scavg = pp.get("scAvg") or 0
                if _scavg and len(_scvals) >= 4:
                    _scproj = 0.55 * (pp.get("scAvg3") or _scavg) + 0.45 * _scavg
                    _scfloor = _scproj - LOW_RANGE_K * _sigma(_scvals)
                    pp["scProj"] = round(_scproj)
                    pp["scProjFloor"] = round(_scfloor)
                    pp["valuePick"] = bool(_scfloor >= _scavg)
            # Form-vs-opposition signal: judge recent trend against the toughness
            # of the last 3 opponents faced (tough = concedes few SC points).
            _rs = pp.get("roundStats") or []
            _ropp = [r.get("opp") for r in _rs[-3:] if r.get("opp")]
            _tough = None
            if _ropp:
                _ts = [1 - _n01(_all_mean[o], _amin, _amax) for o in _ropp if o in _all_mean]
                if _ts:
                    _tough = sum(_ts) / len(_ts)
            _sa = pp.get("scAvg") or 0
            _delta = (pp.get("scAvg3") or _sa) - _sa
            if _tough is not None and _sa > 0:
                _dt = _sa * 0.06
                _sig = ("buy"  if (_delta < -_dt and _tough > 0.55)
                        else "sell" if (_delta > _dt and _tough < 0.45)
                        else "hold" if (_delta >= -_dt and _tough > 0.55)
                        else None)
                if _sig:
                    pp["formSignal"] = {"sig": _sig, "tough": round(_tough, 2), "trend": round(_delta, 1)}
            _ratings = []
            for _opp in _opps[:5]:
                _parts, _ws = [], []
                _dv = _pos_mean.get(_opp, {}).get(_P)
                if _dv is not None and _P in _pspread:
                    _parts.append(_n01(_dv, *_pspread[_P])); _ws.append(0.6)
                _ov = _all_mean.get(_opp)
                if _ov is not None:
                    _parts.append(_n01(_ov, _amin, _amax)); _ws.append(0.4)
                if _parts:
                    _blend = sum(a * w for a, w in zip(_parts, _ws)) / sum(_ws)
                    _ratings.append(round(1 + _blend * 9))
                else:
                    _ratings.append(5)
            if _ratings:
                pp["scheduleRating"] = _ratings
                pp["scheduleOpp"] = [_TEAM_ABBR.get(_opp, _opp[:3].upper()) for _opp in _opps[:5]]

        # ── Team-total weighting: REMOVED ──
        # A top-down redistribution (rescale each player's stat predictions so a
        # team's lineup sums to a 'team budget') used to live here and produced the
        # teamWt factor. Walk-forward backtesting showed redistribution is LESS
        # accurate than each player's own bottom-up projection — worse even in the
        # games-with-outs case it was meant to help — so it was removed. Predictions
        # are now (0.55*last3 + 0.45*season) x matchup x teamFactor x elite-tag
        # (see _TAGS above). No teamWt is produced.
    except Exception as _e:
        log.error(f"Schedule rating failed: {_e}")
        log.error(traceback.format_exc())

    # ── Prediction persistence ──
    # A partial games-log scrape (Footywire throttling/timeout) leaves players
    # past the fetch frontier without roundStats -> no statPred -> they vanish
    # from the predictions page and their profile goes blank, so predictions
    # look like they "change day to day". Carry forward the last good games-log
    # data from the previous players.json for any still-listed, non-out/bye
    # player this run failed to compute. Never overwrites freshly computed data.
    try:
        _prev = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
        _prevp = _prev if isinstance(_prev, list) else _prev.get("players", _prev)
        _prior = {name_key(p.get("name", "")): p for p in _prevp if p.get("name")}
        _carried = 0
        for _pp in players:
            if _pp.get("statPred") and _pp.get("roundStats"):
                continue  # fully computed this run
            if _pp.get("injuryStatus") == "out" or _pp.get("byeNext"):
                continue  # legitimately not predicted
            _old = _prior.get(name_key(_pp.get("name", "")))
            if not _old:
                continue
            _did = False
            if not _pp.get("statPred") and _old.get("statPred"):
                _pp["statPred"] = _old["statPred"]
                if _old.get("statPredLow"):
                    _pp["statPredLow"] = _old["statPredLow"]
                if _old.get("tagWt"):
                    _pp["tagWt"] = _old["tagWt"]
                if _old.get("statMatch"):
                    _pp["statMatch"] = _old["statMatch"]
                _did = True
            if not _pp.get("roundStats") and _old.get("roundStats"):
                _pp["roundStats"] = _old["roundStats"]
                for _k in ("gamesPlayed", "scores", "dtScores"):
                    if _old.get(_k) not in (None, [], {}):
                        _pp[_k] = _old[_k]
                _did = True
            # Carry the Footywire display/filter fields too. Without these a
            # partial scrape (game-logs/averages dropped) blanks the player out
            # of the predictions page even though its prediction persisted — the
            # frontend filters on lastRound + season-average stats.
            for _k in ("lastRound", "lastScore", "gamesBySeason", "disposals",
                       "kicks", "handballs", "marks", "tackles", "goals",
                       "behinds", "hitouts", "clearances", "scAvg", "scAvg3",
                       "dtAvg", "dtAvg3", "dtLast", "dtBe", "classicAvg",
                       "classicAvg3", "classicProj", "classicOwned",
                       "classicPrice", "price", "priceDelta", "prices",
                       "breakeven", "pos", "positions", "aflfPositions", "role",
                       "owned", "ownedDelta", "consistency", "ceiling", "floor",
                       "afRank", "rank"):
                if _pp.get(_k) in (None, 0, "", [], {}) and _old.get(_k) not in (None, 0, "", [], {}):
                    _pp[_k] = _old[_k]
                    _did = True
            if _did:
                _pp["_carried"] = True
                _carried += 1
        if _carried:
            log.info(f"Prediction persistence: carried forward data for {_carried} "
                     f"player(s) not refreshed this run")
    except Exception as _e:
        log.warning(f"Prediction persistence skipped: {_e}")

    try:
        _cal = log_predictions(players, max((pp.get("lastRound") or 0) for pp in players) or 1)
        # Apply the learned per-stat correction to fresh predictions (the raw
        # values were already logged above, so scoring stays honest). Carried
        # players keep their already-calibrated value — don't double-apply.
        if _cal:
            _rk = {"disposals": "dis", "kicks": "k", "handballs": "hb",
                   "marks": "mk", "tackles": "tk", "goals": "gl"}
            _n = 0
            for _pp in players:
                _sp = _pp.get("statPred")
                if not _sp or _pp.get("_carried"):
                    continue
                _splow = _pp.get("statPredLow") or {}
                for _sk, _f in _cal.items():
                    if _sp.get(_sk) is not None:
                        _sp[_sk] = round(_sp[_sk] * _f, 1)
                    if _splow.get(_sk) is not None:
                        _splow[_sk] = round(_splow[_sk] * _f, 1)
                _pp["calibrated"] = True
                _n += 1
            log.info(f"Applied model calibration {_cal} to {_n} players' predictions")
    except Exception as _e:
        log.error(f"Prediction logging/calibration failed: {_e}")

    # ── Kick-off display lock ──
    # Once a player's UPCOMING game has started, freeze the DISPLAYED prediction
    # (number + low range) to the last pre-kick-off value so featured / most-likely
    # tiles can't drift mid-game. log_predictions already freezes the accuracy log
    # at kick-off; this mirrors that freeze in what the UI actually shows. We pin to
    # the previous players.json value (the last shown, already-calibrated number),
    # NOT the raw logged bucket, so the locked tile doesn't jump. Predictions for
    # rounds that haven't kicked off keep recomputing each scrape as normal.
    try:
        if _LOCK_ROUND and _LOCK_TEAMS:
            _lprev = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
            _lprevp = _lprev if isinstance(_lprev, list) else _lprev.get("players", _lprev)
            _lprior = {name_key(p.get("name", "")): p for p in _lprevp if p.get("name")}
            _frozen = 0
            for _pp in players:
                # Lock once the player's team is mid-round (its game has kicked
                # off). We key on team-in-_LOCK_TEAMS, NOT nextRound==_LOCK_ROUND:
                # the AFL fixture advances a team's nextRound the moment its game
                # starts, so the old nextRound check skipped exactly the players
                # whose game had just begun — predictions then drifted all game.
                if _pp.get("team") not in _LOCK_TEAMS:
                    continue  # their game hasn't kicked off
                _old = _lprior.get(name_key(_pp.get("name", "")))
                if not _old or not _old.get("statPred"):
                    continue  # no pre-kick-off snapshot to lock to yet
                _pp["statPred"] = _old["statPred"]
                if _old.get("statPredLow"):
                    _pp["statPredLow"] = _old["statPredLow"]
                _pp["predLocked"] = True
                _frozen += 1
            if _frozen:
                log.info(f"Kick-off display lock: froze {_frozen} player(s)' shown prediction")
    except Exception as _e:
        log.warning(f"Kick-off display lock skipped: {_e}")

    # ── Hold the round ──
    # Don't roll a player to next-round predictions until the CURRENT round is
    # fully done. While _LOCK_ROUND is still being played, anyone who's already
    # played it stays on it: their shown prediction is the one we made for that
    # round (now graded in roundResult), so the cell, popup and movers all read
    # the current round instead of jumping to a next-round projection mid-round.
    # When the round finishes _LOCK_ROUND advances past it and everyone rolls
    # together. Runs AFTER the kick-off lock (played players still had their rolled
    # nextRound then, so the lock skipped them) so there's no conflict.
    try:
        if _LOCK_ROUND:
            _held = 0
            for _pp in players:
                _rr = _pp.get("roundResult")
                if not _rr or _rr.get("round") != _LOCK_ROUND:
                    continue  # hasn't played the current round yet — leave forecast as-is
                _rstats = _rr.get("stats") or {}
                _sp = dict(_pp.get("statPred") or {})
                _splow = dict(_pp.get("statPredLow") or {})
                for _sk, _rv in _rstats.items():
                    if _rv.get("p") is not None:
                        _sp[_sk] = _rv["p"]
                    if _rv.get("low") is not None:
                        _splow[_sk] = _rv["low"]
                _pp["statPred"] = _sp
                _pp["statPredLow"] = _splow
                _pp["nextRound"] = _LOCK_ROUND
                _pp["roundHeld"] = True
                _held += 1
            if _held:
                log.info(f"Round hold: {_held} player(s) kept on round {_LOCK_ROUND} until it finishes")
    except Exception as _e:
        log.warning(f"Round hold skipped: {_e}")

    # Sort by SC rank
    players.sort(key=lambda p: p["rank"])

    # ── 7b. Fill 3-round form + consistency for players without a per-game log
    # (the waiver band) from the SuperCoach Scores page, so the Waiver tab shows
    # real DIFF/TREND/consistency instead of flat season-average placeholders.
    # Players that already have roundStats (top ~50, from the games log) keep
    # their more-granular data.
    filled = 0
    for p in players:
        if p.get("roundStats"):
            continue
        nk1, nk2 = name_key(p["name"]), name_key(p["name"].split()[-1])
        rounds = sc_round_lookup.get(nk1) or sc_round_lookup.get(nk2)
        rec = sc_scores_lookup.get(nk1) or sc_scores_lookup.get(nk2)
        if rounds:
            # Fixed-window series WITH 0s for byes -> the bar chart shows every
            # round (a 0-height block for a bye); 3RD/5RD use only played scores
            # so they stay accurate and genuinely differ from one another.
            p["scores"] = rounds
            played = [s for s in rounds if s and s > 0]
            if played:
                last3 = played[-3:]
                p["scAvg3"] = round(sum(last3) / len(last3), 1)
                ph = price_history_from_scores(p.get("price") or 0, played,
                                               p.get("breakeven") or 0)
                if ph:
                    p["prices"] = ph
            # Also populate lastScore + lastRound from the windowed rounds —
            # otherwise these stay at "" / 0 (game-log fetch only covers the
            # top MAX_GAMES_LOG players, so Isaac Heeney etc. were invisible
            # to the "top scorers of the round" widget despite having scores
            # in their `scores` array).
            #
            # The rounds list is right-aligned to cur_rnd (window ends at
            # cur_rnd), so rounds[-1] is the cur_rnd score; if it's 0 they
            # didn't play this round and we leave lastRound/lastScore alone.
            if cur_rnd and rounds and rounds[-1] > 0:
                p["lastScore"] = rounds[-1]
                p["lastRound"] = cur_rnd
        elif rec and rec.get("avg3"):
            p["scAvg3"] = round(rec["avg3"], 1)
        if rec and rec.get("cons_pct"):
            p["consistency"] = rec["cons_pct"]
        if rounds or rec:
            filled += 1
    log.info(f"Form fill: updated {filled} players (per-round scores / 3-rnd avg)")

    # ── 7. Write output ──
    try:
        fetch_careers(session, players, sc_players)
    except Exception as _e:
        log.warning(f"Career fetch failed: {_e}")

    # Append manual extras (long-term injured / unsigned players we want news
    # to tag) — they're skipped if Footywire has already brought them in.
    _existing = {p.get("name") for p in players if isinstance(p, dict)}
    _extras = _build_extras(_existing)
    if _extras:
        players.extend(_extras)
        log.info(f"Manual extras: added {len(_extras)} ({', '.join(x['name'] for x in _extras)})")

    global LAST_PLAYERS
    LAST_PLAYERS = players
    try:
        _tr = (max((p.get("lastRound") or 0) for p in players) or 0) + 1
        fetch_afl_injury_list(session, _tr)
    except Exception as _e:
        log.error(f"AFL injury list auto-update failed: {_e}")
    write_output(players, sc_players, dt_players, injuries, selections)

    # NOTE: news.json is owned entirely by news_scraper.py (which runs after
    # this script and maintains the rolling archive). fetch_data.py used to also
    # write news.json here — flattening per-player injury news into a separate
    # {news, generated_at, count} schema — but that clobbered the scraper's feed
    # (different schema, injuries only) and, with concurrent runs, corrupted the
    # file. Player injury news still reaches the feed via news_scraper's own
    # injury sources (afl_medical_room / footywire_injuries), so this write was
    # removed to give news.json a single writer.

    print(f"\n✓  Wrote {len(players)} players → {OUTPUT_PATH}")
    print(f"   SC: {len(sc_players)}  DT: {len(dt_players)}  Injuries: {len(injuries)}")
    print(f"\n   Drop players.json next to aflfantasywire.html and reload the browser.\n")


if __name__ == "__main__":
    # Never exit non-zero mid-run leaving players.json stale: if main() blows up
    # after players have been built, persist whatever we have and exit cleanly.
    try:
        main()
    except Exception as e:
        log.error(f"fetch_data.py crashed: {e}")
        log.error(traceback.format_exc())
        if LAST_PLAYERS:
            try:
                write_output(LAST_PLAYERS)
                log.info(f"Wrote {len(LAST_PLAYERS)} players despite crash")
            except Exception as werr:
                log.error(f"Could not write partial players.json: {werr}")

# ── NEWS SCRAPING ─────────────────────────────────────────────────────────────

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
from news_filter import classify_item, is_relevant

AFL_RSS_FEEDS = [
    "https://www.afl.com.au/news/rss",
    "https://www.footywire.com/rss/afl_news.xml",
]

def fetch_afl_news(session, players_list):
    """
    Scrape AFL.com.au and Footywire for news.
    Filter using news_filter.py — only fantasy-relevant items make it through.
    Tag each item with the player it mentions.
    """
    import xml.etree.ElementTree as ET
    news_items = []
    player_names = {p["name"].lower(): p["id"] for p in players_list}
    # Also index by last name
    for p in players_list:
        last = p["name"].split()[-1].lower()
        if last not in player_names:
            player_names[last] = p["id"]

    for feed_url in AFL_RSS_FEEDS:
        try:
            r = session.get(feed_url, timeout=10)
            if not r or r.status_code != 200:
                continue
            root = ET.fromstring(r.text)
            items = root.findall(".//item")
            log.info(f"RSS {feed_url}: {len(items)} items")

            for item in items:
                title = (item.findtext("title") or "").strip()
                desc  = (item.findtext("description") or "").strip()
                link  = (item.findtext("link") or "").strip()
                pub   = (item.findtext("pubDate") or "").strip()
                text  = title + " " + desc

                # Run through fantasy relevance filter
                result = classify_item(text, title)
                if not result["relevant"]:
                    continue

                # Find which player this is about
                pid = None
                mentioned_player = None
                for name, player_id in player_names.items():
                    if name in text.lower():
                        pid = player_id
                        mentioned_player = name.title()
                        break

                news_items.append({
                    "id":        len(news_items) + 1,
                    "type":      result["type"],
                    "source":    "AFL.com.au" if "afl.com.au" in feed_url else "Footywire",
                    "title":     title,
                    "body":      desc[:300],
                    "link":      link,
                    "time":      pub[:20] if pub else "recent",
                    "pid":       pid,
                    "player":    mentioned_player,
                    "relevance": result["score"],
                    "category":  result["category"],
                })
        except Exception as e:
            log.error(f"News fetch failed for {feed_url}: {e}")
            continue

    # Sort by relevance score descending
    news_items.sort(key=lambda x: x["relevance"], reverse=True)
    log.info(f"News: {len(news_items)} relevant items after filtering")
    return news_items[:50]  # top 50 most relevant


def attach_news_to_players(players, news_items):
    """Attach relevant news items to each player's news array."""
    news_by_pid = {}
    for item in news_items:
        # Index by every player the item tags (top-level pid + the richer
        # players[] array), so a merged multi-player article reaches all of
        # their profiles, not just the primary one.
        pids = set()
        if item.get("pid"):
            pids.add(item["pid"])
        for pp in (item.get("players") or []):
            if isinstance(pp, dict) and pp.get("pid"):
                pids.add(pp["pid"])
        for pid in pids:
            news_by_pid.setdefault(pid, []).append(item)

    for p in players:
        existing = p.get("news", [])
        new_items = news_by_pid.get(p["id"], [])
        # Merge, dedup by title, scraped items first
        all_news = new_items + [e for e in existing if e.get("source") != "AFL.com.au"]
        p["news"] = all_news[:10]

    return players
