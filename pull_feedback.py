#!/usr/bin/env python3
"""Pull in-app feature suggestions from the Worker into the local repo.

The site's bottom-right "Suggest a feature" widget POSTs to /api/feedback
(Cloudflare Worker, stored in the SUBS KV). This script — run each auto_scrape
cycle — fetches them via the secret-gated GET endpoint (same PUSH_LIST_SECRET as
notify.py) and merges them into:

  * feature_requests.json  — machine-readable, deduped by id (source of truth)
  * feature_requests.md    — readable digest, newest first

Both are gitignored runtime state: they live on the home machine so Claude can
read and relay them, but user suggestions aren't published to the public repo.
"""
import json
import os
import datetime as _dt

import requests

BASE = os.path.dirname(os.path.abspath(__file__))
JSON_PATH = os.path.join(BASE, "feature_requests.json")
MD_PATH = os.path.join(BASE, "feature_requests.md")
TIMEOUT = 15


def _load_env():
    path = os.path.join(BASE, ".env")
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _write_md(items):
    lines = [
        "# Feature suggestions (from the in-app widget)",
        "",
        "Pulled from aflfantasywire.com/api/feedback. Newest first.",
        "",
    ]
    for it in items:
        at = (it.get("at") or "")[:16].replace("T", " ")
        page = it.get("page") or ""
        page_bit = " `%s`" % page if page else ""
        text = (it.get("text") or "").replace("\n", " ").strip()
        lines.append("- **%s**%s  \n  %s" % (at, page_bit, text))
    tmp = MD_PATH + ".tmp"
    open(tmp, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    os.replace(tmp, MD_PATH)


def run():
    _load_env()
    base = os.environ.get("AFW_BASE", "https://aflfantasywire.com").rstrip("/")
    secret = os.environ.get("PUSH_LIST_SECRET")
    if not secret:
        print("feedback: PUSH_LIST_SECRET missing in .env; nothing to do.")
        return
    try:
        r = requests.get(base + "/api/feedback", params={"secret": secret}, timeout=TIMEOUT)
        r.raise_for_status()
        remote = r.json().get("feedback", [])
    except Exception as e:
        print("feedback: fetch failed: %s" % e)
        return

    try:
        existing = json.load(open(JSON_PATH, encoding="utf-8"))
    except Exception:
        existing = []
    by_id = {it.get("id"): it for it in existing if it.get("id")}

    new = 0
    now = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    for it in remote:
        rid = it.get("id")
        if rid and rid not in by_id:
            it["pulled_at"] = now
            by_id[rid] = it
            new += 1

    items = sorted(by_id.values(), key=lambda x: x.get("at", ""), reverse=True)
    if new == 0 and os.path.exists(JSON_PATH):
        print("feedback: no new suggestions (%d total)." % len(items))
        return

    tmp = JSON_PATH + ".tmp"
    open(tmp, "w", encoding="utf-8").write(json.dumps(items, indent=2, ensure_ascii=False))
    os.replace(tmp, JSON_PATH)
    _write_md(items)
    print("feedback: %d new suggestion(s), %d total." % (new, len(items)))


if __name__ == "__main__":
    run()
