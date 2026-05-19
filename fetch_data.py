#!/usr/bin/env python3
"""
AFLFantasyWire - Data Fetcher
==============================
DATA SOURCES (all Footywire)
  supercoach_breakevens   -> SC price, games, avg, breakeven, position
  supercoach_scores       -> SC avg, 3-round avg, consistency, total
  supercoach_round        -> SC rank, current round's score (last)
  supercoach_prices       -> SC last price change (priceDelta)
  dream_team_*            -> AFL Fantasy equivalents (same 4 pages)
  injury_list             -> active injury notes
  afl_team_selections     -> stub (page is team-grouped, not flat)

HOW TO RUN
  pip install requests beautifulsoup4 lxml
  python fetch_data.py

  This MUST run from a home/office machine - Footywire blocks cloud IPs.

OUTPUT
  players.json - written next to fetch_data.py.
  Override the path via config.json "output_path" (relative to fetch_data.py).
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

def _load_output_path():
    out = BASE_DIR / "players.json"
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if cfg.get("output_path"):
                out = (BASE_DIR / cfg["output_path"]).resolve()
        except Exception as e:
            log.warning(f"Could not read config.json: {e}")
    return out

OUTPUT_PATH = _load_output_path()

# -- FOOTYWIRE URLS ----------------------------------------------------------

FW = "https://www.footywire.com/afl/footy"

URLS = {
    "sc_breakevens": f"{FW}/supercoach_breakevens",
    "sc_scores":     f"{FW}/supercoach_scores",
    "sc_round":      f"{FW}/supercoach_round",
    "sc_prices":     f"{FW}/supercoach_prices",
    "dt_breakevens": f"{FW}/dream_team_breakevens",
    "dt_scores":     f"{FW}/dream_team_scores",
    "dt_round":      f"{FW}/dream_team_round",
    "dt_prices":     f"{FW}/dream_team_prices",
    "injury_list":   f"{FW}/injury_list",
    "selections":    f"{FW}/afl_team_selections",
}

# -- SESSION -----------------------------------------------------------------

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
        "DNT": "1",
    })
    return s

def get(session, url, retries=3, delay=2):
    """Fetch URL with retries, polite rate limit, and clear error messages."""
    for attempt in range(retries):
        try:
            time.sleep(0.8)
            r = session.get(url, timeout=15)
            if r.status_code == 200:
                return r
            elif r.status_code == 403:
                log.error(f"403 Forbidden: {url}")
                log.error("  -> Footywire is blocking this IP. Run from a residential connection.")
                return None
            elif r.status_code == 404:
                log.error(f"404 Not Found: {url}  (URL may have changed)")
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

# -- TABLE / CELL HELPERS ----------------------------------------------------

def find_data_tables(soup, *needles):
    """
    Return all LEAF tables (no nested <table>) whose first row contains one of
    the needle words. Sorted by row count (biggest first).
    """
    leaf = [t for t in soup.find_all("table") if not t.find("table")]
    leaf.sort(key=lambda t: -len(t.find_all("tr")))
    needles = [n.lower() for n in (needles or ("player",))]
    out = []
    for t in leaf:
        rows = t.find_all("tr")
        if len(rows) < 2:
            continue
        first = " ".join(c.get_text(" ", strip=True) for c in rows[0].find_all(["th","td"])).lower()
        if any(n in first for n in needles):
            out.append(t)
    return out

def find_data_table(soup, *needles):
    """First (biggest) leaf table matching the needles, or None."""
    tables = find_data_tables(soup, *needles)
    return tables[0] if tables else None

def cell_text(cell):
    return cell.get_text(" ", strip=True)

POS_RE = re.compile(r"\b(DEF|MID|RUC|FWD)(?:\s*,\s*(?:DEF|MID|RUC|FWD))?\s*$")

def parse_player_cell(cell):
    """
    Extract (name, position, profile_url) from a Footywire player cell.

    Footywire is inconsistent: breakevens/prices pages put the FULL name as
    text before the <a> tag (anchor has the short name), but scores/round/
    injury pages put the FULL name inside the anchor. Prefer text-before-anchor
    when present; fall back to anchor text otherwise.

      "Reilly O'Brien R. O'Brien RUC"  -> ("Reilly O'Brien", "RUC", url)
      "Connor Rozee C. Rozee DEF, MID" -> ("Connor Rozee",   "DEF", url)
      "Brodie Grundy RUC"              -> ("Brodie Grundy",  "RUC", url)
      "Brodie Grundy B Grundy Swans"   -> ("Brodie Grundy",  "",    url)
      "Dion Prestia"                   -> ("Dion Prestia",   "",    url)
    """
    from bs4 import NavigableString
    a = cell.find("a")
    href = a.get("href", "") if a else ""

    if a is not None:
        before = []
        for child in cell.children:
            if child is a:
                break
            s = str(child).strip() if isinstance(child, NavigableString) else child.get_text(strip=True)
            if s:
                before.append(s)
        before_text = " ".join(before).strip()
        name = before_text or a.get_text(strip=True)
    else:
        name = cell_text(cell)

    full = cell_text(cell)
    m = POS_RE.search(full)
    pos = m.group(1) if m else ""

    profile_url = ""
    if href:
        if href.startswith("http"):
            profile_url = href
        elif href.startswith("/"):
            profile_url = f"https://www.footywire.com{href}"
        else:
            profile_url = f"https://www.footywire.com/afl/footy/{href}"
    return name, pos, profile_url

def to_int(s, default=0):
    if s is None: return default
    s = str(s).replace(",", "").replace("$", "").replace("%", "").strip()
    try: return int(float(s))
    except: return default

def to_float(s, default=0.0):
    if s is None: return default
    s = str(s).replace(",", "").replace("$", "").replace("%", "").strip()
    try: return float(s)
    except: return default

def to_money(s, default=0):
    if s is None: return default
    s = str(s).replace("$", "").replace(",", "").replace(" ", "").strip()
    if s.endswith("k"):
        try: return int(float(s[:-1]) * 1000)
        except: return default
    try: return int(float(s))
    except: return default

def to_signed_money(s, default=0):
    """Parse '+$7,500' / '-$56,000' / '+$0' as a signed int."""
    if s is None: return default
    s = str(s).replace("$", "").replace(",", "").replace(" ", "").strip()
    try: return int(float(s))
    except: return default

# -- FOOTYWIRE PARSERS -------------------------------------------------------

def parse_breakevens(html):
    """Player | Team | Price | G | Avg | Breakeven | Likelihood %"""
    soup = BeautifulSoup(html, "lxml")
    t = find_data_table(soup, "player")
    if not t:
        log.warning("breakevens: no data table found")
        return []
    out = []
    for row in t.find_all("tr")[1:]:
        cells = row.find_all("td")
        if len(cells) < 7:
            continue
        name, pos, href = parse_player_cell(cells[0])
        if not name:
            continue
        out.append({
            "name":        name,
            "pos":         pos or "MID",
            "team_raw":    cell_text(cells[1]),
            "price":       to_money(cell_text(cells[2])),
            "games":       to_int(cell_text(cells[3])),
            "avg":         to_float(cell_text(cells[4])),
            "be":          to_int(cell_text(cells[5])),
            "likelihood":  to_int(cell_text(cells[6])),
            "profile_url": href,
        })
    return out

def parse_scores(html):
    """Player | Team | Price | G | Total | Average | 3-Rnd Average | $/Average | $/3-Rnd Avg | Consistency"""
    soup = BeautifulSoup(html, "lxml")
    t = find_data_table(soup, "player")
    if not t:
        log.warning("scores: no data table found")
        return []
    out = []
    for row in t.find_all("tr")[1:]:
        cells = row.find_all("td")
        if len(cells) < 10:
            continue
        name, pos, _ = parse_player_cell(cells[0])
        if not name:
            continue
        out.append({
            "name":        name,
            "pos":         pos,
            "team_raw":    cell_text(cells[1]),
            "total":       to_int(cell_text(cells[4])),
            "avg":         to_float(cell_text(cells[5])),
            "avg3":        to_float(cell_text(cells[6])),
            "consistency": to_float(cell_text(cells[9])),
        })
    return out

def parse_round(html):
    """Rank | Player | Team | Current Salary | YYYY R# Salary | YYYY R# Score | *Value"""
    soup = BeautifulSoup(html, "lxml")
    t = find_data_table(soup, "rank", "player")
    if not t:
        log.warning("round: no data table found")
        return []
    out = []
    for row in t.find_all("tr")[1:]:
        cells = row.find_all("td")
        if len(cells) < 7:
            continue
        name, _, _ = parse_player_cell(cells[1])
        if not name:
            continue
        out.append({
            "name":       name,
            "team_raw":   cell_text(cells[2]),
            "rank":       to_int(cell_text(cells[0])),
            "last_score": to_int(cell_text(cells[5])),
        })
    return out

