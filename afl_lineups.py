#!/usr/bin/env python3
"""
AFL confirmed team lineups
==========================
Pulls the named teams behind afl.com.au/matches/team-lineups via the Champion
Data CFS feed, so we can mark players "confirmed to play" once their club's team
is officially named.

Flow (all public, no login):
  1. POST https://api.afl.com.au/cfs/afl/WMCTok            -> {"token": ...}
  2. GET  https://aflapi.afl.com.au/afl/v2/matches          -> match providerIds
  3. GET  https://api.afl.com.au/cfs/afl/matchRoster/{id}   (X-Media-Mis-Token)
        -> homeTeam/awayTeam each with teamStatus + positions[]

A team's lineup is only trusted once teamStatus == "FINAL_TEAM" (before that it's
SCHEDULED / a provisional squad). Position code "EMERG" = emergency (not counted
as confirmed to play). The named team is the other 23 (18 field + 5 interchange,
incl. the medical sub).

confirmed_lineup() fails OPEN: any network/parse/token error returns whatever was
gathered so far (possibly empty), so a transient blip never wrongly clears every
player's confirmed flag downstream.
"""
import re
import unicodedata
from datetime import datetime, timezone

AFL_API_SEASON_ID = 85  # 2026 Toyota AFL Premiership (matches tweet_bot / news_scraper)

_TOKEN_URL   = "https://api.afl.com.au/cfs/afl/WMCTok"
_MATCHES_URL = "https://aflapi.afl.com.au/afl/v2/matches"
_ROSTER_URL  = "https://api.afl.com.au/cfs/afl/matchRoster/{pid}"
_HDR = {"User-Agent": "Mozilla/5.0",
        "Origin": "https://www.afl.com.au",
        "Referer": "https://www.afl.com.au/"}

# CFS/aflapi club names -> the names used in players.json.
_TEAM_API_TO_OURS = {
    "Adelaide Crows": "Adelaide", "Brisbane Lions": "Brisbane", "Carlton": "Carlton",
    "Collingwood": "Collingwood", "Essendon": "Essendon", "Fremantle": "Fremantle",
    "Geelong Cats": "Geelong", "Gold Coast SUNS": "Gold Coast", "GWS GIANTS": "GWS Giants",
    "Hawthorn": "Hawthorn", "Melbourne": "Melbourne", "North Melbourne": "North Melbourne",
    "Port Adelaide": "Port Adelaide", "Richmond": "Richmond", "St Kilda": "St Kilda",
    "Sydney Swans": "Sydney", "West Coast Eagles": "West Coast", "Western Bulldogs": "Western Bulldogs",
}


def our_team(api_name):
    return _TEAM_API_TO_OURS.get(api_name, api_name)


def _fold(name):
    return unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()


def lineup_key(name):
    """Join key matching fetch_data.name_key (lower, a-z only), but accent-folded
    first so a feed name with diacritics still matches an ASCII players.json name."""
    return re.sub(r"[^a-z]", "", _fold(name).lower())


def alt_key(name, team):
    """Nickname-tolerant fallback: first initial + surname + team. Resolves feed
    formal names vs our common names (Matthew->Matt, Jackson->Jack, Lachlan->Lachie)
    while keeping same-surname team-mates apart (Nick vs Josh Daicos)."""
    toks = [t for t in re.split(r"\s+", _fold(name).strip()) if t]
    tk = re.sub(r"[^a-z]", "", _fold(team).lower())
    if len(toks) >= 2:
        return toks[0][:1].lower() + re.sub(r"[^a-z]", "", toks[-1].lower()) + "|" + tk
    return lineup_key(name) + "|" + tk


def _token(session):
    r = session.post(_TOKEN_URL, headers=_HDR, timeout=12)
    r.raise_for_status()
    return r.json()["token"]


def _round_matches(session, rnd, season):
    r = session.get(_MATCHES_URL,
                    params={"compSeasonId": season, "roundNumber": rnd, "pageSize": 20},
                    headers={"User-Agent": _HDR["User-Agent"]}, timeout=15)
    r.raise_for_status()
    return r.json().get("matches", []) or []


