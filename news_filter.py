#!/usr/bin/env python3
"""News filter for AFLFantasyWire.

Provides simple classification and relevance scoring for news text.
"""

import re

# Keywords are matched with letter-boundaries (see _kw_match), so bare ambiguous
# words like "out" or "test" are deliberately avoided — they previously matched
# "outstanding", "Darwin test", etc. and mislabelled general news as injuries.
KEYWORDS = {
    "injury_out": [
        "ruled out", "will miss", "to miss", "set to miss", "won't play", "wont play",
        "sidelined", "season-ending", "season ending", "out for the season",
        "out indefinitely", "miss the rest", "ruptured", "requires surgery",
        "undergo surgery", "facing surgery", "done for the season", "torn",
        "not available", "unavailable", "concussion protocol", "concussion test",
        "miss a minimum", "weeks on the sideline", "weeks on the sidelines",
        "ACL", "ruptured acl", "broken", "fracture", "fractured",
    ],
    "injury_tbc": [
        "fitness test", "in doubt", "injury cloud", "cloud over", "race against time",
        "managed", "questionable", "doubtful", "tbc", "game-time decision",
        "game time decision", "carrying an injury", "under an injury cloud",
        "concussion", "injured", "strain", "strained", "sprain", "sprained",
        "soreness", "complaint", "knock", "tightness", "corked",
    ],
    "dropped": ["omitted", "dropped", "left out", "not named", "axed", "demoted", "makes way"],
    "named": [
        "teams:", "ins and outs", "ins & outs", "team news", "named side", "named to play",
        "set to return", "returns from injury", "cleared to play", "recalled",
        "handed a recall", "back in the side", "team selection", "lineup", "line-up",
    ],
    "role_change": [
        "role change", "midfield role", "tagging role", "new role",
        "positional switch", "moved into the midfield",
    ],
    "vest_risk": ["medical substitute", "sub vest", "named as the substitute", "21st man", "late withdrawal"],
    "price": ["price rise", "price drop", "breakeven", "break-even", "cash cow"],
    "team_news": [
        "named", "recalled", "omitted", "dropped", "replaced", "in for", "out for",
        "selection", "emergencies", "late out", "substitute", "ins and outs", "ins & outs",
    ],
}

IGNORE_PHRASES = [
    "press conference", "match report", "match preview"  # genuinely low fantasy value
]

CATEGORY_PRIORITY = [
    "injury_out",
    "injury_tbc",
    "dropped",
    "vest_risk",
    "named",
    "role_change",
    "team_news",
    "price",
]

def _normalize(text):
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _kw_match(word, text):
    """Letter-boundary match so "out" does not match "outstanding" and "test"
    does not match "Darwin test". Only letters count as word characters, so
    phrases ending in punctuation (e.g. "teams:") still match."""
    return re.search(r"(?<![a-z])" + re.escape(word) + r"(?![a-z])", text) is not None


def classify_item(text, headline=None):
    """Classify a piece of news text and return relevance metadata."""
    text = _normalize(text)
    headline = _normalize(headline or "")
    combined = f"{headline} {text}".strip()

    result = {
        "relevant": False,
        "score": 0,
        "category": "news",
        "matches": [],
    }

    if not combined:
        return result

    if any(phrase in combined for phrase in IGNORE_PHRASES):
        result["score"] = 5
        return result

    # Base relevance scoring by keyword hits (letter-boundary matched)
    for category, words in KEYWORDS.items():
        for word in words:
            if _kw_match(word, combined):
                result["matches"].append((category, word))
                result["score"] += 15

    # Body-part injuries: "knee injury", "injured his shoulder", "hamstring strain"...
    _bp = ("knee|ankle|hamstring|shoulder|groin|calf|quad|thigh|foot|hip|back|"
           "wrist|hand|finger|collarbone|achilles|rib|ribs|jaw|toe|elbow|pec|hip|head")
    if (re.search(r"(" + _bp + r")\s+(injury|strain|sprain|complaint|soreness|problem|issue)", combined)
            or re.search(r"injur(?:ed|y)\b[^.]{0,25}(" + _bp + r")", combined)):
        if any(_kw_match(t, combined) for t in ["weeks", "months", "miss", "out for", "ruled out", "not available", "unavailable", "season"]):
            result["matches"].append(("injury_out", "bodypart"))
        else:
            result["matches"].append(("injury_tbc", "bodypart"))
        result["score"] += 25

    # General club news worth surfacing: VFL call-ups, form, trades, speculation.
    if any(_kw_match(t, combined) for t in [
            "vfl", "called up", "call-up", "promoted", "debut", "best on ground",
            "big performance", "trade", "traded", "speculation", "speculated",
            "signing", "re-sign", "resign", "contract", "free agent", "linked with",
            "set to join", "request", "delisted", "retire", "retirement"]):
        result["score"] += 30

    # Encourage news items with clear injury/selection signals
    if any(category in ["injury_out", "injury_tbc", "dropped", "named", "role_change", "vest_risk", "team_news"]
           for category, _ in result["matches"]):
        result["score"] += 20

    # Team selection: reward actual changes, demote "unchanged"/full-team announcements.
    if any(_kw_match(t, combined) for t in ["named", "omitted", "dropped", "recalled", "late out"]):
        result["score"] += 10
    if any(t in combined for t in ["unchanged", "no changes", "full team", "no change to"]):
        result["score"] -= 10

    # Minor boost for common fantasy terms
    if any(_kw_match(term, combined) for term in ["supercoach", "fantasy", "breakeven", "price", "injury", "omitted", "named"]):
        result["score"] += 10

    # Demote generic news with no player or injury keywords
    if any(_kw_match(term, combined) for term in ["injury", "named", "omitted", "selected", "recalled"]):
        result["score"] += 5

    # Determine category by highest-priority match
    for category in CATEGORY_PRIORITY:
        if any(match_category == category for match_category, _ in result["matches"]):
            result["category"] = category
            break

    if result["score"] >= 30:
        result["relevant"] = True
    else:
        # Some headlines are still relevant if they refer to strong fantasy events.
        if any(_kw_match(term, combined) for term in ["injury", "omitted", "named", "selected", "tbc", "doubtful"]):
            result["relevant"] = True
            result["score"] = max(result["score"], 25)

    return result


def is_relevant(result):
    """Return True when a classify_item result indicates relevant news."""
    if isinstance(result, dict):
        return bool(result.get("relevant"))
    return False