def parse_prices(html):
    """Player | Current | Total Change | Change % | Last Change | Expected Price | ..."""
    soup = BeautifulSoup(html, "lxml")
    t = find_data_table(soup, "player")
    if not t:
        log.warning("prices: no data table found")
        return []
    out = []
    for row in t.find_all("tr")[1:]:
        cells = row.find_all("td")
        if len(cells) < 5:
            continue
        name, _, _ = parse_player_cell(cells[0])
        if not name:
            continue
        out.append({
            "name":         name,
            "price_delta":  to_signed_money(cell_text(cells[4])),  # Last Change
            "total_change": to_signed_money(cell_text(cells[2])),
        })
    return out

def parse_injuries(html):
    """
    Footywire's injury_list page has one table PER TEAM (~18 tables).
    Parse all of them. Returns {name_key: {status, detail, eta}}.
    """
    soup = BeautifulSoup(html, "lxml")
    tables = find_data_tables(soup, "player")
    # Filter to injury tables (header contains 'injur')
    injury_tables = []
    for t in tables:
        first = " ".join(c.get_text(" ", strip=True) for c in t.find_all("tr")[0].find_all(["th","td"])).lower()
        if "injur" in first:
            injury_tables.append(t)
    out = {}
    for t in injury_tables:
        for row in t.find_all("tr")[1:]:
            cells = row.find_all("td")
            if len(cells) < 3:
                continue
            name, _, _ = parse_player_cell(cells[0])
            if not name:
                continue
            injury    = cell_text(cells[1])
            returning = cell_text(cells[2])
            rl = returning.lower()
            if any(x in rl for x in ("test", "tbc", "managed", "uncertain", "doubtful")):
                status = "tbc"
            else:
                # Anyone on the injury list is at least "out" for this round.
                status = "out"
            out[name_key(name)] = {
                "status": status,
                "detail": injury,
                "eta":    returning,
            }
    return out

