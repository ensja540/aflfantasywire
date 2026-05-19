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

import json, re, time, logging, sys
from datetime import datetime
from pathlib import Path

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("Run:  pip install requests beautifulsoup4 lxml")
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("afw")

BASE_DIR    = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
OUTPUT_PATH = BASE_DIR.parent / "players.json"

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
    "selection":      f"{FW}/selection_changes",
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

def get(session, url, retries=3, delay=2):
    """Fetch URL with retries and rate limiting."""
    for attempt in range(retries):
        try:
            time.sleep(0.8)  # polite delay between requests
            r = session.get(url, timeout=15)
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

def parse_sc_stats(html):
    """
    Parse Footywire SuperCoach season rankings page.

    Actual table columns (as of 2026):
      Rank | Player | Team | Games | Price | Total Score | Average Score | *Value

    - Player and Team are each wrapped in <a> tags. Team is a nickname ("Swans",
      "Kangaroos") not the full club name.
    - Player name may carry an inline status flag, e.g. "Errol Gulden INJ".
    - This page has NO Position, Break-Even, Last Score, Ownership, or per-round
      columns — those are loaded from supercoach_breakevens and per-player
      profile pages and merged in main().
    """
    soup = BeautifulSoup(html, "lxml")
    players = []

    table = _largest_table(soup)
    if not table:
        log.warning("SC stats: no table found")
        return players

    headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
    log.info(f"SC stats columns: {headers[:10]}")

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

    for row in table.find_all("tr"):
        cells = row.find_all(["td","th"])
        if len(cells) < 6: continue
        if all(c.name == "th" for c in cells): continue
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
            href = name_link["href"]
            profile_url = f"https://www.footywire.com{href}" if href.startswith("/") else href

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


def parse_sc_breakevens(html):
    """
    Parse Footywire SuperCoach break-evens page.

    Actual table columns:
      Player | Team | Price | G | Avg | Breakeven | Likelihood %

    Player cell format: "Reilly O'Brien (RUC)" — position embedded in parens
    after the name. May also be combined like "(MID/FWD)".

    Returns dict keyed by name_key(player) → {name, pos, team, price, games,
    avg, be, likelihood}.
    """
    soup = BeautifulSoup(html, "lxml")
    result = {}

    table = _largest_table(soup)
    if not table:
        log.warning("SC breakevens: no table found")
        return result

    headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
    log.info(f"SC breakevens columns: {headers[:8]}")

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
    i_be     = col_idx("breakeven","break","b/e")
    i_like   = col_idx("likelihood","%")

    # "G" header is a single char and won't match the substring search above
    if i_games is None:
        for i, h in enumerate(headers):
            if h.strip() == "g":
                i_games = i
                break

    for row in table.find_all("tr"):
        cells = row.find_all(["td","th"])
        if len(cells) < 5: continue
        if all(c.name == "th" for c in cells): continue
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

        player_cell = cells[i_player] if i_player is not None and i_player < len(cells) else cells[0]
        raw_name = (player_cell.find("a") or player_cell).get_text(strip=True)

        pos_match = re.search(r"\(([A-Z/]+)\)\s*$", raw_name)
        pos = pos_match.group(1).split("/")[0] if pos_match else ""
        name = re.sub(r"\s*\([A-Z/]+\)\s*$", "", raw_name).strip()
        name = re.sub(r"\s+(INJ|SUS|TBC|EMG)\s*$", "", name).strip()
        if not name or name.lower() in ("player","name",""): continue

        result[name_key(name)] = {
            "name":       name,
            "pos":        pos,
            "team":       v(i_team),
            "price":      money(i_price),
            "games":      int(num(i_games)),
            "avg":        num(i_avg),
            "be":         int(num(i_be)),
            "likelihood": num(i_like),
        }

    log.info(f"SC breakevens: parsed {len(result)} players")
    return result


