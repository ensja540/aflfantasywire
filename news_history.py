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
    key_tags = {"out","tbc","fit","named","omitted","vest","role change","suspended","available","test"}
    if (old_tags & key_tags) != (new_tags & key_tags):
        return True

    # ETA wording change ("2-3 weeks" -> "Season") is a real update too
    eta_old = _eta_from_item(old)
    eta_new = _eta_from_item(new)
    if eta_old and eta_new and eta_old != eta_new:
        return True

    return False


def _eta_from_item(item):
    """Pull the ETA tag (3rd tag slot or 'eta' chip)."""
    tags = item.get("tags") or []
    if len(tags) >= 3:
        return (tags[2] or "").strip().lower()
    for s in item.get("stats", []) or []:
        if (s.get("l","").strip().lower() in ("eta","return")):
            return (s.get("v","") or "").strip().lower()
    return ""


# Coarse per-player status used for cross-category change detection
# (e.g. yesterday "named", today "dropped" — different category, same player).
def _player_status_from_item(item):
    cat  = (item.get("category") or "").lower()
    tags = {(t or "").lower() for t in (item.get("tags") or [])}

    if cat == "injury_out"  or "out" in tags:                      return "out"
    if cat == "injury_tbc"  or {"tbc","test","managed"} & tags:    return "test"
    if cat == "vest_risk"   or "vest" in tags or "sub" in tags:    return "vest"
    if cat == "dropped"     or {"omitted","dropped"} & tags:       return "omitted"
    if cat == "role_change" or "role change" in tags:              return "role"
    if cat == "named"       or "named" in tags:                    return "named"
    if cat == "suspension"  or "suspended" in tags:                return "suspended"
    if cat == "injury_available" or {"available","cleared","fit"} & tags: return "available"
    return ""