def parse_selections(html):
    """
    afl_team_selections is grouped per-team with sub-headers like 'Interchange',
    not a flat 'name | change' table. Skipping in v1 - selection news layered
    on by the app instead.
    """
    return {}

# -- TEAM NAME / POSITION NORMALISATION --------------------------------------

TEAM_COLOURS = {
    "Adelaide":         {"tc":"#002b5c","tb":"rgba(0,43,92,0.12)"},
    "Brisbane":         {"tc":"#e8a040","tb":"rgba(160,90,0,0.12)"},
    "Carlton":          {"tc":"#6090d8","tb":"rgba(0,60,140,0.12)"},
    "Collingwood":      {"tc":"#e0e0e0","tb":"rgba(255,255,255,0.07)"},
    "Essendon":         {"tc":"#cc3333","tb":"rgba(180,30,30,0.12)"},
    "Fremantle":        {"tc":"#7b3d9e","tb":"rgba(100,30,130,0.12)"},
    "Geelong":          {"tc":"#1a4e8c","tb":"rgba(0,50,120,0.12)"},
    "Gold Coast":       {"tc":"#e8b840","tb":"rgba(200,150,0,0.12)"},
    "GWS Giants":       {"tc":"#e07030","tb":"rgba(220,80,0,0.12)"},
    "Hawthorn":         {"tc":"#c89020","tb":"rgba(160,110,0,0.12)"},
    "Melbourne":        {"tc":"#1a6fd8","tb":"rgba(0,80,200,0.12)"},
    "North Melbourne":  {"tc":"#1a3a8c","tb":"rgba(0,30,100,0.12)"},
    "Port Adelaide":    {"tc":"#888888","tb":"rgba(100,100,100,0.10)"},
    "Richmond":         {"tc":"#f0c040","tb":"rgba(240,192,64,0.10)"},
    "St Kilda":         {"tc":"#e03030","tb":"rgba(200,0,0,0.12)"},
    "Sydney":           {"tc":"#e04040","tb":"rgba(180,30,30,0.12)"},
    "West Coast":       {"tc":"#1a3d8c","tb":"rgba(0,40,120,0.12)"},
    "Western Bulldogs": {"tc":"#4080c8","tb":"rgba(0,60,160,0.12)"},
}

