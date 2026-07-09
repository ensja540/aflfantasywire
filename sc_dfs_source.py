"""
Ungated SuperCoach data acquisition — SC API + DFS Australia.
======================================================================
Replaces the Footywire SuperCoach-season scrape and the ~350-page per-player
games-log crawl (steps 1, 2, 5b, 5c, 6 of fetch_data.main) with two ungated
calls:

  * Official SuperCoach API  → exact price, price change, ownership, positions,
                               SC rank, current-round score.
  * DFS Australia (AJAX)     → full per-round SuperCoach + AFL Fantasy scores and
                               per-round stat lines for EVERY player, all season.

Breakevens are NOT published by either source, so they still come from
Footywire's single /supercoach_breakevens page (merged in fetch_data.main) —
that one light page is the only remaining Footywire dependency for player data.

`build_sc_players()` returns the SAME `sc` dict shape that parse_sc_stats + the
games-log loop used to produce, plus the DvP "points conceded" accumulators the
schedule rating consumes — so build_player() and every downstream computation
are unchanged.

Why this exists: Footywire gated /afl/footy/* behind a Cloudflare Turnstile
challenge on 2026-07-03, and the heavy games-log crawl was both the thing that
tripped the block/timeouts and the reason a whole round could go un-ingested.
"""
import re
import time
import logging
import unicodedata
from collections import defaultdict

import requests

import fetch_data as fd

log = logging.getLogger("sc_dfs")

# Official SuperCoach classic API (ungated, no auth). heraldsun.com.au redirects
# here; the /players/full path falls through to the Angular SPA — this is the
# real data path. embed=player_stats gives the current-round price/avg block.
SC_API_URL = ("https://www.supercoach.com.au/2026/api/afl/classic/v1/"
              "players-cf?embed=player_stats,positions")

# DFS Australia player-stats download (WordPress AJAX). One POST returns the
# whole season's per-round rows for every player who has played.
DFS_AJAX_URL = "https://dfsaustralia.com/wp-admin/admin-ajax.php"
DFS_ACTION = "afl_player_stats_download_call_mysql"
DFS_REFERER = "https://dfsaustralia.com/afl-stats-download/"

SEASON_YEAR = "2026"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# DFS team abbreviation -> canonical club name (TEAM_COLOURS keys). Everything is
# then run through fd.normalise_team so it always matches build_player's teams.
DFS_TEAM = {
    "ADE": "Adelaide", "BRL": "Brisbane", "CAR": "Carlton", "COL": "Collingwood",
    "ESS": "Essendon", "FRE": "Fremantle", "GCS": "Gold Coast", "GEE": "Geelong",
    "GWS": "GWS Giants", "HAW": "Hawthorn", "MEL": "Melbourne",
    "NTH": "North Melbourne", "PTA": "Port Adelaide", "RIC": "Richmond",
    "STK": "St Kilda", "SYD": "Sydney", "WBD": "Western Bulldogs",
    "WCE": "West Coast",
}

# Stat fields we accumulate per game for the season per-game averages and the
# DvP "conceded" profiles (matched to what the games-log path produced).
STAT_KEYS = ("disposals", "kicks", "handballs", "marks", "tackles",
             "behinds", "goals", "hitouts")
CONC_STAT_KEYS = ("disposals", "kicks", "handballs", "marks", "tackles",
                  "behinds", "goals")


def _session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json"})
    return s


def fetch_sc_api(session=None):
    """List of raw SuperCoach API player objects (~800). Raises on HTTP error."""
    s = session or _session()
    r = s.get(SC_API_URL, timeout=45)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list) or not data:
        raise ValueError(f"SC API returned no players (type={type(data).__name__})")
    log.info(f"SC API: {len(data)} players")
    return data


def fetch_dfs(session=None):
    """List of this-season DFS per-round rows. Raises on HTTP error."""
    s = session or _session()
    r = s.post(DFS_AJAX_URL, data={"action": DFS_ACTION},
               headers={"Referer": DFS_REFERER}, timeout=60)
    r.raise_for_status()
    rows = (r.json() or {}).get("data") or []
    rows = [x for x in rows if str(x.get("year")) == SEASON_YEAR]
    if not rows:
        raise ValueError("DFS returned no 2026 rows")
    log.info(f"DFS: {len(rows)} per-round rows this season")
    return rows


