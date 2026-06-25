#!/usr/bin/env python3
"""
AFL team distribution tables (for the top-down stat weighting model)
====================================================================
Builds, per team, a coverage-independent picture of how that team's per-game
output is generated and shared out — straight from the official AFL match-stats
feed (real box scores), NOT by summing whatever players we happened to scrape.

For each team (over its last N completed games):
  team_total[stat]            real per-game team total (e.g. ~375 disposals)
  pos_share[pos][stat]        fraction of that total taken by DEF/MID/RUC/FWD

Plus a league-wide tag table:
  tag[opp][stat]              multiplier (<=1) for ELITE accumulators vs that
                              opponent — captures teams that tag the opposition's
                              best ball-winners harder than position defence alone
                              (Geelong/North/Bulldogs ~-10% on disposals). Shrunk
                              toward 1 because the per-opponent sample is small.

The model then projects each player as:
    team_total × pos_share[pos] × (player_avg / Σ available same-pos avgs)
                × matchup × tag
so the side's output is conserved, depth is accounted for via pos_share, and an
out player's share redistributes to his available position-mates.

Data source = the same CFS feed as afl_lineups (WMCTok + matchStats/{matchId}).
Results are cached to .__fullstats_cache.json (incremental — only new matches are
fetched each run). Fails open: a fetch error just yields whatever's cached.
"""
import json
import re
import unicodedata
from pathlib import Path
from collections import defaultdict

BASE = Path(__file__).parent
CACHE_PATH = BASE / ".__fullstats_cache.json"
AFL_API_SEASON_ID = 85

_TOKEN_URL = "https://api.afl.com.au/cfs/afl/WMCTok"
_MATCHES_URL = "https://aflapi.afl.com.au/afl/v2/matches"
_STATS_URL = "https://api.afl.com.au/cfs/afl/matchStats/{mid}"
_HDR = {"User-Agent": "Mozilla/5.0", "Origin": "https://www.afl.com.au",
        "Referer": "https://www.afl.com.au/"}

# Stats we model (match-feed key -> our key). disposals/kicks/handballs/marks/
# tackles/goals are the weighted set; behinds/hitouts/clearances come along free.
FEED_STATS = ["disposals", "kicks", "handballs", "marks", "tackles", "goals",
              "behinds", "hitouts", "clearances"]
POSES = ["DEF", "MID", "RUC", "FWD"]

LAST_N = 10            # games of history per team
TAG_SHRINK = 0.5       # trust only half the observed elite-suppression gap
TAG_FLOOR = 0.90       # never dock an elite more than 10% for a tag
ELITE_TOP_N = 30       # league's top-N disposal winners are "taggable"

# Feed club names -> the names used in players.json / fetch_data fixtures, so the
# tag table keys match the opponent strings the predictor looks up (_o0).
_FEED_TO_OURS = {
    "Adelaide Crows": "Adelaide", "Brisbane Lions": "Brisbane", "Carlton": "Carlton",
    "Collingwood": "Collingwood", "Essendon": "Essendon", "Fremantle": "Fremantle",
    "Geelong Cats": "Geelong", "Gold Coast SUNS": "Gold Coast", "GWS GIANTS": "GWS Giants",
    "Hawthorn": "Hawthorn", "Melbourne": "Melbourne", "North Melbourne": "North Melbourne",
    "Port Adelaide": "Port Adelaide", "Richmond": "Richmond", "St Kilda": "St Kilda",
    "Sydney Swans": "Sydney", "West Coast Eagles": "West Coast", "Western Bulldogs": "Western Bulldogs",
}


def nkey(n):
    f = unicodedata.normalize("NFKD", n or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z]", "", f.lower())


def _stat_block(stats):
    out = {k: (stats.get(k) or 0) for k in FEED_STATS if k != "clearances"}
    cl = stats.get("clearances") or {}
    out["clearances"] = (cl.get("totalClearances") or 0) if isinstance(cl, dict) else (cl or 0)
    return out


def refresh_cache(season=AFL_API_SEASON_ID, max_round=30):
    """Fetch match stats for any concluded match not already cached. Returns the
    cache dict {matchId: {...}}. Fails open (returns existing cache on error)."""
    cache = {}
    if CACHE_PATH.exists():
        try:
            cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            cache = {}
    try:
        import requests
        s = requests.Session()
        tok = s.post(_TOKEN_URL, headers=_HDR, timeout=12).json()["token"]
        hdr = dict(_HDR); hdr["X-Media-Mis-Token"] = tok
        todo = []
        for rnd in range(1, max_round + 1):
            try:
                ms = s.get(_MATCHES_URL,
                           params={"compSeasonId": season, "roundNumber": rnd, "pageSize": 20},
                           headers={"User-Agent": _HDR["User-Agent"]}, timeout=15).json().get("matches", [])
            except Exception:
                continue
            if not ms:
                break
            for m in ms:
                if (m.get("status") or "").upper() == "CONCLUDED" and m.get("providerId") not in cache:
                    todo.append((rnd, m["providerId"]))
        for rnd, mid in todo:
            try:
                j = s.get(_STATS_URL.format(mid=mid), headers=hdr, timeout=15).json()

                def pl(side):
                    return [dict(n=(p["player"]["playerName"].get("givenName", "") + " " +
                                    p["player"]["playerName"].get("surname", "")).strip(),
                                 **_stat_block(p["stats"])) for p in j[side]]
                cache[mid] = {
                    "rnd": rnd,
                    "home": j["homeTeamMatchTotals"]["teamName"]["teamName"],
                    "away": j["awayTeamMatchTotals"]["teamName"]["teamName"],
                    "homeTot": _stat_block(j["homeTeamMatchTotals"]["stats"]),
                    "awayTot": _stat_block(j["awayTeamMatchTotals"]["stats"]),
                    "homePl": pl("homePlayerMatchTotals"),
                    "awayPl": pl("awayPlayerMatchTotals"),
                }
            except Exception:
                continue
        CACHE_PATH.write_text(json.dumps(cache), encoding="utf-8")
    except Exception:
        pass
    return cache