# Footywire uses nicknames in its 'Team' column. Map them to canonical names.
TEAM_ALIASES = {
    "CROWS":"Adelaide", "ADELAIDE":"Adelaide", "ADELAIDE CROWS":"Adelaide",
    "LIONS":"Brisbane", "BRISBANE":"Brisbane", "BRISBANE LIONS":"Brisbane",
    "BLUES":"Carlton", "CARLTON":"Carlton",
    "MAGPIES":"Collingwood", "COLLINGWOOD":"Collingwood", "PIES":"Collingwood",
    "BOMBERS":"Essendon", "ESSENDON":"Essendon", "DONS":"Essendon",
    "DOCKERS":"Fremantle", "FREMANTLE":"Fremantle", "FREO":"Fremantle",
    "CATS":"Geelong", "GEELONG":"Geelong", "GEELONG CATS":"Geelong",
    "SUNS":"Gold Coast", "GOLD COAST":"Gold Coast", "GOLD COAST SUNS":"Gold Coast",
    "GIANTS":"GWS Giants", "GWS":"GWS Giants", "GWS GIANTS":"GWS Giants", "GREATER WESTERN SYDNEY":"GWS Giants",
    "HAWKS":"Hawthorn", "HAWTHORN":"Hawthorn",
    "DEMONS":"Melbourne", "MELBOURNE":"Melbourne", "DEES":"Melbourne",
    "KANGAROOS":"North Melbourne", "ROOS":"North Melbourne", "NORTH":"North Melbourne", "NORTH MELBOURNE":"North Melbourne",
    "POWER":"Port Adelaide", "PORT":"Port Adelaide", "PORT ADELAIDE":"Port Adelaide", "PORT ADELAIDE POWER":"Port Adelaide",
    "TIGERS":"Richmond", "RICHMOND":"Richmond",
    "SAINTS":"St Kilda", "ST KILDA":"St Kilda", "ST.KILDA":"St Kilda",
    "SWANS":"Sydney", "SYDNEY":"Sydney", "SYDNEY SWANS":"Sydney",
    "EAGLES":"West Coast", "WEST COAST":"West Coast", "WEST COAST EAGLES":"West Coast",
    "BULLDOGS":"Western Bulldogs", "DOGS":"Western Bulldogs", "WESTERN BULLDOGS":"Western Bulldogs",
}

def normalise_team(raw):
    if not raw: return "Unknown"
    clean = raw.strip().upper()
    if clean in TEAM_ALIASES: return TEAM_ALIASES[clean]
    for k in TEAM_COLOURS:
        if k.upper() == clean: return k
    for k in TEAM_COLOURS:
        if k.upper() in clean or clean in k.upper(): return k
    return raw.strip()

def normalise_pos(raw):
    if not raw: return "MID"
    p = re.split(r"[/,]", str(raw).upper())[0].strip()
    return {"DEF":"DEF","MID":"MID","RUC":"RUC","FWD":"FWD",
            "D":"DEF","M":"MID","R":"RUC","F":"FWD"}.get(p, "MID")

def name_key(name):
    return re.sub(r"[^a-z]", "", name.lower())

# -- SIGNAL / TAGS / PRICE-HISTORY -------------------------------------------

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
    inj = p.get("injuryStatus", "fit")
    if inj == "out": t.append("OUT")
    elif inj == "tbc": t.append("TBC")
    avg3 = p.get("scAvg3", 0) or 0
    be   = p.get("breakeven", 0) or 0
    pd   = p.get("priceDelta", 0) or 0
    own  = p.get("owned", 0) or 0
    sig  = p.get("signal", "hold")
    if avg3 >= 120: t.append("Premium")
    elif avg3 >= 108: t.append("Top 30")
    if pd > 15000: t.append("Price rising")
    elif pd < -12000: t.append("Price falling")
    if own < 20 and sig == "buy": t.append("POD")
    elif own > 60: t.append("Popular")
    if avg3 > be + 15: t.append("B/E safe")
    return t[:5]

