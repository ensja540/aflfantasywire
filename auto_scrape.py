"""
Auto-scraper loop.

Runs fetch_data.py then news_scraper.py every 5 minutes, commits the resulting
players.json / news.json / news_history.json, and pushes to the remote.

Failures are logged to scrape.log but the loop keeps running — a transient
HTTP 429 from Footywire or a flaky network shouldn't stop the whole job.

Usage:
    python auto_scrape.py
"""

import hashlib
import json
import logging
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

# Child scrapers print ✓/→ glyphs; force UTF-8 in the subprocess environment so
# they don't crash under a cp1252 console.
_UTF8_ENV = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}

BASE_DIR     = Path(__file__).resolve().parent
LOG_PATH     = BASE_DIR / "scrape.log"
SIG_PATH     = BASE_DIR / ".scrape_sig"
INTERVAL_SEC = 15 * 60   # 15 minutes

# Footywire's SuperCoach stats page is the source-of-truth for player
# averages. We hash it once per cycle and only run fetch_data.py (which
# pulls ~350 games-log pages) when the hash changes — i.e., when a new
# game has actually completed and Footywire has processed the scores.
# 12-hour safety refresh covers any case where the hash drifts without us.
FW_SC_STATS_URL          = "https://www.footywire.com/afl/footy/supercoach_stats"
FETCH_DATA_SIG_PATH      = BASE_DIR / ".fetch_data_sig"
FETCH_DATA_MAX_GAP_HOURS = 12

# Per-script subprocess timeout. fetch_data.py fetches ~350 games-log pages
# sequentially with polite delays (~2s/player to avoid Footywire rate-limiting),
# so it needs ~12 min for that phase alone; give comfortable headroom so it isn't
# killed mid-fetch (which would otherwise push news-only with stale player data).
SCRIPT_TIMEOUT_SEC = 20 * 60   # 20 minutes

# Even when the content signature is unchanged, force a push after this many
# consecutive no-change runs (~90 min at a 15-min interval) so the deployed site
# never goes stale and we can confirm the loop is alive from the commit history.
MAX_NO_CHANGE_RUNS = 6
_no_change_streak  = 0

# Run the Claude-powered quality agent (agent_monitor.py) every Nth cycle.
# Interval = 15 min × 2 = ~30 min between agent checks. Agent fixes news.json
# in place; the next push picks them up. It also writes proposed_upstream_fixes
# .json for human review (Pass 2) — never commits or pushes code itself.
AGENT_RUN_EVERY    = 2
_agent_run_counter = 0

# Fields that change every run regardless of real data (timestamps, ids,
# recomputed relevance). Excluded from the change signature so timestamp-only
# churn (e.g. "1h ago" -> "2h ago") doesn't look like a real update.
_VOLATILE_FIELDS = {
    "id", "time", "timeLabel", "scrapedAt", "relevance", "urgent", "status",
    # Per-cycle bookkeeping that changes even when nothing substantive does —
    # excluding these means we only push on real content changes.
    "_scraped_at", "seen_count", "age_label", "first_seen", "last_seen",
    "last_status", "status_changed", "status_label", "stale_after_days",
}