def build_tables(pos_by_key, season=AFL_API_SEASON_ID, last_n=LAST_N):
    """Return {"teams": {teamName: {total, posShare}}, "tags": {opp: {stat: mult}}}.
    `pos_by_key` maps nkey(name) -> 'DEF'/'MID'/'RUC'/'FWD' (from players.json)."""
    cache = refresh_cache(season)
    if not cache:
        return {"teams": {}, "tags": {}}

    def getpos(n):
        return pos_by_key.get(nkey(n), "MID")

    # team -> ordered list of (rnd, teamTotals, players)
    games = defaultdict(list)
    for mid in sorted(cache):
        r = cache[mid]
        for side in ("home", "away"):
            games[r[side]].append((r["rnd"], r[side + "Tot"], r[side + "Pl"]))

    teams = {}
    for team, gl in games.items():
        last = gl[-last_n:]
        n = len(last)
        tot = defaultdict(float)
        pos_sum = defaultdict(lambda: defaultdict(float))
        for _, tt, pls in last:
            for s in FEED_STATS:
                tot[s] += tt.get(s, 0)
            for p in pls:
                pos = getpos(p["n"])
                for s in FEED_STATS:
                    pos_sum[pos][s] += p.get(s, 0)
        total = {s: tot[s] / n for s in FEED_STATS}
        pos_share = {pos: {s: (pos_sum[pos][s] / (total[s] * n) if total[s] else 0)
                           for s in FEED_STATS} for pos in POSES}
        teams[team] = {"total": total, "posShare": pos_share, "games": n}

    tags = _build_tags(cache)
    return {"teams": teams, "tags": tags}


def _build_tags(cache):
    """Per-opponent elite-suppression multiplier for disposals (the stat tagging
    targets). Compares elite accumulators' output-vs-own-average to mid-tier
    ball-winners' against the same opponent; the shrunk, floored gap becomes a
    multiplier applied to high-usage players only."""
    pl_vals = defaultdict(list)
    apps = []
    for mid in sorted(cache):
        r = cache[mid]
        for side, opp in (("home", "away"), ("away", "home")):
            for p in r[side + "Pl"]:
                pl_vals[p["n"]].append(p.get("disposals") or 0)
                apps.append((p["n"], r[opp], p.get("disposals") or 0))
    season_avg = {n: sum(v) / len(v) for n, v in pl_vals.items() if len(v) >= 5}
    elite = set(sorted(season_avg, key=lambda n: -season_avg[n])[:ELITE_TOP_N])
    opp_e, opp_b = defaultdict(list), defaultdict(list)
    for n, opp, dv in apps:
        sa = season_avg.get(n)
        if not sa:
            continue
        ratio = dv / sa
        if n in elite:
            opp_e[opp].append(ratio)
        elif sa >= 18:
            opp_b[opp].append(ratio)
    tags = {}
    for opp in opp_e:
        e, b = opp_e[opp], opp_b.get(opp, [])
        if len(e) < 5 or len(b) < 5:
            continue
        gap = (sum(e) / len(e)) - (sum(b) / len(b))   # negative = tags elites
        mult = 1.0 + min(0.0, gap) * TAG_SHRINK       # only ever a downgrade, shrunk
        tags[_FEED_TO_OURS.get(opp, opp)] = {"disposals": round(max(TAG_FLOOR, mult), 3)}
    return tags


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    pj = json.loads((BASE / "players.json").read_text(encoding="utf-8"))
    ps = pj["players"] if isinstance(pj, dict) else pj
    pmap = {nkey(p["name"]): (p.get("pos") or "MID") for p in ps}
    t = build_tables(pmap)
    print("teams:", len(t["teams"]), "| tag teams:", len(t["tags"]))
    bl = t["teams"].get("Brisbane Lions")
    if bl:
        print("Brisbane total/game:", {k: round(v) for k, v in bl["total"].items()})
        print("Brisbane MID share:", {k: round(v, 2) for k, v in bl["posShare"]["MID"].items()})
    print("tag mults:", {k: v["disposals"] for k, v in sorted(t["tags"].items(), key=lambda x: x[1]["disposals"])[:5]})