def estimate_price_history(current_price, avg3, be, num_rounds=7):
    """Estimate a price-history sparkline from current price + trajectory."""
    history = []
    diff = (avg3 or 0) - (be or 0)
    weekly_change = round(diff * 800)
    for i in range(num_rounds, 0, -1):
        price = max(100000, round(current_price - (weekly_change * i * 0.7)))
        history.append(price)
    history.append(current_price)
    return history[-7:]

# -- PLAYER RECORD BUILDER ---------------------------------------------------

def build_player(sc, dt, injuries, rank):
    """Merge SC + DT stats with injury data into the app's player schema."""
    name = sc.get("name", "")
    team = normalise_team(sc.get("team_raw", "") or (dt.get("team_raw", "") if dt else ""))
    pos  = normalise_pos(sc.get("pos", "") or (dt.get("pos", "") if dt else ""))
    col  = TEAM_COLOURS.get(team, {"tc":"#888","tb":"rgba(100,100,100,0.1)"})

    sc_price       = sc.get("price", 500000) or 500000
    sc_avg         = sc.get("avg", 0) or 0
    sc_avg3        = sc.get("avg3", sc_avg) or sc_avg
    sc_last        = sc.get("last_score", 0) or 0
    sc_be          = sc.get("be", 0) or 0
    sc_consistency = sc.get("consistency", 75) or 75
    sc_owned       = 0  # not available without SC API

    if dt:
        dt_price = dt.get("price", sc_price) or sc_price
        dt_avg   = dt.get("avg", 0) or round(sc_avg * 1.03)
        dt_avg3  = dt.get("avg3", 0) or round(sc_avg3 * 1.03)
        dt_last  = dt.get("last_score", 0) or round(sc_last * 1.03)
        dt_be    = dt.get("be", 0) or round(sc_be * 0.97)
    else:
        dt_price = sc_price
        dt_avg   = round(sc_avg * 1.03)
        dt_avg3  = round(sc_avg3 * 1.03)
        dt_last  = round(sc_last * 1.03)
        dt_be    = round(sc_be * 0.97)

    nk = name_key(name)
    inj_data   = injuries.get(nk) or injuries.get(name_key(name.split()[-1])) or {}
    inj_status = inj_data.get("status", "fit")
    inj_detail = inj_data.get("detail", "")
    inj_eta    = inj_data.get("eta", "")

    # SC priceDelta - prefer real Last Change from prices page; estimate as fallback.
    price_delta = sc.get("price_delta")
    if price_delta is None:
        price_delta = round((sc_avg3 - sc_be) * 800) if sc_avg3 and sc_be else 0
    price_hist = estimate_price_history(sc_price, sc_avg3, sc_be)

    # DT priceDelta + sparkline (AFL Fantasy mode)
    dt_price_delta = (dt.get("price_delta") if dt else None)
    if dt_price_delta is None:
        dt_price_delta = round((dt_avg3 - dt_be) * 800) if dt_avg3 and dt_be else 0
    dt_price_hist  = estimate_price_history(dt_price, dt_avg3, dt_be)
    dt_consistency = (dt.get("consistency") if dt else None) or sc_consistency

    # Per-league signals - SC for Classic mode, DT for AFL Fantasy mode.
    sig,    conf    = build_signal(sc_avg3, sc_be, inj_status, price_delta)
    dt_sig, dt_conf = build_signal(dt_avg3, dt_be, inj_status, dt_price_delta)

    # Tags - classic-flavoured (price/BE oriented).
    tag_input = {
        "injuryStatus": inj_status, "signal": sig,
        "scAvg3": round(sc_avg3, 1), "breakeven": sc_be,
        "priceDelta": price_delta, "owned": sc_owned,
    }
    tags = auto_tags(tag_input)

    parts = [f"{name} - {sig.upper()} signal."]
    if sc_avg3 and sc_be:
        diff = round(sc_avg3 - sc_be)
        parts.append(f"3-round avg {round(sc_avg3)} is {abs(diff)} {'above' if diff > 0 else 'below'} break-even ({sc_be}).")
    if inj_status == "out":
        parts.append(f"OUT - {inj_detail or 'injury'}.")
    elif inj_status == "tbc":
        parts.append(f"TBC - {inj_detail or 'managed'}.")

    dt_parts = [f"{name} - {dt_sig.upper()} signal (AFL Fantasy)."]
    if dt_avg3 and dt_be:
        d = round(dt_avg3 - dt_be)
        dt_parts.append(f"DT 3-round avg {round(dt_avg3)} is {abs(d)} {'above' if d > 0 else 'below'} DT break-even ({dt_be}).")
    if inj_status == "out":
        dt_parts.append(f"OUT - {inj_detail or 'injury'}.")
    elif inj_status == "tbc":
        dt_parts.append(f"TBC - {inj_detail or 'managed'}.")

    news = []
    if inj_status in ("out", "tbc") and inj_detail:
        news.append({
            "id": 1, "type": "injury", "source": "Footywire", "time": "latest",
            "title": f"{name} - {inj_status.upper()}: {inj_detail}",
            "body":  f"Status: {inj_status.upper()}. {inj_detail}. ETA: {inj_eta or 'unknown'}.",
            "tags": [inj_status.upper(), inj_detail[:30], inj_eta or ""],
        })

    # No per-round data on the consolidated pages; use a flat sparkline.
    sc_scores = [sc_last] * 7 if sc_last else [round(sc_avg)] * 7
    dt_scores = [dt_last] * 7 if dt_last else [round(dt_avg)] * 7

    name_parts = name.split()
    init = (name_parts[0][0] + name_parts[-1][0]).upper() if len(name_parts) >= 2 else name[:2].upper()

    return {
        "id":   rank,
        "name": name,
        "init": init,
        "team": team,
        "pos":  pos,
        "tc":   col["tc"],
        "tb":   col["tb"],

        "signal":     sig,
        "signalConf": conf,
        "rank":       sc.get("rank") or rank,
        "afRank":     (dt.get("rank") if dt else None) or rank,

        "owned":      sc_owned,
        "ownedDelta": 0,

        "scAvg":     round(sc_avg, 1),
        "scAvg3":    round(sc_avg3, 1),
        "lastScore": sc_last,

        "dtAvg":  round(dt_avg, 1),
        "dtAvg3": round(dt_avg3, 1),
        "dtLast": dt_last,

        "price":      sc_price,
        "priceDelta": price_delta,
        "breakeven":  sc_be,

        "dtPrice":      dt_price,
        "dtPriceDelta": dt_price_delta,
        "dtBe":         dt_be,

        "dtSignal":     dt_sig,
        "dtSignalConf": dt_conf,
        "dtBshReason":  " ".join(dt_parts),

        "disposals":  25.0,
        "clearances": 5.0,
        "tackles":    4.0,
        "goals":      0.5,

        "scores":   sc_scores,
        "dtScores": dt_scores,
        "prices":   price_hist,
        "dtPrices": dt_price_hist,

        "ceiling": round(max(sc_avg * 1.3, sc_last) or sc_avg or 0),
        "floor":   round(max(0, min(sc_avg * 0.7, sc_last or sc_avg))),
        "consistency":   round(sc_consistency),
        "dtConsistency": round(dt_consistency),

        "bshCommunity": {
            "buy":  60 if sig == "buy"  else (15 if sig == "sell" else 35),
            "hold": 30 if sig == "hold" else (20 if sig == "buy"  else 25),
            "sell": 10 if sig == "buy"  else (60 if sig == "sell" else 40),
        },
        "injuryStatus": inj_status,
        "injuryDetail": inj_detail,
        "tags":         tags,
        "bshReason":    " ".join(parts),
        "scheduleRating": [7, 7, 7, 7, 7],
        "news":          news,
        "profileUrl":    sc.get("profile_url", ""),
        "_source":       "footywire",
        "_scraped_at":   datetime.now().isoformat(),
    }

