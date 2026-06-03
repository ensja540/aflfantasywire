# -*- coding: utf-8 -*-
"""Web Push sender for AFLFantasyWire.

Reads the push subscriptions stored by the Cloudflare Worker (each carries the
device's watchlist of player ids), finds freshly-scraped news that tags a
watchlisted player, and sends a Web Push notification via pywebpush. Dead
subscriptions (HTTP 404/410) are pruned by telling the Worker to forget them.

Designed to be run after each scrape (auto_scrape.py) or standalone:
    venv/Scripts/python.exe notify.py

Required .env entries (the .env file is gitignored; never commit secrets):
    PUSH_LIST_SECRET=<same value set as the Worker's PUSH_LIST_SECRET secret>
    VAPID_PRIVATE_KEY=<the pkcs8 base64url private key>
    VAPID_SUBJECT=mailto:ensor.jack@gmail.com
    AFW_BASE=https://aflfantasywire.com        # optional, this is the default
"""

import base64
import datetime as _dt
import json
import os
import sys
import hashlib

import requests

# --- config -----------------------------------------------------------------

BASE = "C:/aflfantasywire"
NEWS_PATH = os.path.join(BASE, "news.json")
STATE_PATH = os.path.join(BASE, "notify_sent.json")  # gitignored runtime state

# Only consider news scraped/published within this window as "pushable" so the
# first run (or a fresh subscriber) doesn't get blasted with the whole backlog.
FRESH_HOURS = 24
# Categories we never push on their own (puff pieces); urgent items override.
SKIP_CATEGORIES = {"general", ""}
# Forget per-endpoint sent ids older than this (keeps the state file bounded).
STATE_TTL_DAYS = 10
SEND_TIMEOUT = 10


