#!/usr/bin/env python3
"""
AFLFantasyWire — News History & Status Tracker
================================================
Tracks news items across scrape runs to detect:

  NEW      — first time we've seen this player + category combo
  UPDATE   — same player + category, but status or detail changed
  ONGOING  — same player + category, no meaningful change since last scrape
  RESOLVED — player was injured/flagged but is now cleared

Also maintains a persistent injury register so we know:
  - When each issue was first reported
  - How long it has been ongoing
  - Whether it has been resolved

Storage: news_history.json (same folder as this script)
         Persists between scraper runs.

Usage:
  from news_history import NewsHistory
  history = NewsHistory()
  items_with_status = history.process(new_items)
  history.save()
"""

import json
import re
import hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE_DIR     = Path(__file__).parent
HISTORY_PATH = BASE_DIR / "news_history.json"


def _key(item):
    """
    Stable key for an item based on player + category.
    Two items with the same key are about the same issue.
    """
    pid  = str(item.get("pid") or "")
    cat  = item.get("category", "general")
    # For non-player items (price updates, general news) use headline hash
    if not pid:
        h = hashlib.md5(item.get("headline","")[:60].encode()).hexdigest()[:8]
        return f"noPlayer_{cat}_{h}"
    return f"p{pid}_{cat}"


def _fingerprint(item):
    """
    Short hash of the item's meaningful content.
    If this changes between scrapes → it's an UPDATE.
    """
    content = " ".join([
        item.get("headline",""),
        item.get("category",""),
        str(item.get("signal","")),
        " ".join(item.get("tags",[])[:3]),
    ])
    return hashlib.md5(content.encode()).hexdigest()[:12]


def _status_changed(old, new):
    """
    Returns True if the story has meaningfully changed.
    e.g. TBC → OUT, or fit → TBC.
    """
    old_tags = set(t.lower() for t in (old.get("tags") or []))
    new_tags = set(t.lower() for t in (new.get("tags") or []))
    old_cat  = old.get("category","")
    new_cat  = new.get("category","")

    # Category change = meaningful update
    if old_cat != new_cat:
        return True

    # Signal change = meaningful update
    if old.get("signal") != new.get("signal"):
        return True

    # Key tag changes
    key_tags = {"out","tbc","fit","named","omitted","vest","role change","suspended"}
    if (old_tags & key_tags) != (new_tags & key_tags):
        return True

    return False


