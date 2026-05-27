#!/usr/bin/env python3
"""
AFLFantasyWire — #SuperCoach feed
=================================
Pulls recent AFL #SuperCoach tweets via the X v2 recent-search API and writes
the best handful to supercoach_tweets.json for the site to render as cards.

#SuperCoach is also used by NRL/cricket SuperCoach, so the query is AFL-biased
and results are post-filtered to drop other codes and AFLW (AFL men's only).

Throttled to one search per REFRESH_MIN minutes to conserve API credits.

  python supercoach_feed.py           # refresh if stale
  python supercoach_feed.py --force   # refresh now
"""
import json, sys, re
from pathlib import Path
from datetime import datetime, timezone, timedelta

import requests

BASE = Path(__file__).parent
OUT = BASE / "supercoach_tweets.json"
REFRESH_MIN = 30
KEEP = 6

# AFL signal required (so we don't surface NRL/cricket #supercoach).
AFL_RE = re.compile(r"\bafl\b|aflfantasy|#aflfantasy|dream\s?team|footy|"
                    r"\bmid\b|\bruck\b|\bdefender\b|\bforward\b|break-?even|"
                    r"captain|cash cow|trade|round \d", re.I)
# Drop other codes / women's footy.
BLOCK_RE = re.compile(r"\bnrl\b|rugby league|\bnba\b|cricket|netball|"
                      r"\baflw\b|women", re.I)


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


def stale():
    try:
        prev = json.loads(OUT.read_text(encoding="utf-8"))
        last = datetime.fromisoformat(prev["fetched_at"])
        return (datetime.now(timezone.utc) - last) > timedelta(minutes=REFRESH_MIN)
    except Exception:
        return True


def main():
    if not stale() and "--force" not in sys.argv:
        print("supercoach_feed: recent, skipping")
        return
    env = load_env()
    bearer = env.get("X_BEARER_TOKEN")
    if not bearer:
        print("supercoach_feed: no X_BEARER_TOKEN")
        return
    query = ("#supercoach (AFL OR #AFLFantasy OR fantasy OR footy) "
             "-is:retweet -is:reply -nrl lang:en")
    try:
        r = requests.get(
            "https://api.twitter.com/2/tweets/search/recent",
            params={"query": query, "max_results": 25,
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
    out = []
    for t in d.get("data", []):
        text = (t.get("text") or "").strip()
        if BLOCK_RE.search(text) or not AFL_RE.search(text):
            continue
        if len(re.sub(r"https?://\S+", "", text).strip()) < 25:
            continue  # basically just a link
        u = users.get(t.get("author_id"), {})
        if not u.get("username"):
            continue
        pm = t.get("public_metrics", {})
        out.append({
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
        if len(out) >= KEEP:
            break
    if not out:
        print("supercoach_feed: no AFL #supercoach tweets matched — keeping existing file")
        return
    OUT.write_text(json.dumps(
        {"fetched_at": datetime.now(timezone.utc).isoformat(), "tweets": out},
        indent=2), encoding="utf-8")
    print(f"supercoach_feed: wrote {len(out)} tweets")


if __name__ == "__main__":
    main()