def _load_env():
    """Minimal .env loader (so we don't depend on python-dotenv)."""
    path = os.path.join(BASE, ".env")
    if os.path.isfile(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _b64url_decode(s):
    s = s + "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s.encode("ascii"))


def _build_vapid(priv_b64):
    """Build a py_vapid Vapid01 from the pkcs8 base64url private key."""
    from cryptography.hazmat.primitives.serialization import load_der_private_key
    from py_vapid import Vapid01

    key = load_der_private_key(_b64url_decode(priv_b64), password=None)
    v = Vapid01()
    v.private_key = key
    return v


def _news_items():
    raw = json.load(open(NEWS_PATH, encoding="utf-8"))
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for k in ("items", "news", "feed"):
            if isinstance(raw.get(k), list):
                return raw[k]
        # otherwise: first list-of-dicts value
        for v in raw.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
    return []


def _item_time(it):
    iso = it.get("pubISO") or it.get("scrapedAt") or it.get("time")
    if not iso:
        return None
    try:
        return _dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except Exception:
        return None


def _is_fresh(it):
    t = _item_time(it)
    if t is None:
        return True  # no timestamp -> don't exclude on age
    if t.tzinfo is None:
        t = t.replace(tzinfo=_dt.timezone.utc)
    age = _dt.datetime.now(_dt.timezone.utc) - t
    return age <= _dt.timedelta(hours=FRESH_HOURS)


def _pushable(it):
    if not _is_fresh(it):
        return False
    if it.get("urgent"):
        return True
    cat = str(it.get("category") or "").strip().lower()
    return cat not in SKIP_CATEGORIES


def _stable_nid(it):
    """Content-stable notification id for dedup.

    The served `id` is reassigned positionally on EVERY scrape (news_scraper
    does `item["id"] = i`), so deduping on it re-fires the same story whenever
    the feed reorders — which is why a single article (e.g. Jye Caldwell's) kept
    notifying over and over. Key on the article link instead (or player+headline
    when there's no link) so each story notifies a given subscriber exactly once.
    """
    link = (it.get("link") or "").strip()
    if link:
        basis = "L:" + link
    else:
        basis = "P:%s|%s" % (
            it.get("pid") or it.get("player") or "",
            (it.get("headline") or it.get("body") or "")[:120],
        )
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


def _item_pids(it):
    pids = set()
    for p in it.get("players") or []:
        if isinstance(p, dict) and p.get("pid") is not None:
            pids.add(str(p["pid"]))
    if it.get("pid") is not None:
        pids.add(str(it["pid"]))
    return pids


def _load_state():
    if os.path.isfile(STATE_PATH):
        try:
            return json.load(open(STATE_PATH, encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_state(state):
    cutoff = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=STATE_TTL_DAYS)).timestamp()
    for ep, sent in list(state.items()):
        state[ep] = {nid: ts for nid, ts in sent.items() if ts >= cutoff}
        if not state[ep]:
            del state[ep]
    json.dump(state, open(STATE_PATH, "w", encoding="utf-8"))


def _endpoint_key(endpoint):
    import hashlib
    return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()[:16]


TEST_PAYLOAD = {
    "title": "Test — AFLFantasyWire",
    "body": "Push notifications are working!",
    "url": "https://aflfantasywire.com",
    "tag": "afw-test",
}


def run_test():
    """Send one fixed test notification to every stored subscriber, ignoring
    watchlists and freshness. Verifies the push pipeline end-to-end:
        python notify.py --test
    """
    _load_env()
    base = os.environ.get("AFW_BASE", "https://aflfantasywire.com").rstrip("/")
    secret = os.environ.get("PUSH_LIST_SECRET")
    priv = os.environ.get("VAPID_PRIVATE_KEY")
    subject = os.environ.get("VAPID_SUBJECT")
    if not (secret and priv and subject):
        print("notify: missing PUSH_LIST_SECRET / VAPID_PRIVATE_KEY / VAPID_SUBJECT in .env; cannot send test.")
        return

    from pywebpush import webpush, WebPushException

    vapid = _build_vapid(priv)
    claims_base = {"sub": subject}

    try:
        r = requests.get(base + "/api/subscriptions", params={"secret": secret}, timeout=SEND_TIMEOUT)
        r.raise_for_status()
        subs = r.json().get("subscriptions", [])
    except Exception as e:
        print("notify: could not fetch subscriptions:", e)
        return
    if not subs:
        print("notify: no subscriptions stored; nothing to test.")
        return

    payload = json.dumps(TEST_PAYLOAD)
    sent = 0
    pruned = 0
    for entry in subs:
        sub = entry.get("subscription") or {}
        endpoint = sub.get("endpoint")
        if not endpoint:
            continue
        try:
            webpush(subscription_info=sub, data=payload,
                    vapid_private_key=vapid, vapid_claims=dict(claims_base),
                    timeout=SEND_TIMEOUT)
            sent += 1
        except WebPushException as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code in (404, 410):
                try:
                    requests.post(base + "/api/unsubscribe",
                                  json={"endpoint": endpoint}, timeout=SEND_TIMEOUT)
                except Exception:
                    pass
                pruned += 1
            else:
                print("notify: test send failed (%s) for %s" % (code, _endpoint_key(endpoint)))
    print("notify: test sent %d notification(s); pruned %d dead subscription(s)." % (sent, pruned))


def run():
    _load_env()
    base = os.environ.get("AFW_BASE", "https://aflfantasywire.com").rstrip("/")
    secret = os.environ.get("PUSH_LIST_SECRET")
    priv = os.environ.get("VAPID_PRIVATE_KEY")
    subject = os.environ.get("VAPID_SUBJECT")
    if not (secret and priv and subject):
        print("notify: missing PUSH_LIST_SECRET / VAPID_PRIVATE_KEY / VAPID_SUBJECT in .env; nothing to do.")
        return

    from pywebpush import webpush, WebPushException

    vapid = _build_vapid(priv)
    claims_base = {"sub": subject}

    # 1) pull subscriptions from the Worker
    try:
        r = requests.get(base + "/api/subscriptions", params={"secret": secret}, timeout=SEND_TIMEOUT)
        r.raise_for_status()
        subs = r.json().get("subscriptions", [])
    except Exception as e:
        print("notify: could not fetch subscriptions:", e)
        return
    if not subs:
        print("notify: no subscriptions stored.")
        return

    items = [it for it in _news_items() if _pushable(it)]
    if not items:
        print("notify: no fresh pushable news.")
        return

    state = _load_state()
    now_ts = _dt.datetime.now(_dt.timezone.utc).timestamp()
    total_sent = 0
    total_pruned = 0

    for entry in subs:
        sub = entry.get("subscription") or {}
        endpoint = sub.get("endpoint")
        if not endpoint:
            continue
        watch = set(str(x) for x in (entry.get("watchlist") or []))
        if not watch:
            continue
        ekey = _endpoint_key(endpoint)
        already = state.setdefault(ekey, {})

        for it in items:
            nid = _stable_nid(it)
            if nid in already:
                continue
            matched = _item_pids(it) & watch
            if not matched:
                continue
            name = ""
            for p in it.get("players") or []:
                if isinstance(p, dict) and str(p.get("pid")) in matched:
                    name = p.get("name") or ""
                    break
            name = name or it.get("player") or "Your watchlist"
            headline = (it.get("headline") or it.get("body") or "").strip()
            payload = json.dumps({
                "title": "\U0001F514 " + name,
                "body": headline[:180],
                "url": it.get("link") or base,
                "tag": "afw-" + nid,
            })
            try:
                webpush(subscription_info=sub, data=payload,
                        vapid_private_key=vapid, vapid_claims=dict(claims_base),
                        timeout=SEND_TIMEOUT)
                already[nid] = now_ts
                total_sent += 1
            except WebPushException as e:
                code = getattr(getattr(e, "response", None), "status_code", None)
                if code in (404, 410):
                    # subscription is dead: ask the Worker to forget it
                    try:
                        requests.post(base + "/api/unsubscribe",
                                      json={"endpoint": endpoint}, timeout=SEND_TIMEOUT)
                    except Exception:
                        pass
                    state.pop(ekey, None)
                    total_pruned += 1
                    break  # stop processing this (dead) endpoint
                else:
                    print("notify: send failed (%s) for %s" % (code, ekey))

    _save_state(state)
    print("notify: sent %d notification(s); pruned %d dead subscription(s)." % (total_sent, total_pruned))


if __name__ == "__main__":
    try:
        if "--test" in sys.argv[1:]:
            run_test()
        else:
            run()
    except Exception as e:
        print("notify: fatal:", e)
        sys.exit(1)
