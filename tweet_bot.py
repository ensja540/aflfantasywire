#!/usr/bin/env python3
"""
AFLFantasyWire — Tweet bot
==========================
Generates ~5 brand-compliant AFL fantasy tweets from players.json / news.json
and posts them to X (Twitter).

BRAND RULES (enforced here, not free-text — so we can't hallucinate):
  - Every tweet ends with "#SuperCoach #AFLFantasy".
  - Tone: informative + a light steer ("one to keep an eye on", "worth a look").
  - No slang / dismissive terms.
  - Tweets are built ONLY from verifiable numbers (scores, 3-rd avg, season avg,
    break-even, price, ownership). We NEVER state a cause/role/why a score moved.
  - Classic tweets may use price / break-even / ownership. Draft tweets use form
    only — never price/BE/ownership.
  - Breaking only when an item is genuinely fresh (NewsHistory status == "new").

USAGE
  python tweet_bot.py            # preview only (prints the 5 tweets, posts nothing)
  python tweet_bot.py --post     # generate AND post to X

Credentials come from repo-root .env:
  X_CONSUMER_KEY / X_CONSUMER_SECRET / X_ACCESS_TOKEN / X_ACCESS_TOKEN_SECRET
"""
import json, sys, random
from pathlib import Path
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = Path(__file__).parent
HASHTAGS = "#SuperCoach #AFLFantasy"
TWEETED_LOG = BASE / "tweeted.json"
DAILY_TARGET = 5
RISE_GAP = 18   # 3-rd avg minus season avg to count as "rising"
FALL_GAP = -18


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


def _load(name, key):
    try:
        d = json.loads((BASE / name).read_text(encoding="utf-8"))
        return d.get(key, d) if isinstance(d, dict) else d
    except Exception:
        return []


def money(p):
    p = int(p or 0)
    if p >= 1_000_000:
        return f"${p/1_000_000:.1f}m".replace(".0m", "m")
    return f"${round(p/1000)}k"


def scoreline(scores, n=5):
    return "-".join(str(int(s)) for s in scores[-n:])


def played_scores(p):
    return [s for s in (p.get("scores") or []) if s and s > 0]


def classic_tweets(players):
    out = []
    for p in players:
        avg = p.get("scAvg") or 0
        avg3 = p.get("scAvg3") or 0
        be = p.get("breakeven") or 0
        price = p.get("price") or 0
        own = p.get("owned") or 0
        if not avg or len(played_scores(p)) < 3:
            continue
        gap = avg3 - avg
        if gap >= RISE_GAP and 0 < be < avg3:
            own_bit = f", and sits at {own}% ownership" if own else ""
            out.append(("classic", p["id"], "crise",
                        f"{p['name']} is averaging {round(avg3)} over his past three rounds "
                        f"({round(avg)} for the season) with a break-even of {be} at {money(price)}"
                        f"{own_bit}. One to keep an eye on. {HASHTAGS}"))
        elif gap <= FALL_GAP and be > avg3:
            out.append(("classic", p["id"], "cfall",
                        f"{p['name']}'s three-round average has eased to {round(avg3)} "
                        f"({round(avg)} for the year) with a break-even of {be} — his price looks "
                        f"under pressure. Something to weigh up. {HASHTAGS}"))
    return out


def draft_tweets(players):
    out = []
    for p in players:
        ps = played_scores(p)
        if len(ps) < 4:
            continue
        avg = p.get("scAvg") or 0
        avg3 = p.get("scAvg3") or 0
        gap = avg3 - avg
        last5 = ps[-5:]
        if len(last5) >= 5 and min(last5) >= 85 and avg3 >= 100:
            out.append(("draft", p["id"], "dcons",
                        f"{p['name']} hasn't dropped below {min(last5)} in his past five "
                        f"({scoreline(ps)}). Steady production worth a look in Draft. {HASHTAGS}"))
        elif gap >= RISE_GAP:
            out.append(("draft", p["id"], "drise",
                        f"{p['name']} has lifted his recent output: {scoreline(ps)} across his "
                        f"past games. Form worth keeping an eye on for Draft sides. {HASHTAGS}"))
        elif gap <= FALL_GAP:
            out.append(("draft", p["id"], "dfall",
                        f"{p['name']}'s recent output has eased ({scoreline(ps)}). "
                        f"Form to monitor before locking him into your Draft side. {HASHTAGS}"))
    return out


def breaking_tweets(news):
    out = []
    for it in news:
        if (it.get("type") == "injury" and it.get("status") == "new"
                and it.get("player") and it.get("pid")):
            bp = ""
            tags = it.get("tags") or []
            if len(tags) > 1 and tags[1]:
                bp = str(tags[1])
            detail = f" ({bp})" if bp else ""
            out.append(("breaking", it.get("pid"), "binj",
                        f"Team news: {it['player']} is listed on the injury list{detail}. "
                        f"One to check before your team locks. {HASHTAGS}"))
    return out


