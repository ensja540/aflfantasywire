#!/usr/bin/env python3
"""
AFLFantasyWire — News Health Check & Fast Fill
==============================================
Runs news_scraper.py repeatedly until the feed is "healthy" (>= 500 items,
chronologically sorted, no generic bodies, no duplicate headlines). On the
first run it can fast-fill the archive with broad AFL RSS articles to reach the
cap quickly.

IMPORTANT — run this STANDALONE, with the auto_scrape loop STOPPED. It writes
and commits news.json and runs news_scraper.py; if the loop is also running,
the two will clobber each other's news.json (concurrent writers corrupt it).

Usage:
    python news_health_check.py
"""
import json
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

BASE_DIR  = Path(__file__).parent
NEWS_PATH = BASE_DIR / "news.json"
TARGET    = 500


def check_news_health():
    try:
        with open(NEWS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        news = data.get("news", data) if isinstance(data, dict) else data

        total = len(news)

        # Chronological order (newest first) — compare adjacent timestamps
        times = [n.get("first_seen") or n.get("time", "") for n in news]
        is_sorted = all(times[i] >= times[i + 1]
                        for i in range(len(times) - 1)
                        if times[i] and times[i + 1])

        types = Counter(n.get("type", "?") for n in news)
        has_selection = types.get("selection", 0) > 0
        has_injury    = types.get("injury", 0) > 0

        generic = sum(1 for n in news
                      if "monitor team news" in (n.get("body", "") or "").lower())

        headlines = [n.get("headline", "") for n in news]
        duplicates = len(headlines) - len(set(headlines))

        return {
            "total": total,
            "target": TARGET,
            "is_sorted": is_sorted,
            "has_selection": has_selection,
            "has_injury": has_injury,
            "generic_count": generic,
            "duplicate_count": duplicates,
            "healthy": total >= TARGET and is_sorted and generic == 0 and duplicates == 0,
            "types": dict(types),
        }
    except Exception as e:
        return {"error": str(e), "healthy": False, "total": 0}


def _git(*args):
    try:
        subprocess.run(["git", *args], cwd=str(BASE_DIR), timeout=120,
                       capture_output=True, text=True)
    except Exception as e:
        print(f"  git {' '.join(args)} failed: {e}")


def _fast_fill_to_target():
    """Bulk the archive toward TARGET using news_scraper.fast_fill(), then
    commit & push. Returns the post-fill count."""
    import news_scraper

    health = check_news_health()
    if health.get("total", 0) >= TARGET:
        return health["total"]

    print(f"Only {health.get('total', 0)} items — running fast fill to reach {TARGET}...")
    fast_items = news_scraper.fast_fill()

    try:
        with open(NEWS_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {"news": []}
    existing = data.get("news", []) if isinstance(data, dict) else data

    existing_headlines = {n.get("headline", "") for n in existing}
    new_items = [i for i in fast_items if i.get("headline") not in existing_headlines]

    merged = existing + new_items
    merged.sort(key=lambda x: x.get("first_seen") or x.get("time") or "", reverse=True)
    merged = merged[:TARGET]

    if isinstance(data, dict):
        data["news"] = merged
        data["item_count"] = len(merged)
    else:
        data = {"news": merged, "item_count": len(merged)}

    with open(NEWS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"Fast fill complete: {len(merged)} items in news.json "
          f"(+{len(new_items)} new)")

    _git("add", "news.json")
    _git("commit", "-m", f"fast fill news to {len(merged)} items")
    _git("push")
    return len(merged)


def run_until_healthy():
    print("=== News Health Check ===")

    # First pass: bulk-fill the archive toward the target.
    _fast_fill_to_target()

    max_attempts = 20
    attempt = 0
    while attempt < max_attempts:
        attempt += 1
        health = check_news_health()

        if health.get("error"):
            print(f"Attempt {attempt}: ERROR — {health['error']}")
        else:
            print(f"Attempt {attempt}: {health['total']}/{TARGET} items | "
                  f"sorted:{health['is_sorted']} | generic:{health['generic_count']} | "
                  f"dupes:{health['duplicate_count']} | types:{health['types']}")

        if health.get("healthy"):
            print("✓ News feed is healthy!")
            break

        if health.get("total", 0) < TARGET:
            print(f"  → Need {TARGET - health.get('total', 0)} more items")
        if not health.get("is_sorted"):
            print("  → Items not in chronological order")
        if health.get("generic_count", 0) > 0:
            print(f"  → {health['generic_count']} generic body text items to remove")
        if health.get("duplicate_count", 0) > 0:
            print(f"  → {health['duplicate_count']} duplicate headlines to remove")

        print("  Running news_scraper.py...")
        result = subprocess.run(
            [sys.executable, "news_scraper.py"],
            cwd=str(BASE_DIR),
            timeout=600,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            print(f"  news_scraper.py failed: {result.stderr[-200:]}")

        time.sleep(5)
    else:
        print(f"Max attempts reached. Final count: {check_news_health().get('total', 0)}/{TARGET}")

    health = check_news_health()
    print("\n=== Final Status ===")
    print(f"Total items: {health.get('total', 0)}/{TARGET}")
    print(f"Chronologically sorted: {health.get('is_sorted')}")
    print(f"Type breakdown: {health.get('types')}")
    print(f"Generic items: {health.get('generic_count', 0)}")
    print(f"Duplicates: {health.get('duplicate_count', 0)}")


if __name__ == "__main__":
    run_until_healthy()
