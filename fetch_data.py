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

import json, re, time, logging, sys, traceback
from datetime import datetime
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

def make_session():
    """Browser-like session that passes Footywire's bot checks."""
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
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
            pos = pos_raw.split("/")[0].strip()

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
        "sc_rounds":  [], "sc_scores":  [], "af_scores":  [],
        "disposals":  [], "marks":      [], "goals":      [],
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
        i_sc    = ci("sc")
        i_af    = ci("af")
        i_d     = ci("d")
        i_m     = ci("m")
        i_g     = ci("g")
        i_t     = ci("t")
        i_ho    = ci("ho")
        i_cl    = ci("cl")

        for tr in rows[1:]:
            cells = [c.get_text(strip=True)
                     for c in tr.find_all(["td","th"], recursive=False)]
            if i_sc is None or len(cells) <= i_sc: continue

            try: rnd = int(re.sub(r"[^\d\-]", "", cells[i_round]) or "-99")
            except: continue
            if rnd < 0 or rnd > 30: continue

            def parse_int(idx):
                if idx is None or idx >= len(cells): return None
                raw = cells[idx]
                if raw in ("", "-", "DNP", "BYE", "—"): return None
                try: return int(re.sub(r"[^\d\-]", "", raw) or 0)
                except: return None

            result["sc_rounds"].append(rnd)
            result["sc_scores"].append(parse_int(i_sc))
            result["af_scores"].append(parse_int(i_af))
            result["disposals"].append(parse_int(i_d))
            result["marks"].append(parse_int(i_m))
            result["goals"].append(parse_int(i_g))
            result["tackles"].append(parse_int(i_t))
            result["hitouts"].append(parse_int(i_ho))
            result["clearances"].append(parse_int(i_cl))

        if result["sc_scores"]: break

    # Footywire lists most-recent first. Reverse every parallel list so the
    # latest round sits at the end, matching the rest of the pipeline (which
    # treats [-1] as "most recent").
    parallel = ("sc_rounds","sc_scores","af_scores","disposals","marks",
                "goals","tackles","hitouts","clearances")
    for k in parallel:
        result[k].reverse()

    # Drop trailing None SC rounds — the most recent listed round is usually
    # the in-progress one the player hasn't played yet.
    while result["sc_scores"] and result["sc_scores"][-1] is None:
        for k in parallel:
            if result[k]: result[k].pop()

    return result


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

    for p in players:
        first = (p.get("first_name") or "").strip()
        last  = (p.get("last_name")  or "").strip()
        if not first and not last: continue
        name  = f"{first} {last}".strip()
        nk    = name_key(name)
        stats = p.get("stats") or {}

        result[nk] = {
            "classic_owned": float(stats.get("owned_by") or 0),
            "classic_avg":   float(stats.get("avg_points") or 0),
            "classic_avg3":  float(stats.get("last_3_avg") or 0),
            "classic_proj":  float(stats.get("proj_avg") or 0),
            "classic_price": int(p.get("cost") or 0),
        }

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

            injuries[name_key(name)] = {
                "status":    status,        # "out" | "test" | "available"
                "body_part": body_part,     # "Hamstring", "Knee", ...
                "eta":       eta,           # "2 weeks", "Season", "TBC", ...
                "detail":    injury_raw,    # raw injury text (kept for back-compat / titles)
                "returning": returning_raw, # raw returning text
            }

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
    "Adelaide":{"tc":"#002b5c","tb":"rgba(0,43,92,0.12)"},
    "Brisbane":{"tc":"#e8a040","tb":"rgba(160,90,0,0.12)"},
    "Carlton":{"tc":"#6090d8","tb":"rgba(0,60,140,0.12)"},
    "Collingwood":{"tc":"#e0e0e0","tb":"rgba(255,255,255,0.07)"},
    "Essendon":{"tc":"#cc3333","tb":"rgba(180,30,30,0.12)"},
    "Fremantle":{"tc":"#7b3d9e","tb":"rgba(100,30,130,0.12)"},
    "Geelong":{"tc":"#1a4e8c","tb":"rgba(0,50,120,0.12)"},
    "Gold Coast":{"tc":"#e8b840","tb":"rgba(200,150,0,0.12)"},
    "GWS Giants":{"tc":"#e07030","tb":"rgba(220,80,0,0.12)"},
    "Hawthorn":{"tc":"#c89020","tb":"rgba(160,110,0,0.12)"},
    "Melbourne":{"tc":"#1a6fd8","tb":"rgba(0,80,200,0.12)"},
    "North Melbourne":{"tc":"#1a3a8c","tb":"rgba(0,30,100,0.12)"},
    "Port Adelaide":{"tc":"#888888","tb":"rgba(100,100,100,0.10)"},
    "Richmond":{"tc":"#f0c040","tb":"rgba(240,192,64,0.10)"},
    "St Kilda":{"tc":"#e03030","tb":"rgba(200,0,0,0.12)"},
    "Sydney":{"tc":"#e04040","tb":"rgba(180,30,30,0.12)"},
    "West Coast":{"tc":"#1a3d8c","tb":"rgba(0,40,120,0.12)"},
    "Western Bulldogs":{"tc":"#4080c8","tb":"rgba(0,60,160,0.12)"},
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
    p = str(raw).upper().split("/")[0].strip()
    return {"DEF":"DEF","MID":"MID","RUC":"RUC","FWD":"FWD","D":"DEF","M":"MID","R":"RUC","F":"FWD"}.get(p,"MID")