def confirmed_lineup(rounds, season=AFL_API_SEASON_ID, include_concluded=False):
    """Return {lineup_key(name): info} for every player in an officially NAMED team
    across the given rounds. `info` = {name, status('named'|'emergency'), team, opp,
    matchId, round, startUtc} with team/opp mapped to our naming. Teams not yet at
    FINAL_TEAM, and (by default) matches already CONCLUDED, are skipped so the result
    reflects only the UPCOMING round. Fails open.
    """
    import requests
    out = {}
    try:
        s = requests.Session()
        hdr = dict(_HDR)
        hdr["X-Media-Mis-Token"] = _token(s)
        for rnd in rounds:
            try:
                matches = _round_matches(s, rnd, season)
            except Exception:
                continue
            for m in matches:
                pid = m.get("providerId")
                if not pid:
                    continue
                if not include_concluded and (m.get("status") or "").upper() == "CONCLUDED":
                    continue  # already played — not "confirmed to play"
                home = our_team((m.get("home", {}).get("team", {}) or {}).get("name") or "")
                away = our_team((m.get("away", {}).get("team", {}) or {}).get("name") or "")
                start = m.get("utcStartTime")
                try:
                    jr = s.get(_ROSTER_URL.format(pid=pid), headers=hdr, timeout=15)
                    if not jr.ok:
                        continue
                    j = jr.json()
                except Exception:
                    continue
                for side, team, opp in (("homeTeam", home, away),
                                        ("awayTeam", away, home)):
                    tinfo = j.get(side) or {}
                    if (tinfo.get("teamStatus") or "").upper() != "FINAL_TEAM":
                        continue  # not officially named yet
                    for pos in tinfo.get("positions") or []:
                        pl = pos.get("player") or {}
                        pn = pl.get("playerName") or {}
                        full = (f"{pn.get('givenName','')} {pn.get('surname','')}").strip()
                        if not full:
                            continue
                        status = "emergency" if (pos.get("position") == "EMERG") else "named"
                        out[lineup_key(full)] = {
                            "name": full, "status": status, "team": team, "opp": opp,
                            "home": home, "away": away,
                            "matchId": pid, "round": rnd, "startUtc": start,
                        }
    except Exception:
        return out  # fail open
    return out


def apply_confirmed(players, lineup):
    """Stamp each player dict with confirmed-lineup fields from `lineup`. Matches on
    full-name key first, then initial+surname+team. Sets:
        confirmed (bool, named & not emergency), lineupStatus ('named'/'emergency'/None),
        confirmedRound, confirmedOpp, confirmedStartUtc.
    Players not found in any named team are cleared (confirmed=False). Returns the
    number matched. If `lineup` is empty (no teams named / API blip), leaves existing
    flags UNTOUCHED so a transient miss never wipes the ticks site-wide.
    """
    if not lineup:
        return 0
    by_alt = {}
    for v in lineup.values():
        by_alt[alt_key(v["name"], v["team"])] = v
    n = 0
    for p in players:
        info = lineup.get(lineup_key(p.get("name", ""))) \
            or by_alt.get(alt_key(p.get("name", ""), p.get("team", "")))
        if info:
            p["confirmed"] = (info["status"] == "named")
            p["lineupStatus"] = info["status"]
            p["confirmedRound"] = info["round"]
            p["confirmedOpp"] = info["opp"]
            p["confirmedStartUtc"] = info["startUtc"]
            n += 1
        else:
            p["confirmed"] = False
            p["lineupStatus"] = None
    return n


def hours_until(start_utc):
    """Hours from now (UTC) until an ISO match start time; None if unparseable."""
    if not start_utc:
        return None
    try:
        st = datetime.fromisoformat(start_utc.replace("Z", "+00:00"))
        return (st - datetime.now(timezone.utc)).total_seconds() / 3600
    except Exception:
        return None


if __name__ == "__main__":
    import sys, json
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    rounds = [int(a) for a in sys.argv[1:] if a.isdigit()] or [15, 16]
    lu = confirmed_lineup(rounds)
    named = {k: v for k, v in lu.items() if v["status"] == "named"}
    emerg = {k: v for k, v in lu.items() if v["status"] == "emergency"}
    print(f"rounds {rounds}: {len(named)} named, {len(emerg)} emergencies")
    teams = sorted({v["team"] for v in named.values()})
    print(f"teams named ({len(teams)}): {', '.join(teams)}")