class NewsHistory:
    """Tracks news items across scraper runs."""

    def __init__(self):
        self.data = self._load()

    def _load(self):
        if HISTORY_PATH.exists():
            try:
                data = json.loads(HISTORY_PATH.read_text())
                # Backfill new top-level keys on older history files
                data.setdefault("player_status",   {})
                data.setdefault("team_selections", {})
                return data
            except Exception:
                pass
        return {
            "version":         2,
            "updated":         None,
            "items":           {},   # key → {first_seen, last_seen, last_fp, last_status, last_item, seen_count}
            "resolved":        [],   # items that were flagged then cleared
            "player_status":   {},   # pid_str → {status, prev_status, last_seen, source, team}
            "team_selections": {},   # club_name → {pid_str: {named, role, last_seen}}
        }

    def save(self):
        self.data["updated"] = datetime.now(timezone.utc).isoformat()
        HISTORY_PATH.write_text(json.dumps(self.data, indent=2))

    def process(self, items):
        """
        Takes a list of new scraped items.
        Adds status, age_label, first_seen, last_status, status_changed fields
        to each. Returns the enriched list (sorted; ongoing items still present
        — call filter_real_time to drop them).
        """
        now     = datetime.now(timezone.utc)
        now_str = now.isoformat()
        result  = []

        for item in items:
            key       = _key(item)
            fp        = _fingerprint(item)
            new_pstat = _player_status_from_item(item)
            pid_str   = str(item.get("pid") or "") or None

            # Cross-category player-level status lookup (e.g. yesterday "named",
            # today "dropped" — different (pid,cat) key but same player).
            prev_pstat = ""
            if pid_str and pid_str in self.data.get("player_status", {}):
                prev_pstat = self.data["player_status"][pid_str].get("status", "")

            if key not in self.data["items"]:
                # ── NEW (or first sighting of this category for the player) ──
                item["status"]         = "new"
                item["status_label"]   = "🔴 New"
                item["first_seen"]     = now_str
                item["age_label"]      = "Just in"
                item["seen_count"]     = 1
                item["last_status"]    = prev_pstat or None
                item["status_changed"] = bool(new_pstat) and new_pstat != prev_pstat

                self.data["items"][key] = {
                    "first_seen": now_str,
                    "last_seen":  now_str,
                    "last_fp":    fp,
                    "last_status": new_pstat,
                    "last_item":  item,
                    "seen_count": 1,
                }

            else:
                existing = self.data["items"][key]
                first_dt = datetime.fromisoformat(existing["first_seen"])
                age_hrs  = (now - first_dt).total_seconds() / 3600
                age_days = age_hrs / 24
                seen_ct  = existing.get("seen_count", 1) + 1
                old_pstat = existing.get("last_status", "") or _player_status_from_item(existing.get("last_item", {}))

                if age_hrs < 1:
                    age_label = f"{int(age_hrs*60)}m ago"
                elif age_hrs < 24:
                    age_label = f"{int(age_hrs)}h ago"
                elif age_days < 7:
                    age_label = f"{int(age_days)}d ongoing"
                else:
                    age_label = f"{int(age_days//7)}w ongoing"

                item["first_seen"] = existing["first_seen"]
                item["age_label"]  = age_label
                item["seen_count"] = seen_ct
                item["last_status"] = old_pstat or None

                content_changed = existing["last_fp"] != fp and _status_changed(existing["last_item"], item)
                player_status_changed = bool(new_pstat) and bool(old_pstat) and new_pstat != old_pstat
                item["status_changed"] = content_changed or player_status_changed

                if content_changed or player_status_changed:
                    # ── UPDATE ──
                    item["status"]       = "update"
                    item["status_label"] = "🟡 Updated"
                    item["prev_status"]  = old_pstat or existing["last_item"].get("category","")
                else:
                    # ── ONGOING (no material change) ──
                    item["status"]       = "ongoing"
                    item["status_label"] = f"🔁 Ongoing · {age_label}"

                existing["last_seen"]   = now_str
                existing["last_fp"]     = fp
                existing["last_status"] = new_pstat or old_pstat
                existing["last_item"]   = item
                existing["seen_count"]  = seen_ct

            # Maintain the cross-category per-player status map so the next
            # scrape can spot transitions like "named" -> "dropped"
            if pid_str and new_pstat:
                ps = self.data.setdefault("player_status", {})
                old_record = ps.get(pid_str, {})
                ps[pid_str] = {
                    "status":      new_pstat,
                    "prev_status": old_record.get("status", "") if old_record.get("status") != new_pstat else old_record.get("prev_status", ""),
                    "team":        item.get("team", "") or old_record.get("team", ""),
                    "last_seen":   now_str,
                    "source":      item.get("source", ""),
                }

            # Track per-club lineup state when we see a definite named/dropped event
            club = item.get("team") or ""
            if pid_str and club and new_pstat in ("named", "omitted", "dropped", "vest", "role"):
                ts = self.data.setdefault("team_selections", {})
                club_book = ts.setdefault(club, {})
                club_book[pid_str] = {
                    "named":     new_pstat == "named",
                    "status":    new_pstat,
                    "role":      item.get("pos", "") or "",
                    "last_seen": now_str,
                }

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

    def filter_real_time(self, items):
        """
        Drop items with status="ongoing" (already-seen story with no material
        change). Keep "new", "update", and "resolved" — those represent
        real-time movement that warrants a feed slot.

        Also drops same-player same-coarse-status duplicates within this batch
        (e.g. two AFL/Footywire scrapers each emitting the same "Cripps TBC"
        once). Higher-reliability source wins; AFL.com.au beats Footywire beats
        the rest, ties broken by item relevance score.
        """
        kept = [i for i in items if i.get("status") in ("new", "update", "resolved")]

        # Dedupe by (pid, coarse_status) within the kept set
        def src_rank(item):
            src = (item.get("source") or "").lower()
            if "afl.com" in src or src == "afl.com.au": return 3
            if "footywire" in src:                       return 2
            return 1

        best = {}
        for item in kept:
            pid    = str(item.get("pid") or "")
            pstat  = _player_status_from_item(item) or item.get("category","")
            if not pid:
                # No-pid items (general news) keyed by headline hash
                pstat = f"{pstat}_{hashlib.md5((item.get('headline') or '').encode()).hexdigest()[:8]}"
            key = f"{pid}|{pstat}"
            existing = best.get(key)
            if existing is None:
                best[key] = item
                continue
            score_new = (src_rank(item),     item.get("reliability", 0), item.get("relevance", 0))
            score_old = (src_rank(existing), existing.get("reliability", 0), existing.get("relevance", 0))
            if score_new > score_old:
                best[key] = item

        return list(best.values())

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