def parse_player_rounds(html):
    """
    Parse a player profile page (/afl/footy/pu-{team}--{player}) for:
      - Position (from "Position: Ruck" / "Position: Midfielder" etc. in header)
      - Round-by-round SuperCoach scores table: Round | Price | Score | Value
        (Round 0 = AFL Opening Round; missing/DNP scores shown as "-")

    Returns: {pos, rounds: [int|None per round], prices: [int per round]}
    """
    soup = BeautifulSoup(html, "lxml")
    result = {"pos": "", "rounds": [], "prices": []}

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
        headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        if not headers or headers[0] != "round": continue
        if "score" not in " ".join(headers): continue

        for row in table.find_all("tr")[1:]:
            cells = [c.get_text(strip=True) for c in row.find_all(["td","th"])]
            if len(cells) < 3: continue

            try: rnd = int(re.sub(r"[^\d\-]", "", cells[0]) or "-99")
            except: continue
            if rnd < 0 or rnd > 30: continue

            score_raw = cells[2]
            if score_raw in ("", "-", "DNP", "BYE", "—"):
                result["rounds"].append(None)
            else:
                try: result["rounds"].append(int(re.sub(r"[^\d]", "", score_raw) or 0))
                except: result["rounds"].append(None)

            price_raw = cells[1].replace("$","").replace(",","")
            try: result["prices"].append(int(float(price_raw)))
            except: result["prices"].append(0)

        break

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
    """
    Parse the AFL Fantasy (Dream Team) statistics page.
    Same structure as SC stats but DT-specific values.
    """
    soup = BeautifulSoup(html, "lxml")
    players = []
    table = (
        soup.find("table", id=re.compile("dream|dt|fantasy", re.I)) or
        _largest_table(soup)
    )
    if not table:
        log.warning("DT stats: no table found")
        return players

    headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]

    def col_idx(*names):
        for name in names:
            for i, h in enumerate(headers):
                if name in h: return i
        return None

    i_player = col_idx("player","name")
    i_price  = col_idx("price","cost","value")
    i_avg    = col_idx("avg","average")
    i_be     = col_idx("be","break")
    i_last   = col_idx("last","pts")
    i_owned  = col_idx("own","%")
    round_cols = [i for i, h in enumerate(headers) if re.match(r"r\d+$", h)]

    for row in table.find_all("tr")[1:]:
        cells = row.find_all(["td","th"])
        if len(cells) < 5: continue
        txt = [c.get_text(strip=True) for c in cells]
        def v(i, d=""): return txt[i] if i is not None and i < len(txt) else d
        def num(i,d=0):
            try: return float(re.sub(r"[^\d.]","",v(i) or "0") or 0)
            except: return d
        def money(i,d=0):
            raw=(v(i,"0")).replace("$","").replace(",","")
            if raw.endswith("k"): return int(float(raw[:-1])*1000)
            try: return int(float(raw))
            except: return d

        player_cell = cells[i_player] if i_player is not None else cells[1]
        name = (player_cell.find("a") or player_cell).get_text(strip=True)
        if not name or name.lower() in ("player","name",""): continue

        played = []
        for ci in round_cols:
            raw = v(ci,"")
            if raw and raw not in ("-","DNP",""):
                try: played.append(int(re.sub(r"[^\d]","",raw) or 0))
                except: pass

        last7 = played[-7:] if len(played) >= 7 else (played + [0]*(7-len(played)))
        last3_scores = played[-3:] if played else []
        avg3  = round(sum(last3_scores)/len(last3_scores), 1) if last3_scores else num(i_avg)

        players.append({
            "name":     name,
            "dt_price": money(i_price) or 500000,
            "dt_avg":   num(i_avg),
            "dt_avg3":  avg3,
            "dt_last":  int(num(i_last)),
            "dt_be":    int(num(i_be)),
            "dt_owned": num(i_owned),
            "dt_scores": last7,
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


def parse_injury_list(html):
    """
    Parse Footywire injury list page.
    Returns dict of {player_name: {"status": "out|tbc|fit", "detail": "...", "eta": "..."}}
    """
    soup = BeautifulSoup(html, "lxml")
    injuries = {}

    table = _largest_table(soup)
    if not table: return injuries

    for row in table.find_all("tr")[1:]:
        cells = row.find_all(["td","th"])
        if len(cells) < 3: continue
        txt = [c.get_text(strip=True) for c in cells]

        name   = txt[0] if txt else ""
        detail = txt[2] if len(txt) > 2 else ""
        eta    = txt[3] if len(txt) > 3 else ""

        if not name: continue

        detail_lower = detail.lower() + eta.lower()
        if any(x in detail_lower for x in ("out","omit","test","season")):
            status = "out"
        elif any(x in detail_lower for x in ("tbc","test","managed","uncertain","doubtful")):
            status = "tbc"
        else:
            status = "fit"

        injuries[name.lower()] = {
            "status": status,
            "detail": detail,
            "eta": eta,
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
    elif inj == "tbc": s -= 20
    sig = "buy" if s >= 30 else ("sell" if s <= -15 else "hold")
    return sig, min(95, max(40, 50 + abs(s)))

def auto_tags(p):
    t = []
    inj = p.get("injuryStatus","fit")
    if inj == "out": t.append("OUT")
    elif inj == "tbc": t.append("TBC")
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

def build_player(sc, dt, injuries, selections, rank):
    """Merge SC stats + DT stats + injury/selection data into the app schema."""

    name  = sc.get("name","") or dt.get("name","")
    team  = normalise_team(sc.get("team","") or dt.get("team",""))
    pos   = normalise_pos(sc.get("pos","") or dt.get("pos",""))
    col   = TEAM_COLOURS.get(team, {"tc":"#888","tb":"rgba(100,100,100,0.1)"})

    sc_avg   = sc.get("sc_avg", 0) or 0
    sc_avg3  = sc.get("sc_avg3", sc_avg) or sc_avg
    sc_last  = sc.get("sc_last", 0) or 0
    sc_price = sc.get("sc_price", 500000) or 500000
    sc_be    = sc.get("sc_be", 0) or 0
    sc_owned = sc.get("sc_owned", 0) or 0
    sc_scores = sc.get("sc_scores", [sc_last]*7) or [sc_last]*7

    # AFL Fantasy Classic ownership (Footywire doesn't expose SC ownership for free,
    # so Classic ownership from fantasy.afl.com.au is the only live ownership signal).
    classic_owned = float(sc.get("classic_owned", 0) or 0)
    classic_avg   = float(sc.get("classic_avg",   0) or 0)
    classic_avg3  = float(sc.get("classic_avg3",  0) or 0)
    classic_proj  = float(sc.get("classic_proj",  0) or 0)
    classic_price = int(sc.get("classic_price", 0) or 0)

    dt_avg   = dt.get("dt_avg", round(sc_avg  * 1.03)) if dt else round(sc_avg  * 1.03)
    dt_avg3  = dt.get("dt_avg3",round(sc_avg3 * 1.03)) if dt else round(sc_avg3 * 1.03)
    dt_last  = dt.get("dt_last",round(sc_last * 1.03)) if dt else round(sc_last * 1.03)
    dt_be    = dt.get("dt_be",  round(sc_be   * 0.97)) if dt else round(sc_be   * 0.97)
    dt_owned = dt.get("dt_owned", sc_owned) if dt else sc_owned
    dt_scores= dt.get("dt_scores",[dt_last]*7) if dt else [dt_last]*7

    # Injury status from injury list
    nk = name_key(name)
    inj_data   = injuries.get(nk) or injuries.get(name_key(name.split()[-1])) or {}
    sel_data   = selections.get(nk) or {}
    inj_status = inj_data.get("status","fit")
    inj_detail = inj_data.get("detail","")
    inj_eta    = inj_data.get("eta","")

    # Price delta estimate
    price_delta = round((sc_avg3 - sc_be) * 800) if sc_avg3 and sc_be else 0
    price_hist  = estimate_price_history(sc_price, sc_avg3, sc_be)

    sig, conf = build_signal(sc_avg3, sc_be, inj_status, price_delta)

    # Consistency: % of scores >= 90% of average
    threshold = sc_avg * 0.9
    all_sc = sc.get("sc_all_scores", sc_scores)
    played = [s for s in all_sc if s and s > 0]
    consistency = round(len([s for s in played if s >= threshold]) / len(played) * 100) if played else 75

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
    if inj_status == "out": parts.append(f"OUT — {inj_detail or 'injury'}.")
    elif inj_status == "tbc": parts.append(f"TBC — {inj_detail or 'managed'}.")

    # Build news items from injury/selection data
    news = []
    if inj_status in ("out","tbc") and inj_detail:
        news.append({
            "id":1, "type":"injury", "source":"Footywire",
            "time":"latest",
            "title": f"{name} — {inj_status.upper()}: {inj_detail}",
            "body": f"Status: {inj_status.upper()}. {inj_detail}. ETA: {inj_eta or 'unknown'}.",
            "tags": [inj_status.upper(), inj_detail[:30], inj_eta or ""],
        })
    if sel_data.get("note"):
        news.append({
            "id":2, "type":"selection", "source":"Footywire",
            "time":"latest",
            "title": f"Selection update: {name}",
            "body": sel_data["note"],
            "tags": ["Selection", sel_data.get("change","").title()],
        })

    return {
        "id": rank,
        "name": name,
        "init": (name.split()[0][0] + name.split()[-1][0]).upper() if len(name.split())>=2 else name[:2].upper(),
        "team": team,
        "pos": pos,
        "tc": col["tc"],
        "tb": col["tb"],

        "signal": sig,
        "signalConf": conf,
        "rank": sc.get("sc_rank", rank),
        "afRank": dt.get("dt_rank", rank) if dt else rank,

        "owned": round(sc_owned, 1),
        "ownedDelta": 0,   # requires two fetches to compute delta
        "classicOwned": round(classic_owned, 1),
        "classicAvg":   round(classic_avg,  1),
        "classicAvg3":  round(classic_avg3, 1),
        "classicProj":  round(classic_proj, 1),
        "classicPrice": classic_price,

        "scAvg":   round(sc_avg,  1),
        "scAvg3":  round(sc_avg3, 1),
        "lastScore": sc_last,

        "dtAvg":  round(dt_avg,  1),
        "dtAvg3": round(dt_avg3, 1),
        "dtLast": dt_last,

        "price":      sc_price,
        "priceDelta": price_delta,
        "breakeven":  sc_be,
        "dtBe":       dt_be,

        "disposals":  sc.get("detail",{}).get("disposals",  25.0),
        "clearances": sc.get("detail",{}).get("clearances", 5.0),
        "tackles":    sc.get("detail",{}).get("tackles",    4.0),
        "goals":      sc.get("detail",{}).get("goals",      0.5),

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

    # Merge BE + position into each sc_player by name_key
    for p in sc_players:
        nk = name_key(p["name"])
        be_data = sc_be_lookup.get(nk) or sc_be_lookup.get(name_key(p["name"].split()[-1]))
        if be_data:
            p["sc_be"] = be_data["be"]
            if be_data["pos"]:
                p["pos"] = be_data["pos"]
            # Prefer breakevens price if season price was 0 (rare)
            if not p["sc_price"] and be_data["price"]:
                p["sc_price"] = be_data["price"]

    time.sleep(1)

    # ── 6. Fetch per-player profile pages for round-by-round scores ──
    TOP_N = 200
    log.info(f"Fetching round-by-round scores for top {TOP_N} players...")
    for i, p in enumerate(sc_players[:TOP_N]):
        url = p.get("profile_url", "")
        if not url:
            continue
        r5 = get(session, url)
        if not r5:
            continue

        rounds_data = parse_player_rounds(r5.text)

        # Profile-page position overrides breakevens (more specific)
        if rounds_data["pos"]:
            p["pos"] = rounds_data["pos"]

        all_scores = rounds_data["rounds"]
        played = [s for s in all_scores if s is not None and s > 0]
        p["sc_all_scores"] = played

        # Last 7 played scores (right-pad with 0s if fewer)
        if len(played) >= 7:
            p["sc_scores"] = played[-7:]
        elif played:
            p["sc_scores"] = played + [0] * (7 - len(played))
        else:
            p["sc_scores"] = [0] * 7

        p["sc_last"] = played[-1] if played else 0

        last3 = played[-3:]
        p["sc_avg3"] = round(sum(last3) / len(last3), 1) if last3 else p["sc_avg"]

        if i % 25 == 24:
            log.info(f"  {i+1}/{TOP_N} player profiles fetched")
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
    for i, sc in enumerate(sc_players[:200], 1):
        nk  = name_key(sc["name"])
        nk_last = name_key(sc["name"].split()[-1])
        dt  = dt_lookup.get(nk) or dt_lookup.get(nk_last)
        player = build_player(sc, dt, injuries, selections, i)
        players.append(player)

    # Sort by SC rank
    players.sort(key=lambda p: p["rank"])

    # ── 7. Write output ──
    output = {
        "scraped_at":   datetime.now().isoformat(),
        "round":        "Current",
        "season":       2026,
        "player_count": len(players),
        "sources": {
            "sc_players":  len(sc_players),
            "dt_players":  len(dt_players),
            "injuries":    len(injuries),
            "selections":  len(selections),
        },
        "players": players,
    }

    with open(OUTPUT_PATH, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\n✓  Wrote {len(players)} players → {OUTPUT_PATH}")
    print(f"   SC: {len(sc_players)}  DT: {len(dt_players)}  Injuries: {len(injuries)}")
    print(f"\n   Drop players.json next to aflfantasywire.html and reload the browser.\n")


if __name__ == "__main__":
    main()

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