# Detailed log to file, terse status line to stdout.
logging.basicConfig(
    filename=str(LOG_PATH),
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("auto_scrape")


def _status(msg: str) -> None:
    """Print a single status line (overwriting the previous one when possible)."""
    sys.stdout.write(f"\r[{datetime.now().strftime('%H:%M:%S')}] {msg}".ljust(120))
    sys.stdout.flush()


def _endline() -> None:
    sys.stdout.write("\n")
    sys.stdout.flush()


def _fetch_data_check() -> tuple[bool, str | None, str]:
    """Decide whether `fetch_data.py` needs to run this cycle.

    Returns ``(should_run, new_sig_or_None, reason)``. The caller persists
    ``new_sig`` to ``.fetch_data_sig`` after a successful fetch_data run so
    the next cycle can skip if Footywire's stats page is unchanged.
    """
    # 12-hour safety floor: never let player data go stale forever even if
    # Footywire's content hash happens to drift identically.
    try:
        age_hours = (time.time() - FETCH_DATA_SIG_PATH.stat().st_mtime) / 3600
        if age_hours > FETCH_DATA_MAX_GAP_HOURS:
            return True, None, f"safety refresh ({int(age_hours)}h since last run)"
    except FileNotFoundError:
        return True, None, "first run"

    # Cheap probe — single GET of the SC stats page, hash the body.
    try:
        req = urllib.request.Request(
            FW_SC_STATS_URL,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read(200_000)
    except Exception as e:
        # Probe failed — fall back to running so we don't silently stop the
        # data pipeline because of a transient network issue.
        return True, None, f"probe failed ({e})"

    new_sig = hashlib.sha256(body).hexdigest()
    try:
        prev = FETCH_DATA_SIG_PATH.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        prev = ""

    if new_sig == prev:
        return False, new_sig, "Footywire stats unchanged"
    return True, new_sig, "Footywire stats changed"


def run_script(script_name: str) -> tuple[bool, str]:
    """Run a Python script with the current interpreter. Returns (ok, last_line)."""
    script = BASE_DIR / script_name
    if not script.exists():
        msg = f"{script_name} not found"
        log.error(msg)
        return False, msg

    try:
        result = subprocess.run(
            [sys.executable, str(script)],
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_UTF8_ENV,
            timeout=SCRIPT_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        msg = f"{script_name} timed out after {SCRIPT_TIMEOUT_SEC // 60} min"
        log.error(msg)
        return False, msg
    except Exception as e:
        msg = f"{script_name} failed to start: {e}"
        log.error(msg)
        return False, msg

    tail = (result.stdout or "").strip().splitlines()
    last = tail[-1] if tail else ""
    if result.returncode != 0:
        err = (result.stderr or "").strip().splitlines()
        err_tail = err[-1] if err else f"exit {result.returncode}"
        log.error(f"{script_name} exit {result.returncode}: {err_tail}")
        return False, err_tail
    log.info(f"{script_name} ok: {last}")
    return True, last


def git_step(args: list[str]) -> tuple[bool, str]:
    """Run a single git command; return (ok, combined output tail)."""
    try:
        r = subprocess.run(
            ["git"] + args,
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
    except Exception as e:
        log.error(f"git {' '.join(args)} failed to start: {e}")
        return False, str(e)
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    tail = out.splitlines()[-1] if out else ""
    if r.returncode != 0:
        log.error(f"git {' '.join(args)} -> {r.returncode}: {tail}")
        return False, tail
    return True, tail


def _normalized(path: Path, list_key: str) -> str:
    """Return a stable JSON string of a data file's records with volatile fields
    (timestamps, ids) stripped, so timestamp-only churn isn't seen as a real
    change. Returns '' if the file can't be read or parsed."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        rows = data.get(list_key, data) if isinstance(data, dict) else data
        if not isinstance(rows, list):
            return ""
        cleaned = [
            {k: v for k, v in r.items() if k not in _VOLATILE_FIELDS}
            for r in rows if isinstance(r, dict)
        ]
        cleaned.sort(key=lambda r: json.dumps(r, sort_keys=True, ensure_ascii=False))
        return json.dumps(cleaned, sort_keys=True, ensure_ascii=False)
    except Exception:
        return ""


# Front-end / Worker code + PWA assets that should auto-deploy alongside data.
# An edit to any of these triggers a commit+push just like a data change.
CODE_FILES = ["index.html", "worker.js", "manifest.json", "sw.js", "push.js"]


def _code_signature() -> str:
    """Hash the raw bytes of the deployed code/asset files, so an edit to the
    site bundle or Worker (not just scraped data) also triggers a push."""
    h = hashlib.sha256()
    for name in CODE_FILES:
        try:
            h.update((BASE_DIR / name).read_bytes())
        except Exception:
            pass
        h.update(b"\x00")
    return h.hexdigest()


def _data_signature() -> str:
    """Hash the substantive content of players.json + news.json (timestamps and
    ids excluded) plus the deployed code files, so we push whenever either the
    scraped data OR the site/Worker code changes."""
    players = _normalized(BASE_DIR / "players.json", "players")
    news    = _normalized(BASE_DIR / "news.json", "news")
    buzz    = _normalized(BASE_DIR / "supercoach_tweets.json", "tweets")
    code    = _code_signature()
    return hashlib.sha256((players + "\x00" + news + "\x00" + buzz + "\x00" + code).encode("utf-8")).hexdigest()


def commit_and_push(timestamp: str, force: bool = False) -> tuple[bool, str]:
    """Commit and push ONLY if the scraped data actually changed.

    We compare a content signature (players.json + news.json with volatile
    timestamp/id fields stripped) against the previous run's signature. If they
    match, the data is unchanged — we skip the commit and push entirely so we
    don't trigger an unnecessary Cloudflare build. The signature is persisted to
    .scrape_sig and only updated after a successful push.

    When ``force`` is set, we push regardless of the signature — creating an
    empty commit if there is no real diff — so a long unchanged streak still
    refreshes the deployment.
    """
    sig = _data_signature()
    try:
        prev = SIG_PATH.read_text(encoding="utf-8").strip()
    except Exception:
        prev = ""

    if sig and sig == prev and not force:
        log.info("No changes — skipping push")
        return True, "no changes"

    add_list = ["players.json", "news.json", "news_history.json", "supercoach_tweets.json"]
    add_list += [f for f in CODE_FILES if (BASE_DIR / f).exists()]
    ok, _ = git_step(["add"] + add_list)
    if not ok:
        return False, "git add failed"

    commit_args = ["commit", "-m", f"auto update {timestamp}"]
    if force:
        # Allow an empty commit so a no-change streak still triggers a push.
        commit_args = ["commit", "--allow-empty", "-m", f"auto refresh {timestamp}"]
    ok, tail = git_step(commit_args)
    if not ok:
        # No staged diff (e.g. only volatile fields moved) -> nothing to push.
        if "nothing to commit" in tail.lower() or "no changes added" in tail.lower():
            try:
                SIG_PATH.write_text(sig, encoding="utf-8")
            except Exception:
                pass
            log.info("No changes — skipping push")
            return True, "no changes"
        return False, f"commit failed: {tail}"

    ok, tail = git_step(["push"])
    if not ok:
        return False, f"push failed: {tail}"

    try:
        SIG_PATH.write_text(sig, encoding="utf-8")
    except Exception:
        pass
    return True, "pushed"


def _count_players() -> int:
    """Read players.json and return the player count, or 0 if anything goes wrong."""
    try:
        import json
        data = json.loads((BASE_DIR / "players.json").read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return len(data.get("players") or [])
        if isinstance(data, list):
            return len(data)
    except Exception:
        pass
    return 0


def _count_news() -> int:
    try:
        import json
        data = json.loads((BASE_DIR / "news.json").read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return int(data.get("item_count") or len(data.get("news") or []))
        if isinstance(data, list):
            return len(data)
    except Exception:
        pass
    return 0


def run_once() -> None:
    started = datetime.now()
    timestamp = started.strftime("%Y-%m-%d %H:%M:%S")
    log.info(f"=== Auto-scrape run @ {timestamp} ===")

    # Only re-fetch player stats when Footywire's SC stats page has actually
    # changed (i.e. a game has finished and scores are processed). Each run
    # of fetch_data.py hammers ~350 games-log pages over ~12 minutes; the
    # ~95% of cycles where nothing has changed get skipped here.
    should_fetch, fetch_sig, fetch_reason = _fetch_data_check()
    if should_fetch:
        _status("Scraping... fetch_data.py")
        fetch_ok, fetch_msg = run_script("fetch_data.py")
        if fetch_ok and fetch_sig:
            try:
                FETCH_DATA_SIG_PATH.write_text(fetch_sig, encoding="utf-8")
            except Exception as _e:
                log.warning(f"Could not save fetch_data signature: {_e}")
    else:
        log.info(f"fetch_data skipped: {fetch_reason}")
        fetch_ok, fetch_msg = True, f"skipped — {fetch_reason}"

    _status("Scraping... news_scraper.py")
    news_ok, news_msg = run_script("news_scraper.py")

    n_players = _count_players()
    n_news    = _count_news()

    bits = [f"{n_players} players", f"{n_news} news items"]
    if not fetch_ok:
        bits.append(f"fetch FAILED ({fetch_msg})")
    if not news_ok:
        bits.append(f"news FAILED ({news_msg})")

    if fetch_ok or news_ok:
        global _no_change_streak
        force = _no_change_streak >= MAX_NO_CHANGE_RUNS
        push_ok, push_msg = commit_and_push(timestamp, force=force)
        if push_msg == "no changes":
            _no_change_streak += 1
        else:
            # A real push (or a forced refresh) resets the streak.
            _no_change_streak = 0
        if push_msg == "pushed" and force:
            log.info(f"Forced refresh push after {MAX_NO_CHANGE_RUNS} no-change runs")
        bits.append("Pushed (forced refresh)." if (push_msg == "pushed" and force) else
                    "Pushed." if push_msg == "pushed" else
                    "No changes." if push_msg == "no changes" else
                    f"Push FAILED ({push_msg})")
    else:
        bits.append("Skipped push (both scrapers failed).")

    # Web Push — notify subscribers of fresh news matching their watchlist.
    # No-ops if there are no subs / no fresh matches, so safe every cycle.
    try:
        nf = subprocess.run([sys.executable, str(BASE_DIR / "notify.py")],
                            cwd=str(BASE_DIR), capture_output=True, text=True,
                            encoding="utf-8", errors="replace", env=_UTF8_ENV, timeout=120)
        nt = (nf.stdout or "").strip().splitlines()
        if nt:
            log.info("notify: " + nt[-1])
    except Exception as e:
        log.warning(f"notify failed: {e}")

    # Claude quality agent — runs every AGENT_RUN_EVERY cycles (~30 min).
    # No-ops when ANTHROPIC_API_KEY is missing. Applies safe fixes to news.json
    # only; never edits source code. Writes proposed_upstream_fixes.json for
    # human review when applicable.
    global _agent_run_counter
    _agent_run_counter += 1
    if _agent_run_counter >= AGENT_RUN_EVERY:
        _agent_run_counter = 0
        try:
            ar = subprocess.run([sys.executable, str(BASE_DIR / "agent_monitor.py")],
                                cwd=str(BASE_DIR), capture_output=True, text=True,
                                encoding="utf-8", errors="replace",
                                env=_UTF8_ENV, timeout=180)
            at = (ar.stdout or "").strip().splitlines()
            for ln in at:
                if ln.strip():
                    log.info("agent: " + ln.strip())
        except Exception as e:
            log.warning(f"agent monitor failed: {e}")

    # SuperCoach live feed — pulls at most once per AM/arvo/PM window (3x/day,
    # AEST); no-ops otherwise, so it's safe to call every cycle.
    try:
        fr = subprocess.run([sys.executable, str(BASE_DIR / "supercoach_feed.py"), "--auto"],
                            cwd=str(BASE_DIR), capture_output=True, text=True,
                            encoding="utf-8", errors="replace", env=_UTF8_ENV, timeout=60)
        ft = (fr.stdout or "").strip().splitlines()
        if ft:
            log.info("supercoach_feed: " + ft[-1])
    except Exception as e:
        log.warning(f"supercoach_feed failed: {e}")

    # Scheduled tweet — self-throttled to 5/day, spaced, 6am-11pm AEST. No-ops
    # outside the window / once today's quota is met, so it's safe to call here
    # every cycle.
    try:
        tw = subprocess.run(
            [sys.executable, str(BASE_DIR / "tweet_bot.py"), "--auto"],
            cwd=str(BASE_DIR), capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=_UTF8_ENV, timeout=120,
        )
        tail = (tw.stdout or "").strip().splitlines()
        if tail:
            log.info("tweet_bot: " + tail[-1])
            if any("[ok] posted" in ln for ln in tail):
                bits.append("Tweeted.")
    except Exception as e:
        log.warning(f"tweet_bot --auto failed: {e}")

    _status(f"Scraping... done. {' '.join(bits)} Next run in {INTERVAL_SEC // 60} min.")
    _endline()
    log.info("Run complete — " + " | ".join(bits))


def main() -> None:
    print(f"AFLFantasyWire auto-scraper — interval {INTERVAL_SEC // 60} min, "
          f"log at {LOG_PATH}")
    while True:
        try:
            run_once()
        except KeyboardInterrupt:
            print("\nStopped by user.")
            return
        except Exception as e:
            # Catch-all so a single bad run never kills the loop.
            log.exception(f"Unhandled error in run_once: {e}")
            _status(f"Run errored: {e}. Continuing in 5 min.")
            _endline()
        time.sleep(INTERVAL_SEC)


if __name__ == "__main__":
    main()
