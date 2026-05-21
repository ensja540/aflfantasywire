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
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BASE_DIR     = Path(__file__).resolve().parent
LOG_PATH     = BASE_DIR / "scrape.log"
SIG_PATH     = BASE_DIR / ".scrape_sig"
INTERVAL_SEC = 5 * 60   # 5 minutes

# Fields that change every run regardless of real data (timestamps, ids,
# recomputed relevance). Excluded from the change signature so timestamp-only
# churn (e.g. "1h ago" -> "2h ago") doesn't look like a real update.
_VOLATILE_FIELDS = {"id", "time", "timeLabel", "scrapedAt", "relevance", "urgent", "status"}


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
            timeout=10 * 60,
        )
    except subprocess.TimeoutExpired:
        msg = f"{script_name} timed out after 10 min"
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


def _data_signature() -> str:
    """Hash the substantive content of players.json + news.json (timestamps and
    ids excluded) so we can tell whether the data actually changed."""
    players = _normalized(BASE_DIR / "players.json", "players")
    news    = _normalized(BASE_DIR / "news.json", "news")
    return hashlib.sha256((players + "\x00" + news).encode("utf-8")).hexdigest()


def commit_and_push(timestamp: str) -> tuple[bool, str]:
    """Commit and push ONLY if the scraped data actually changed.

    We compare a content signature (players.json + news.json with volatile
    timestamp/id fields stripped) against the previous run's signature. If they
    match, the data is unchanged — we skip the commit and push entirely so we
    don't trigger an unnecessary Cloudflare build. The signature is persisted to
    .scrape_sig and only updated after a successful push.
    """
    sig = _data_signature()
    try:
        prev = SIG_PATH.read_text(encoding="utf-8").strip()
    except Exception:
        prev = ""

    if sig and sig == prev:
        log.info("No changes — skipping push")
        return True, "no changes"

    ok, _ = git_step(["add", "players.json", "news.json", "news_history.json"])
    if not ok:
        return False, "git add failed"

    ok, tail = git_step(["commit", "-m", f"auto update {timestamp}"])
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

    _status("Scraping... fetch_data.py")
    fetch_ok, fetch_msg = run_script("fetch_data.py")

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
        push_ok, push_msg = commit_and_push(timestamp)
        bits.append("Pushed." if push_msg == "pushed" else
                    "No changes." if push_msg == "no changes" else
                    f"Push FAILED ({push_msg})")
    else:
        bits.append("Skipped push (both scrapers failed).")

    _status(f"Scraping... done. {' '.join(bits)} Next run in 5 min.")
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
