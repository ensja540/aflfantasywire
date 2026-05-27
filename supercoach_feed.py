#!/usr/bin/env python3
"""
AFLFantasyWire — SuperCoach live feed
=====================================
Pulls recent AFL #SuperCoach tweets via the X v2 recent-search API and ADDS the
newest relevant ones to supercoach_tweets.json (accumulating, deduped by id,
newest first, capped at KEEP=10 — oldest drop off).

Cost-conscious: one small batch (max_results=10) per pull, and --auto pulls at
most once per AM / arvo / PM window (3×/day, AEST), so reads stay low.

  python supercoach_feed.py            # pull now (up to KEEP new)
  python supercoach_feed.py --add=N    # cap new additions at N
  python supercoach_feed.py --auto     # pull only if this AM/arvo/PM slot is due
"""
import json, sys, re
from pathlib import Path
from datetime import datetime, timezone, timedelta

import requests

BASE = Path(__file__).parent
OUT = BASE / "supercoach_tweets.json"
KEEP = 10           # max tweets kept on the site (oldest drop off)
MAX_RESULTS = 10    # smallest batch the API allows — keeps read-cost low

AFL_RE = re.compile(r"\bafl\b|aflfantasy|#aflfantasy|dream\s?team|footy|"
                    r"\bmid\b|\bruck\b|\bdefender\b|\bforward\b|break-?even|"
                    r"captain|cash cow|trade|round \d", re.I)
BLOCK_RE = re.compile(r"\bnrl\b|rugby league|\bnba\b|cricket|netball|"
                      r"\baflw\b|women", re.I)


def _mel():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("Australia/Melbourne")
    except Exception:
        return timezone(timedelta(hours=10))


def aest_now():
    return datetime.now(_mel())


def slot(dt):
    """am (6-12) / arvo (12-17) / pm (17-23), else None (outside window)."""
    h = dt.hour
    return "am" if 6 <= h < 12 else "arvo" if 12 <= h < 17 else "pm" if 17 <= h < 23 else None


def should_pull():
    now = aest_now()
    s = slot(now)
    if not s:
        return False, f"outside AM/arvo/PM windows (AEST {now:%H:%M})"
    try:
        prev = json.loads(OUT.read_text(encoding="utf-8")).get("fetched_at")
        if prev:
            last = datetime.fromisoformat(prev).astimezone(_mel())
            if last.date() == now.date() and slot(last) == s:
                return False, f"already pulled the {s} slot today"
    except Exception:
        pass
    return True, f"{s} slot due (AEST {now:%H:%M})"


def load_env():
    env = {}
    p = BASE / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def existing_tweets():
    try:
        return json.loads(OUT.read_text(encoding="utf-8")).get("tweets", [])
    except Exception:
        return []


def main():
    if "--auto" in sys.argv:
        ok, why = should_pull()
        print(f"[auto] {why}")
        if not ok:
            return

    add_n = KEEP
    for a in sys.argv:
        if a.startswith("--add="):
            try:
                add_n = int(a.split("=", 1)[1])
            except Exception:
                pass

    have = existing_tweets()
    have_ids = {t.get("id") for t in have}

    env = load_env()
    bearer = env.get("X_BEARER_TOKEN")
    if not bearer:
        print("supercoach_feed: no X_BEARER_TOKEN")
        return
    query = ("#supercoach (AFL OR #AFLFantasy OR fantasy OR footy) "
             "-is:retweet -is:reply -nrl -from:AFLFantasyWire lang:en")
    try:
        r = requests.get(
            "https://api.twitter.com/2/tweets/search/recent",
            params={"query": query, "max_results": MAX_RESULTS,
                    "tweet.fields": "created_at,public_metrics",
                    "expansions": "author_id",
                    "user.fields": "name,username,profile_image_url,verified"},
            headers={"Authorization": f"Bearer {bearer}"}, timeout=25)
    except Exception as e:
        print(f"supercoach_feed: request failed: {e}")
        return
    if not r.ok:
        print(f"supercoach_feed: search {r.status_code}: {r.text[:200]}")
        return
    d = r.json()
    users = {u["id"]: u for u in d.get("includes", {}).get("users", [])}

    fresh = []
    for t in d.get("data", []):
        if t.get("id") in have_ids:
            continue
        text = (t.get("text") or "").strip()
        if BLOCK_RE.search(text) or not AFL_RE.search(text):
            continue
        if len(re.sub(r"https?://\S+", "", text).strip()) < 25:
            continue
        u = users.get(t.get("author_id"), {})
        if not u.get("username") or u.get("username", "").lower() == "aflfantasywire":
            continue
        pm = t.get("public_metrics", {})
        fresh.append({
            "id": t["id"],
            "text": text,
            "name": u.get("name", ""),
            "username": u.get("username", ""),
            "avatar": (u.get("profile_image_url") or "").replace("_normal", "_bigger"),
            "verified": bool(u.get("verified")),
            "created_at": t.get("created_at", ""),
            "likes": pm.get("like_count", 0),
            "retweets": pm.get("retweet_count", 0),
            "url": f"https://twitter.com/{u.get('username','i')}/status/{t['id']}",
        })
        if len(fresh) >= add_n:
            break

    if not fresh:
        print("supercoach_feed: no new AFL #supercoach tweets — feed unchanged")
        return

    seen, kept = set(), []
    for t in sorted(fresh + have, key=lambda x: x.get("created_at", ""), reverse=True):
        if t.get("id") in seen:
            continue
        seen.add(t["id"])
        kept.append(t)
    kept = kept[:KEEP]
    OUT.write_text(json.dumps(
        {"fetched_at": datetime.now(timezone.utc).isoformat(), "tweets": kept},
        indent=2), encoding="utf-8")
    print(f"supercoach_feed: added {len(fresh)} new, {len(kept)} total")


if __name__ == "__main__":
    main()
