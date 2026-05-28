#!/usr/bin/env python3
"""
AFLFantasyWire — Agent Quality Monitor
======================================

Runs Claude API quality checks on news.json and proposes upstream fixes for
the scraper.

Pass 1 (active fixes)
---------------------
Inspects a sample of news items. Applies CONSERVATIVE fixes:
  * Fills in missing player / team tags.
  * Corrects type / category when the agent disagrees AND confidence ≥ 70.
  * Removes items the agent flags as unfixable junk (structured-source items
    are protected from removal).
Does NOT touch the status / status_label fields — those are owned by
NewsHistory and would otherwise regress to "🔴 New" every run.
Does NOT auto-commit — the next auto_scrape cycle commits news.json
naturally if a real change was made.

Pass 2 (advisory only)
----------------------
Looks at the patterns in Pass 1's issues and asks Sonnet for patches against
news_scraper.py / news_filter.py. Patches are written to
proposed_upstream_fixes.json for human review. Never applied, never
committed, never pushed.

Costs are kept down with prompt caching: the player roster + team list
(~10KB, stable across runs) lives in a cached system block.

Usage:
    python agent_monitor.py
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import anthropic

BASE_DIR        = Path(__file__).resolve().parent
LOG_PATH        = BASE_DIR / "agent.log"
REPORT_PATH     = BASE_DIR / "agent_report.json"
PROPOSALS_PATH  = BASE_DIR / "proposed_upstream_fixes.json"
NEWS_PATH       = BASE_DIR / "news.json"
PLAYERS_PATH    = BASE_DIR / "players.json"

# Model picks (latest in May 2026):
#   Haiku 4.5 — fast + cheap for the per-item QA scan, runs every 30 min.
#   Sonnet 4.6 — better reasoning for proposing code patches; only runs when
#                the QA pass already found ≥3 issues, so callsites are rare.
QC_MODEL       = "claude-haiku-4-5-20251001"
PROPOSAL_MODEL = "claude-sonnet-4-6"

# Number of items to send through the QA pass each run. The model gets a
# representative sample, not the whole feed, to keep latency + cost bounded.
SAMPLE_SIZE = 25

# Apply a Pass-1 fix only if the agent reported this much confidence.
MIN_FIX_CONFIDENCE = 70

logging.basicConfig(
    filename=str(LOG_PATH),
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("agent_monitor")


# ── env / data loading ──────────────────────────────────────────────────────

def _load_env():
    """Minimal .env reader — same shape as notify.py so we don't add another
    dependency."""
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_reference_data():
    pd = json.loads(PLAYERS_PATH.read_text(encoding="utf-8"))
    players = pd.get("players", pd) if isinstance(pd, dict) else pd
    player_names = sorted({p["name"] for p in players if p.get("name")})
    teams        = sorted({p["team"] for p in players if p.get("team")})
    return player_names, teams


def load_news():
    data = json.loads(NEWS_PATH.read_text(encoding="utf-8"))
    return data.get("news", data) if isinstance(data, dict) else data


def _client():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log.error("ANTHROPIC_API_KEY not set — agent cannot run.")
        return None
    return anthropic.Anthropic(api_key=api_key)


def _strip_json_fences(text):
    """Tolerate the occasional ```json fenced reply."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    return text.strip()


# ── Pass 1: quality check ───────────────────────────────────────────────────

def run_quality_check(client, news_sample, player_names, teams):
    """Return the QA report dict (see prompt for shape)."""
    sample = news_sample[:SAMPLE_SIZE]

    # Reference data goes in a cacheable system block so 30-min reruns only
    # pay for the changing news items, not the 10KB roster each time.
    system_block = [
        {
            "type": "text",
            "text": (
                "You are a quality control agent for an AFL fantasy news feed.\n\n"
                "Known AFL players (canonical roster):\n"
                + "\n".join(player_names)
                + "\n\nKnown AFL teams:\n"
                + "\n".join(teams) + "\n"
            ),
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": (
                "For each news item check:\n"
                "1. PLAYER TAG: does item.player match a real player from the roster above? "
                "Is it BLANK when a player IS named in the headline/body?\n"
                "2. TEAM TAG: does item.team match a real AFL team? Blank when one IS named?\n"
                "3. CONTENT QUALITY: is item.body a real description, or a generic teaser "
                "like 'Monitor team news before lockout' with no actual information?\n"
                "4. CATEGORY: are item.type + item.category accurate given the headline+body? "
                "Valid types: injury, selection, news, rumour, analysis. "
                "Valid categories: injury_out, injury_tbc, team_news, general, price.\n"
                "\n"
                "RULES:\n"
                "- Do NOT flag an item just because its player isn't in the roster — some "
                "manual extras (Will Day, Marcus Herbert) are intentionally outside Footywire's "
                "active list.\n"
                "- Do NOT touch status / status_label / first_seen — those are owned by a "
                "separate history tracker.\n"
                "- Only mark `remove:true` for items that are pure junk with no salvageable "
                "tag (NOT for items that just need a category fix).\n"
                "- Set per-issue confidence honestly (0-100). Low-confidence issues will "
                "be ignored by the fix step.\n"
                "\n"
                "Output ONLY valid JSON in this exact shape (no preamble, no markdown):\n"
                "{\n"
                '  "health_score": 0-100,\n'
                '  "total_checked": N,\n'
                '  "issues": [\n'
                '    {\n'
                '      "headline": "exact headline of the item",\n'
                '      "problems": ["list of short problem descriptions"],\n'
                '      "fixes": {\n'
                '        "player": "correct player name or null",\n'
                '        "team": "correct team name or null",\n'
                '        "type": "correct type or null",\n'
                '        "category": "correct category or null",\n'
                '        "remove": true if unfixable junk, else null\n'
                "      },\n"
                '      "confidence": 0-100\n'
                "    }\n"
                "  ],\n"
                '  "untagged_count": N,\n'
                '  "wrong_team_count": N,\n'
                '  "wrong_player_count": N,\n'
                '  "generic_body_count": N,\n'
                '  "summary": "one sentence describing feed quality"\n'
                "}\n"
            ),
        },
    ]

    user_msg = (
        f"Quality-check these {len(sample)} news items:\n\n"
        + json.dumps(sample, ensure_ascii=False, indent=2)[:14000]
    )

    response = client.messages.create(
        model=QC_MODEL,
        max_tokens=4000,
        system=system_block,
        messages=[{"role": "user", "content": user_msg}],
    )
    text = _strip_json_fences(response.content[0].text)
    return json.loads(text)


def apply_safe_fixes(news, report):
    """Conservative Pass-1 fixes. Returns (new_news, n_field_fixes, n_removed)."""
    if not report or not report.get("issues"):
        return news, 0, 0

    issue_lookup = {(i.get("headline") or "").strip(): i for i in report["issues"]}

    valid_types = {"injury", "selection", "news", "rumour", "analysis"}
    valid_cats  = {"injury_out", "injury_tbc", "team_news", "general", "price"}
    # Items from these structured sources are sacrosanct — the agent must not
    # remove them. They are the ground truth for injuries / team selections.
    PROTECTED_SOURCES = {
        "afl_injury_page", "afl_medical_room",
        "footywire_injuries", "team_announcements",
        "afl_team_selections", "footywire_selections",
    }

    n_field_fixes = 0
    n_removed = 0
    out = []
    for item in news:
        headline = (item.get("headline") or "").strip()
        issue = issue_lookup.get(headline)
        if not issue:
            out.append(item)
            continue
        conf = int(issue.get("confidence") or 0)
        if conf < MIN_FIX_CONFIDENCE:
            out.append(item)
            continue
        fixes = issue.get("fixes") or {}

        # Removal — only when explicitly flagged AND item isn't from a trusted source.
        if fixes.get("remove") and item.get("_source") not in PROTECTED_SOURCES:
            n_removed += 1
            continue

        # Tag fills — only fill blanks, never overwrite a value the scraper set.
        if fixes.get("player") and not item.get("player"):
            item["player"] = fixes["player"]
            n_field_fixes += 1
        if fixes.get("team") and not item.get("team"):
            item["team"] = fixes["team"]
            n_field_fixes += 1

        # Category corrections — only when the issue mentions category/type, and
        # the new value is in the valid set AND differs from the current.
        prob_text = " ".join(issue.get("problems") or []).lower()
        if "category" in prob_text or "type" in prob_text:
            new_t = fixes.get("type")
            new_c = fixes.get("category")
            if new_t in valid_types and new_t != item.get("type"):
                item["type"] = new_t
                n_field_fixes += 1
            if new_c in valid_cats and new_c != item.get("category"):
                item["category"] = new_c
                # Keep urgent aligned with category, mirroring the scraper.
                item["urgent"] = (item.get("type") == "injury" and new_c == "injury_out")
                n_field_fixes += 1

        out.append(item)
    return out, n_field_fixes, n_removed


def save_news(news):
    existing = json.loads(NEWS_PATH.read_text(encoding="utf-8"))
    if isinstance(existing, dict):
        existing["news"] = news
        if "item_count" in existing:
            existing["item_count"] = len(news)
        existing["last_agent_check"] = datetime.now(timezone.utc).isoformat()
        NEWS_PATH.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    else:
        NEWS_PATH.write_text(
            json.dumps(news, ensure_ascii=False, indent=2), encoding="utf-8"
        )


# ── Pass 2: upstream-fix PROPOSALS (no application) ─────────────────────────

def _function_excerpt(code, fname):
    idx = code.find(f"def {fname}(")
    if idx < 0:
        return ""
    end = code.find("\ndef ", idx + 1)
    return code[idx: end if end > 0 else idx + 6000]


def propose_upstream_fixes(client, report, news_sample):
    """Return a proposal dict (or None). NEVER edits source files."""
    issues = report.get("issues") or []
    if len(issues) < 3:
        log.info("Pass 2 skipped: <3 issues — nothing to generalise from.")
        return None

    scraper_code = (BASE_DIR / "news_scraper.py").read_text(encoding="utf-8")
    filter_code  = (BASE_DIR / "news_filter.py").read_text(encoding="utf-8")

    # Send only the functions plausibly responsible for the patterns we see.
    relevant_scraper = "\n\n".join(filter(None, [
        _function_excerpt(scraper_code, "_classify_headline"),
        _function_excerpt(scraper_code, "reclassify_item"),
        _function_excerpt(scraper_code, "enforce_category"),
        _function_excerpt(scraper_code, "extract_player_mentions"),
        _function_excerpt(scraper_code, "extract_players_from_url"),
    ]))

    # Cacheable system block — code rarely changes between 30-min runs.
    system_block = [{
        "type": "text",
        "text": (
            "You are a senior Python developer reviewing the AFLFantasyWire "
            "news scraper. Below are the relevant functions of news_scraper.py "
            "and the full news_filter.py module.\n\n"
            "----- news_filter.py (full) -----\n" + filter_code + "\n\n"
            "----- news_scraper.py (relevant functions) -----\n" + relevant_scraper + "\n"
        ),
        "cache_control": {"type": "ephemeral"},
    }]

    user_msg = (
        f"The QA agent flagged these issues (sample of {len(issues[:8])}):\n\n"
        f"{json.dumps(issues[:8], indent=2, ensure_ascii=False)[:4000]}\n\n"
        f"Representative news items those issues came from:\n"
        f"{json.dumps(news_sample[:5], indent=2, ensure_ascii=False)[:3000]}\n\n"
        "Propose root-cause patches. Be conservative — at most 5 patches, "
        "only for clearly recurring patterns. `old_code` must match the source "
        "above VERBATIM (any whitespace difference will make the human-applied "
        "patch fail).\n\n"
        "Return ONLY this JSON (no markdown):\n"
        "{\n"
        '  "should_fix": bool,\n'
        '  "confidence": 0-100,\n'
        '  "fixes": [\n'
        '    {\n'
        '      "file": "news_filter.py" | "news_scraper.py",\n'
        '      "function": "function name",\n'
        '      "problem": "what is wrong",\n'
        '      "old_code": "exact lines to replace (verbatim)",\n'
        '      "new_code": "replacement",\n'
        '      "reason": "why this is the root-cause fix"\n'
        "    }\n"
        "  ],\n"
        '  "summary": "one sentence"\n'
        "}"
    )

    response = client.messages.create(
        model=PROPOSAL_MODEL,
        max_tokens=4000,
        system=system_block,
        messages=[{"role": "user", "content": user_msg}],
    )
    text = _strip_json_fences(response.content[0].text)
    try:
        proposal = json.loads(text)
    except json.JSONDecodeError as e:
        log.warning(f"Pass 2 returned non-JSON: {e}")
        return None
    proposal["timestamp"] = datetime.now(timezone.utc).isoformat()
    proposal["status"] = "FOR_REVIEW"
    proposal["note"] = (
        "These patches are SUGGESTIONS ONLY. Nothing has been applied to your "
        "source files or git. Apply manually after reviewing each `old_code` / "
        "`new_code` pair."
    )
    return proposal


def save_proposals(proposal):
    if proposal is None:
        return
    PROPOSALS_PATH.write_text(
        json.dumps(proposal, indent=2, ensure_ascii=False), encoding="utf-8"
    )


# ── main ────────────────────────────────────────────────────────────────────

def main():
    _load_env()
    log.info("=== Agent monitor run ===")
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Agent monitor starting…")

    client = _client()
    if client is None:
        print("  ANTHROPIC_API_KEY is not set in environment/.env — exiting.")
        return

    try:
        player_names, teams = load_reference_data()
        news = load_news()
    except Exception as e:
        log.exception(f"Failed to load reference data: {e}")
        print(f"  Failed to load data: {e}")
        return

    log.info(f"Checking {min(len(news), SAMPLE_SIZE)} of {len(news)} items")

    # ── Pass 1 ──────────────────────────────────────────────────────────────
    try:
        report = run_quality_check(client, news, player_names, teams)
    except Exception as e:
        log.exception(f"Pass 1 (quality check) failed: {e}")
        print(f"  Pass 1 failed: {e}")
        return

    health = int(report.get("health_score") or 0)
    issues = report.get("issues") or []
    print(f"  Health score: {health}/100")
    print(f"  Issues: {len(issues)} "
          f"(untagged: {report.get('untagged_count', 0)}, "
          f"wrong team: {report.get('wrong_team_count', 0)}, "
          f"wrong player: {report.get('wrong_player_count', 0)}, "
          f"generic body: {report.get('generic_body_count', 0)})")
    print(f"  Summary: {report.get('summary', '')}")

    if issues:
        fixed_news, n_field, n_remove = apply_safe_fixes(news, report)
        if n_field or n_remove:
            save_news(fixed_news)
            print(f"  Applied {n_field} field-fix(es); removed {n_remove} item(s).")
            log.info(f"Pass 1 fixes: {n_field} field-fixes, {n_remove} removals")

    # ── Pass 2 (proposals only) ─────────────────────────────────────────────
    if health < 80 and len(issues) >= 3:
        print("  Pass 2: drafting upstream-fix proposals…")
        try:
            proposal = propose_upstream_fixes(client, report, news[:10])
        except Exception as e:
            log.exception(f"Pass 2 (proposals) failed: {e}")
            print(f"  Pass 2 failed: {e}")
            proposal = None
        if proposal:
            save_proposals(proposal)
            n_fix = len(proposal.get("fixes") or [])
            print(f"  Wrote {n_fix} proposed patch(es) to {PROPOSALS_PATH.name} "
                  f"(confidence: {proposal.get('confidence', 0)}). "
                  f"Review and apply manually.")
            log.info(f"Pass 2 wrote {n_fix} proposals (confidence={proposal.get('confidence')})")
        else:
            print("  Pass 2: no proposals generated.")

    report["timestamp"] = datetime.now(timezone.utc).isoformat()
    REPORT_PATH.write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    log.info(f"Health: {health}/100. Run complete.")


if __name__ == "__main__":
    main()