def normalise_pos_list(raw):
    """Split a multi-position flag (e.g. "MID/FWD", "DEF,MID") into a deduped
    list of canonical codes, preserving order. Used for dual-position players."""
    if not raw: return []
    m = {"DEF":"DEF","MID":"MID","RUC":"RUC","FWD":"FWD","D":"DEF","M":"MID","R":"RUC","F":"FWD"}
    out = []
    for part in re.split(r"[/,]", str(raw).upper()):
        part = part.strip()
        if not part: continue
        v = m.get(part)
        if v and v not in out: out.append(v)
    return out

def name_key(name):
    return re.sub(r"[^a-z]","",name.lower())

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
    pos   = normalise_pos(sc.get("pos","") or dt.get("pos",""))
    positions = sc.get("sc_positions") or [pos]
    if pos not in positions: positions = [pos] + positions
    col   = TEAM_COLOURS.get(team, {"tc":"#888","tb":"rgba(100,100,100,0.1)"})

    sc_avg   = sc.get("sc_avg", 0) or 0
    sc_avg3  = sc.get("sc_avg3", sc_avg) or sc_avg
    sc_last  = sc.get("sc_last", 0) or 0
    sc_price = sc.get("sc_price", 500000) or 500000
    sc_be    = sc.get("sc_be", 0) or 0
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
    inj_data      = injuries.get(nk) or injuries.get(name_key(name.split()[-1])) or {}
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
            "pid":  rank,           # frontend keys on pid to find the player record
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
            "pid": rank, "player": name, "team": team, "pos": pos,
            "title": f"Selection update: {name}",
            "headline": f"Selection update: {name}",
            "body": sel_data["note"],
            "tags": ["Selection", sel_data.get("change","").title()],
        })

    return {
        "id": rank,
        "name": name,
        "init": (name.split()[0][0] + name.split()[-1][0]).upper() if len(name.split())>=2 else name[:2].upper(),
        "team": team,
        "pos": pos,
        "positions": positions,
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
}


def write_output(players, sc_players=None, dt_players=None, injuries=None, selections=None):
    """Write players.json. Safe to call with a partial list (e.g. from the crash
    handler) — source counts fall back sensibly when the extras aren't passed."""
    for _p in players:
        _a = NAME_ALIASES.get(_p.get("name"))
        if _a:
            _p["name"] = _a
    output = {
        "scraped_at":   datetime.now().isoformat(),
        "round":        "Current",
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
        log.error("Make sure you are running this from a home/office machine.")
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
    # Also by last name
    for p in dt_players:
        last = name_key(p["name"].split()[-1])
        if last not in dt_lookup:
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
    MAX_GAMES_LOG = 50
    GAMES_LOG_TIME_LIMIT = 180  # 3 minutes max
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

            sc_played = [s for s in games["sc_scores"] if s is not None and s > 0]
            af_played = [s for s in games["af_scores"] if s is not None and s > 0]
            p["sc_all_scores"] = sc_played
            p["dt_all_scores"] = af_played

            # Full round-by-round line for the player profile.
            rs, gr = [], (games.get("sc_rounds") or [])
            for idx in range(len(games["sc_scores"])):
                sc_s = games["sc_scores"][idx]
                if sc_s is None:
                    continue
                def _g(key, ix=idx):
                    arr = games.get(key) or []
                    return arr[ix] if ix < len(arr) and arr[ix] is not None else 0
                rs.append({"r": gr[idx] if idx < len(gr) else f"R{idx+1}",
                           "sc": sc_s, "dt": _g("af_scores"), "dis": _g("disposals"),
                           "mk": _g("marks"), "tk": _g("tackles"), "gl": _g("goals")})
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
        co = classic_lookup.get(nk) or classic_lookup.get(name_key(p["name"].split()[-1]))
        if co:
            p["classic_owned"] = co["classic_owned"]
            p["classic_avg"]   = co["classic_avg"]
            p["classic_avg3"]  = co["classic_avg3"]
            p["classic_proj"]  = co["classic_proj"]
            p["classic_price"] = co["classic_price"]

    # ── 7. Merge and build final player list ──
    log.info("Merging data sources...")
    players = []
    for i, sc in enumerate(sc_players[:600], 1):
        nk  = name_key(sc["name"])
        nk_last = name_key(sc["name"].split()[-1])
        dt  = dt_lookup.get(nk) or dt_lookup.get(nk_last)
        player = build_player(sc, dt, injuries, selections, i)
        players.append(player)

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
        elif rec and rec.get("avg3"):
            p["scAvg3"] = round(rec["avg3"], 1)
        if rec and rec.get("cons_pct"):
            p["consistency"] = rec["cons_pct"]
        if rounds or rec:
            filled += 1
    log.info(f"Form fill: updated {filled} players (per-round scores / 3-rnd avg)")

    # ── 7. Write output ──
    global LAST_PLAYERS
    LAST_PLAYERS = players
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
        if item.get("pid"):
            if item["pid"] not in news_by_pid:
                news_by_pid[item["pid"]] = []
            news_by_pid[item["pid"]].append(item)

    for p in players:
        existing = p.get("news", [])
        new_items = news_by_pid.get(p["id"], [])
        # Merge, dedup by title, scraped items first
        all_news = new_items + [e for e in existing if e.get("source") != "AFL.com.au"]
        p["news"] = all_news[:10]

    return players
