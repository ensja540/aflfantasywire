#!/usr/bin/env python3
"""
AFLFantasyWire — #SuperCoach feed
=================================
Pulls recent AFL #SuperCoach tweets via the X v2 recent-search API and ADDS the
newest relevant ones to supercoach_tweets.json (accumulating, deduped by id,
newest first, capped at KEEP). Run once per tweet we post (the tweet_bot calls
this with --add=2 after a successful post), so the buzz feed grows alongside our
own posting cadence.

#SuperCoach is also used by NRL/cricket SuperCoach, so the query is AFL-biased
and results are post-filtered to drop other codes, AFLW, and our own account.

  python supercoach_feed.py            # add ADD_DEFAULT new tweets
  python supercoach_feed.py --add=2    # add up to 2 new tweets
"""
import json, sys, re
from pathlib import Path
from datetime import datetime, timezone

import requests

BASE = Path(__file__).parent
OUT = BASE / "supercoach_tweets.json"
KEEP = 8            # max tweets retained in the buzz feed
ADD_DEFAULT = 2     # new tweets added per run

AFL_RE = re.compile(r"\bafl\b|aflfantasy|#aflfantasy|dream\s?team|footy|"
                    r"\bmid\b|\bruck\b|\bdefender\b|\bforward\b|break-?even|"
                    r"captain|cash cow|trade|round \d", re.I)
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


def existing_tweets():
    try:
        return json.loads(OUT.read_text(encoding="utf-8")).get("tweets", [])
    except Exception:
        return []


def main():
    add_n = ADD_DEFAULT
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

    fresh = []
    for t in d.get("data", []):
        if t.get("id") in have_ids:
            continue  # already in the feed
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

    # Accumulate: newest first, dedup by id, cap KEEP.
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