# -- LEAGUE FETCHER ----------------------------------------------------------

def fetch_league(session, prefix, label):
    """Fetch all 4 pages for one league (SC or DT) and merge per player."""
    log.info(f"Fetching {label} breakevens...")
    r = get(session, URLS[f"{prefix}_breakevens"])
    if not r:
        return {}
    by_name = {}
    for p in parse_breakevens(r.text):
        by_name[name_key(p["name"])] = p
    log.info(f"  {label} breakevens: {len(by_name)} players")

    time.sleep(1)
    log.info(f"Fetching {label} scores (avg3, consistency)...")
    r = get(session, URLS[f"{prefix}_scores"])
    if r:
        merged = 0
        for p in parse_scores(r.text):
            nk = name_key(p["name"])
            if nk in by_name:
                by_name[nk]["avg3"]        = p["avg3"]
                by_name[nk]["consistency"] = p["consistency"]
                by_name[nk]["total"]       = p["total"]
                if not by_name[nk].get("avg"):
                    by_name[nk]["avg"] = p["avg"]
                merged += 1
        log.info(f"  {label} scores: merged {merged} players")

    time.sleep(1)
    log.info(f"Fetching {label} round (last score, rank)...")
    r = get(session, URLS[f"{prefix}_round"])
    if r:
        merged = 0
        for p in parse_round(r.text):
            nk = name_key(p["name"])
            if nk in by_name:
                by_name[nk]["last_score"] = p["last_score"]
                by_name[nk]["rank"]       = p["rank"]
                merged += 1
        log.info(f"  {label} round: merged {merged} players")

    time.sleep(1)
    log.info(f"Fetching {label} prices (priceDelta)...")
    r = get(session, URLS[f"{prefix}_prices"])
    if r:
        merged = 0
        for p in parse_prices(r.text):
            nk = name_key(p["name"])
            if nk in by_name:
                by_name[nk]["price_delta"]  = p["price_delta"]
                by_name[nk]["total_change"] = p["total_change"]
                merged += 1
        log.info(f"  {label} prices: merged {merged} players")

    return by_name