def _nk(s):
    """ASCII-folded, letters-only name key (matches SC API and DFS spellings)."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z]", "", s.lower())


def _to_int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _dfs_stat(row, key):
    """One stat from a DFS row. DFS has no disposals column — derive it."""
    if key == "disposals":
        return _to_int(row.get("kicks")) + _to_int(row.get("handballs"))
    return _to_int(row.get(key))


def _dfs_series(dfs_rows):
    """name_key -> {round:int -> row} for this season."""
    out = defaultdict(dict)
    for r in dfs_rows:
        out[_nk(r.get("player"))][_to_int(r.get("round"))] = r
    return out


def _name_pins(existing_players):
    """(*surname_key*, team, first-initial) -> existing display name.

    Lets a migrated player keep the exact name (and therefore the stable_pid,
    and therefore any saved My Team / watchlist entry) the site already used,
    even where the new sources spell the given name differently (e.g. DFS/SC
    "Bradley Hill" vs the site's "Brad Hill"). Keyed with the first initial so
    same-surname team-mates don't collide.
    """
    pins = {}
    for ep in existing_players or []:
        nm = ep.get("name") or ""
        parts = nm.split()
        if len(parts) >= 2:
            key = (_nk(parts[-1]), ep.get("team", ""), parts[0][:1].lower())
            pins.setdefault(key, nm)
    return pins


def build_sc_players(sc_api, dfs_rows, existing_players=None):
    """Build the `sc_players` list (parse_sc_stats + games-log shape) and the
    DvP conceded accumulators from the SC API + DFS.

    Returns (sc_players, conc_all, conc_pos, conc_stat, cur_round):
      * sc_players — list of `sc` dicts consumed by fetch_data.build_player.
      * conc_all[team]              -> [SC scores conceded]
      * conc_pos[team][POS]         -> [SC scores conceded to that position]
      * conc_stat[team][POS][stat]  -> [raw stat values conceded]
      * cur_round — highest round number seen in the DFS data.
    """
    series = _dfs_series(dfs_rows)
    cur_round = max((_to_int(r.get("round")) for r in dfs_rows), default=0)
    pins = _name_pins(existing_players)

    conc_all, conc_pos, conc_stat = {}, {}, {}
    sc_players = []

    for pa in sc_api:
        first = (pa.get("first_name") or "").strip()
        last = (pa.get("last_name") or "").strip()
        api_name = f"{first} {last}".strip()
        if not api_name:
            continue
        team = fd.normalise_team((pa.get("team") or {}).get("name") or "")

        # Preserve the site's existing display name/pid where we can.
        name = pins.get((_nk(last), team, first[:1].lower())) or api_name

        positions = [(pp.get("position") or "").upper()
                     for pp in (pa.get("positions") or [])]
        positions = [p for p in positions if p in ("DEF", "MID", "RUC", "FWD")] or ["MID"]
        pos = positions[0]

        stat0 = (pa.get("player_stats") or [{}])[0] or {}
        price = _to_int(stat0.get("price")) or 500000
        price_change = _to_int(stat0.get("price_change"))
        try:
            owned_sc = round(float(stat0.get("owned") or 0), 1)
        except (TypeError, ValueError):
            owned_sc = 0.0

        # Per-round series from DFS (keyed on the API's raw name — the two
        # sources agree on spelling with each other).
        pdata = series.get(_nk(api_name), {})
        sc_all, dt_all, round_stats = [], [], []
        stat_games = defaultdict(list)

        for rd in sorted(pdata.keys()):
            row = pdata[rd]
            sc_s = _to_int(row.get("SC"))
            dt_s = _to_int(row.get("dreamTeamPoints"))
            opp = fd.normalise_team(DFS_TEAM.get(row.get("opp"), row.get("opp") or ""))
            round_stats.append({
                "r": rd, "sc": sc_s, "dt": dt_s,
                "dis": _dfs_stat(row, "disposals"), "mk": _dfs_stat(row, "marks"),
                "tk": _dfs_stat(row, "tackles"), "gl": _dfs_stat(row, "goals"),
                "b": _dfs_stat(row, "behinds"), "k": _dfs_stat(row, "kicks"),
                "hb": _dfs_stat(row, "handballs"), "ho": _dfs_stat(row, "hitouts"),
                "opp": opp,
            })
            if sc_s > 0:
                sc_all.append(sc_s)
            if dt_s > 0:
                dt_all.append(dt_s)
            for sk in STAT_KEYS:
                stat_games[sk].append(_dfs_stat(row, sk))

            # DvP: attribute the SC score (and raw stats) to the opponent that
            # conceded it — real fantasy rounds only (>=1), and only where that
            # opponent had the same coach as now (matches the games-log rule).
            if (sc_s > 0 and opp and opp != "Unknown" and rd >= 1
                    and fd._coach_valid_2026(opp, rd)):
                conc_all.setdefault(opp, []).append(sc_s)
                conc_pos.setdefault(opp, {}).setdefault(pos, []).append(sc_s)
                for sk in CONC_STAT_KEYS:
                    (conc_stat.setdefault(opp, {}).setdefault(pos, {})
                     .setdefault(sk, []).append(_dfs_stat(row, sk)))

        sc_avg = round(sum(sc_all) / len(sc_all), 1) if sc_all else 0
        sc_avg3 = round(sum(sc_all[-3:]) / len(sc_all[-3:]), 1) if sc_all else sc_avg
        sc_last = sc_all[-1] if sc_all else 0
        dt_avg = round(sum(dt_all) / len(dt_all), 1) if dt_all else round(sc_avg * 1.03, 1)
        dt_avg3 = round(sum(dt_all[-3:]) / len(dt_all[-3:]), 1) if dt_all else dt_avg
        dt_last = dt_all[-1] if dt_all else 0

        def _last7(arr):
            a = arr[-7:]
            return a + [0] * (7 - len(a)) if a else [0] * 7

        def _avg_stat(sk):
            v = stat_games.get(sk) or []
            return round(sum(v) / len(v), 1) if v else 0

        sc_players.append({
            "name": name, "team": team, "pos": pos,
            "sc_positions": positions,
            "sc_price": price,
            "sc_price_delta": price_change,      # exact; build_player prefers this
            "sc_avg": sc_avg, "sc_avg3": sc_avg3, "sc_last": sc_last,
            "sc_be": 0,                          # filled from Footywire BE page
            "sc_owned": owned_sc,
            "sc_games": len(sc_all), "sc_total": sum(sc_all),
            "sc_scores": _last7(sc_all), "sc_all_scores": sc_all,
            "dt_scores": _last7(dt_all), "dt_all_scores": dt_all,
            "dt_avg": dt_avg, "dt_avg3": dt_avg3, "dt_last": dt_last,
            "round_stats": round_stats,
            "gamesPlayed": len(sc_all),
            "disposals": _avg_stat("disposals"), "marks": _avg_stat("marks"),
            "goals": _avg_stat("goals"), "behinds": _avg_stat("behinds"),
            "kicks": _avg_stat("kicks"), "handballs": _avg_stat("handballs"),
            "tackles": _avg_stat("tackles"), "hitouts": _avg_stat("hitouts"),
            "clearances": 0,                     # not in DFS; display-only field
            "profile_url": "",                   # no games-log page to crawl
        })

    # Rank by SC season average (desc), matching Footywire's SC season page.
    sc_players.sort(key=lambda p: p.get("sc_avg") or 0, reverse=True)
    for i, p in enumerate(sc_players, 1):
        p["sc_rank"] = i

    log.info(f"Built {len(sc_players)} players from SC API + DFS "
             f"(current round {cur_round})")
    return sc_players, conc_all, conc_pos, conc_stat, cur_round


def fetch_all(existing_players=None, session=None):
    """Convenience: fetch both sources and build. Returns the build_sc_players
    tuple. Network errors propagate to the caller (fetch_data.main decides how
    to fail)."""
    s = session or _session()
    sc_api = fetch_sc_api(s)
    dfs_rows = fetch_dfs(s)
    return build_sc_players(sc_api, dfs_rows, existing_players)
