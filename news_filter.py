#!/usr/bin/env python3
"""News filter for AFLFantasyWire.

Provides simple classification and relevance scoring for news text.
"""

import re

KEYWORDS = {
    "injury_out": ["out", "ruled out", "will miss", "sidelined", "season-ending", "season ending"],
    "injury_tbc": ["tbc", "questionable", "doubtful", "managed", "uncertain", "unclear", "test", "monitor", "possible"],
    "dropped": ["omitted", "dropped", "left out", "not named", "replaced by", "replaced"],
    "named": ["named", "selected", "unchanged", "in for", "returns", "returns to", "set to play"],
    "role_change": ["role change", "role change", "move to", "shift to", "tagged", "forward pocket", "half forward", "centre bounce", "midfield role", "forward","ruck"],
    "vest_risk": ["vest", "substitute", "sub", "emergency", "emg"],
    "price": ["price", "breakeven", "breakeven", "price rise", "price drop", "price delta"],
}

IGNORE_PHRASES = [
    "trade news", "coach says", "press conference", "match report", "preview", "gossip", "rumour"  # allow some rumour if relevant
]

CATEGORY_PRIORITY = [
    "injury_out",
    "injury_tbc",
    "dropped",
    "vest_risk",
    "named",
    "role_change",
    "price",
]

def _normalize(text):
    return re.sub(r"\s+", " ", (text or "").strip().lower())


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

    # Base relevance scoring by keyword hits
    for category, words in KEYWORDS.items():
        for word in words:
            if word in combined:
                result["matches"].append((category, word))
                result["score"] += 15

    # Encourage news items with clear injury/selection signals
    if any(category in ["injury_out", "injury_tbc", "dropped", "named", "role_change", "vest_risk"]
           for category, _ in result["matches"]):
        result["score"] += 20

    # Minor boost for common fantasy terms
    if any(term in combined for term in ["supercoach", "fantasy", "breakeven", "price", "injury", "omitted", "named"]):
        result["score"] += 10

    # Demote generic news with no player or injury keywords
    if "injury" in combined or "out" in combined or "named" in combined or "omitted" in combined or "selected" in combined:
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
        if any(term in combined for term in ["injury", "omitted", "named", "selected", "vest", "tbc", "doubtful"]):
            result["relevant"] = True
            result["score"] = max(result["score"], 25)

    return result


def is_relevant(result):
    """Return True when a classify_item result indicates relevant news."""
    if isinstance(result, dict):
        return bool(result.get("relevant"))
    return False