class NewsHistory:
    """Tracks news items across scraper runs."""

    def __init__(self):
        self.data = self._load()

    def _load(self):
        if HISTORY_PATH.exists():
            try:
                return json.loads(HISTORY_PATH.read_text())
            except Exception:
                pass
        return {
            "version":  1,
            "updated":  None,
            "items":    {},   # key → {first_seen, last_seen, last_fp, last_item, seen_count}
            "resolved": [],   # items that were flagged then cleared
        }

    def save(self):
        self.data["updated"] = datetime.now(timezone.utc).isoformat()
        HISTORY_PATH.write_text(json.dumps(self.data, indent=2))

    def process(self, items):
        """
        Takes a list of new scraped items.
        Adds status, age_label, and first_seen fields to each.
        Returns the enriched list.
        """
        now     = datetime.now(timezone.utc)
        now_str = now.isoformat()
        result  = []

        for item in items:
            key = _key(item)
            fp  = _fingerprint(item)

            if key not in self.data["items"]:
                # ── NEW ──
                item["status"]      = "new"
                item["status_label"]= "🔴 New"
                item["first_seen"]  = now_str
                item["age_label"]   = "Just in"
                item["seen_count"]  = 1

                self.data["items"][key] = {
                    "first_seen": now_str,
                    "last_seen":  now_str,
                    "last_fp":    fp,
                    "last_item":  item,
                    "seen_count": 1,
                }

            else:
                existing = self.data["items"][key]
                first_dt = datetime.fromisoformat(existing["first_seen"])
                age_hrs  = (now - first_dt).total_seconds() / 3600
                age_days = age_hrs / 24
                seen_ct  = existing.get("seen_count", 1) + 1

                # Build age label
                if age_hrs < 1:
                    age_label = f"{int(age_hrs*60)}m ago"
                elif age_hrs < 24:
                    age_label = f"{int(age_hrs)}h ago"
                elif age_days < 7:
                    age_label = f"{int(age_days)}d ongoing"
                else:
                    age_label = f"{int(age_days//7)}w ongoing"

                item["first_seen"]  = existing["first_seen"]
                item["age_label"]   = age_label
                item["seen_count"]  = seen_ct

                if existing["last_fp"] != fp and _status_changed(existing["last_item"], item):
                    # ── UPDATE ──
                    item["status"]       = "update"
                    item["status_label"] = "🟡 Updated"
                    item["prev_status"]  = existing["last_item"].get("category","")
                else:
                    # ── ONGOING ──
                    item["status"]       = "ongoing"
                    item["status_label"] = f"🔁 Ongoing · {age_label}"

                # Update history record
                existing["last_seen"]  = now_str
                existing["last_fp"]    = fp
                existing["last_item"]  = item
                existing["seen_count"] = seen_ct

            result.append(item)

        # ── Detect RESOLVED items ──
        # Any item in history that is an injury/selection and
        # NOT in the current scrape for 24+ hours = potentially resolved
        active_keys = {_key(item) for item in items}
        for key, record in self.data["items"].items():
            if key in active_keys:
                continue
            last_item = record.get("last_item", {})
            last_cat  = last_item.get("category","")
            last_dt   = datetime.fromisoformat(record["last_seen"])
            hours_gone = (now - last_dt).total_seconds() / 3600

            # If it was an injury/selection item and hasn't been seen in 24h
            if last_cat in ("injury_out","injury_tbc","vest_risk","dropped") and hours_gone > 24:
                resolved = {
                    **last_item,
                    "status":       "resolved",
                    "status_label": "✅ Resolved",
                    "resolved_at":  now_str,
                    "headline":     f"{last_item.get('player','')} — cleared / returned to play",
                    "body":         f"{last_item.get('player','')} no longer appearing on injury/selection watch. Assumed fit.",
                    "type":         "selection",
                    "signal":       "buy",
                    "urgent":       False,
                }
                result.append(resolved)
                self.data["resolved"].append(resolved)
                # Remove from active tracking
                del self.data["items"][key]

        # Sort: new/update first, then ongoing by relevance
        def sort_key(x):
            order = {"new":0, "update":1, "resolved":2, "ongoing":3}
            return (order.get(x.get("status","ongoing"), 3), -x.get("relevance",0))

        result.sort(key=sort_key)
        return result

    def summary(self):
        """Print a summary of tracked items."""
        items = self.data["items"]
        cats  = {}
        for record in items.values():
            cat = record["last_item"].get("category","?")
            cats[cat] = cats.get(cat, 0) + 1
        print(f"Tracking {len(items)} active issues:")
        for cat, count in sorted(cats.items()):
            print(f"  {cat}: {count}")
        print(f"Resolved: {len(self.data.get('resolved',[]))}")


if __name__ == "__main__":
    # Test with mock data
    history = NewsHistory()

    mock_items = [
        {
            "pid": 5, "player": "Errol Gulden", "category": "injury_out",
            "headline": "Gulden OUT — hamstring confirmed",
            "signal": "sell", "tags": ["OUT","Hamstring"], "relevance": 90,
            "type": "injury",
        },
        {
            "pid": 4, "player": "Patrick Cripps", "category": "injury_tbc",
            "headline": "Cripps TBC with hip flexor",
            "signal": "hold", "tags": ["TBC","Hip flexor"], "relevance": 60,
            "type": "injury",
        },
        {
            "pid": 2, "player": "Nick Daicos", "category": "named",
            "headline": "Daicos named unchanged for Round 11",
            "signal": None, "tags": ["Named"], "relevance": 30,
            "type": "selection",
        },
    ]

    print("=== First run (all NEW) ===")
    processed = history.process(mock_items)
    for item in processed:
        print(f"  [{item['status_label']}] {item['headline'][:60]}")

    history.save()

    print("\n=== Second run (should be ONGOING) ===")
    processed2 = history.process(mock_items)
    for item in processed2:
        print(f"  [{item['status_label']}] {item['headline'][:60]}")

    print("\n=== Third run with status change (Cripps now OUT) ===")
    mock_items[1]["category"] = "injury_out"
    mock_items[1]["tags"]     = ["OUT","Hip flexor","Miss R11"]
    mock_items[1]["signal"]   = "sell"
    processed3 = history.process(mock_items)
    for item in processed3:
        print(f"  [{item['status_label']}] {item['headline'][:60]}")

    history.save()
    print()
    history.summary()