def load_log():
    try:
        return json.loads(TWEETED_LOG.read_text(encoding="utf-8"))
    except Exception:
        return {"posted": []}


def aest_now():
    """Current time in Melbourne (handles AEST/AEDT); falls back to UTC+10."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Australia/Melbourne"))
    except Exception:
        from datetime import timezone, timedelta
        return datetime.now(timezone.utc) + timedelta(hours=10)


def should_auto_post(log):
    """Gate for --auto: post only during 6am–11pm AEST, max DAILY_TARGET/day,
    spaced so 5 tweets spread across the ~17h window (≈3h apart). Returns
    (ok, reason)."""
    now = aest_now()
    if not (6 <= now.hour < 23):
        return False, f"outside posting window (AEST {now:%H:%M})"
    today = now.strftime("%Y-%m-%d")
    todays = [e for e in log.get("posted", []) if (e.get("at_aest", "")[:10] == today)]
    if len(todays) >= DAILY_TARGET:
        return False, f"already posted {len(todays)}/{DAILY_TARGET} today"
    if todays:
        last = max(e.get("at_aest", "") for e in todays)
        try:
            gap_h = (now - datetime.fromisoformat(last)).total_seconds() / 3600
            if gap_h < 2.8:
                return False, f"only {gap_h:.1f}h since last (spacing ~3h)"
        except Exception:
            pass
    return True, f"clear to post ({len(todays)}/{DAILY_TARGET} today, AEST {now:%H:%M})"


def pick(players, news, log):
    """Pick up to DAILY_TARGET varied tweets not posted in the last 14 days."""
    recent = {(e["pid"], e["angle"]) for e in log.get("posted", [])[-200:]}
    pools = {
        "breaking": breaking_tweets(news),
        "classic": classic_tweets(players),
        "draft": draft_tweets(players),
    }
    # Rank classic/draft by how strong the move is (biggest |avg3-avg| first).
    pid_gap = {p["id"]: abs((p.get("scAvg3") or 0) - (p.get("scAvg") or 0)) for p in players}
    for k in ("classic", "draft"):
        pools[k].sort(key=lambda t: pid_gap.get(t[1], 0), reverse=True)

    chosen, used_pids = [], set()
    # Order of preference: up to 2 breaking, then alternate classic/draft.
    order = (["breaking"] * 2) + (["classic", "draft"] * DAILY_TARGET)
    for kind in order:
        if len(chosen) >= DAILY_TARGET:
            break
        for cand in pools.get(kind, []):
            _, pid, angle, text = cand
            if (pid, angle) in recent or pid in used_pids or len(text) > 278:
                continue
            chosen.append(cand)
            used_pids.add(pid)
            pools[kind].remove(cand)
            break
    return chosen


def post_tweet(text, env):
    from requests_oauthlib import OAuth1Session
    oauth = OAuth1Session(
        env["X_CONSUMER_KEY"], client_secret=env["X_CONSUMER_SECRET"],
        resource_owner_key=env["X_ACCESS_TOKEN"], resource_owner_secret=env["X_ACCESS_TOKEN_SECRET"],
    )
    r = oauth.post("https://api.twitter.com/2/tweets", json={"text": text}, timeout=30)
    return r.status_code, r.text


def main():
    do_post = "--post" in sys.argv
    count = DAILY_TARGET
    for a in sys.argv:
        if a.startswith("--count="):
            try:
                count = int(a.split("=", 1)[1])
            except Exception:
                pass
    players = _load("players.json", "players")
    news = _load("news.json", "news")
    log = load_log()
    if "--auto" in sys.argv:
        ok, why = should_auto_post(log)
        print(f"[auto] {why}")
        if not ok:
            return
        do_post, count = True, 1
    chosen = pick(players, news, log)[:count]

    print(f"=== {len(chosen)} tweets ({'POSTING' if do_post else 'PREVIEW'}) ===")
    for i, (kind, pid, angle, text) in enumerate(chosen, 1):
        print(f"\n[{i}] {kind} ({len(text)} chars)\n{text}")

    if not do_post:
        print("\n(preview only — run with --post to publish)")
        return

    env = load_env()
    for cred in ("X_CONSUMER_KEY", "X_CONSUMER_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"):
        if not env.get(cred):
            print(f"Missing {cred} in .env — cannot post.")
            return

    posted = log.get("posted", [])
    for kind, pid, angle, text in chosen:
        code, body = post_tweet(text, env)
        if code in (200, 201):
            tid = ""
            try:
                tid = json.loads(body).get("data", {}).get("id", "")
            except Exception:
                pass
            print(f"  [ok] posted ({tid}): {text[:60]}")
            posted.append({"pid": pid, "angle": angle, "id": tid,
                           "at": datetime.now().isoformat(),
                           "at_aest": aest_now().isoformat(), "text": text})
        else:
            print(f"  [FAIL] ({code}): {body[:300]}")
            # Stop on auth/credit errors so we don't hammer.
            if code in (401, 402, 403):
                break
    log["posted"] = posted
    TWEETED_LOG.write_text(json.dumps(log, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