# -- MAIN --------------------------------------------------------------------

def main():
    print("=" * 60)
    print("  AFLFantasyWire - Footywire Data Fetcher")
    print("=" * 60)
    print(f"  {datetime.now().strftime('%H:%M:%S  %d %b %Y')}")
    print(f"  Output: {OUTPUT_PATH}\n")

    session = make_session()

    sc_by_name = fetch_league(session, "sc", "SuperCoach")
    if not sc_by_name:
        log.error("No SC players parsed. Exiting.")
        log.error("Make sure you are running this from a home/office machine.")
        sys.exit(1)

    time.sleep(1)
    dt_by_name = fetch_league(session, "dt", "AFL Fantasy")

    time.sleep(1)
    log.info("Fetching injury list...")
    r = get(session, URLS["injury_list"])
    injuries = parse_injuries(r.text) if r else {}
    log.info(f"  Injuries: {len(injuries)} players")

    # Selections deliberately stubbed - see parse_selections() docstring.
    selections = {}

    log.info("Merging SC + DT and building player records...")
    sc_list = list(sc_by_name.items())
    # Sort by SC rank (when known) then by season avg desc so injured/unplayed
    # premiums still appear in a sensible position.
    sc_list.sort(key=lambda kv: (
        kv[1].get("rank") or 9999,
        -(kv[1].get("avg3") or 0),
        -(kv[1].get("avg") or 0),
    ))

    players = []
    for i, (nk, sc) in enumerate(sc_list, 1):
        dt = dt_by_name.get(nk) or dt_by_name.get(name_key(sc["name"].split()[-1]))
        players.append(build_player(sc, dt, injuries, i))

    output = {
        "scraped_at":   datetime.now().isoformat(),
        "round":        "Current",
        "season":       datetime.now().year,
        "player_count": len(players),
        "sources": {
            "sc_players": len(sc_by_name),
            "dt_players": len(dt_by_name),
            "injuries":   len(injuries),
            "selections": len(selections),
        },
        "players": players,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)

    print(f"\nWrote {len(players)} players -> {OUTPUT_PATH}")
    print(f"  SC: {len(sc_by_name)}  DT: {len(dt_by_name)}  Injuries: {len(injuries)}")
    print(f"\n  Drop players.json next to aflfantasywire.html and reload the browser.\n")


if __name__ == "__main__":
    main()
