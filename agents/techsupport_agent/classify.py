"""Heuristic ticket classifier: type (question/bug/feature-request) + severity."""

from __future__ import annotations

import re
from typing import Iterable, Optional, Sequence

from agents.techsupport_agent.models import (
    SEVERITY_HIGH,
    SEVERITY_LOW,
    SEVERITY_MEDIUM,
    TYPE_BUG,
    TYPE_FEATURE,
    TYPE_QUESTION,
    Classification,
    IncomingItem,
    Ticket,
)

BUG_TERMS = (
    "traceback",
    "stack trace",
    "exception",
    "typeerror",
    "runtimeerror",
    "crash",
    "segfault",
    "bug",
    "regression",
    "broken",
    "fails",
    "failed",
    "error",
    "exception:",
)
FEATURE_TERMS = (
    "feature request",
    "feature-request",
    "would be nice",
    "please add",
    "enhancement",
    "wishlist",
    "support for",
    "can we add",
    "it would help if",
)
QUESTION_TERMS = (
    "how do i",
    "how to",
    "what is",
    "where is",
    "can i",
    "how can",
    "docs for",
    "help with",
    "?",
)
HIGH_SEV = (
    "crash",
    "data loss",
    "security",
    "outage",
    "production down",
    "cannot start",
    "segfault",
    "rce",
)
MED_SEV = (
    "error",
    "fails",
    "failed",
    "broken",
    "regression",
    "incorrect",
    "traceback",
    "exception",
)
LABEL_TYPE = {
    "bug": TYPE_BUG,
    "bug report": TYPE_BUG,
    "defect": TYPE_BUG,
    "enhancement": TYPE_FEATURE,
    "feature": TYPE_FEATURE,
    "feature request": TYPE_FEATURE,
    "question": TYPE_QUESTION,
    "help wanted": TYPE_QUESTION,
    "docs": TYPE_QUESTION,
}


def classify_text(
    title: str,
    body: str = "",
    *,
    labels: Sequence[str] = (),
) -> Classification:
    blob = f"{title}\n{body}".strip()
    lowered = blob.lower()
    label_type = _type_from_labels(labels)
    bug_hits = _count_hits(lowered, BUG_TERMS)
    feature_hits = _count_hits(lowered, FEATURE_TERMS)
    question_hits = _count_hits(lowered, QUESTION_TERMS)

    scores = {
        TYPE_BUG: bug_hits,
        TYPE_FEATURE: feature_hits,
        TYPE_QUESTION: question_hits,
    }
    if label_type:
        scores[label_type] += 4

    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    winner, top = ranked[0]
    second = ranked[1][1]
    if top <= 0:
        ticket_type = TYPE_QUESTION
        confidence = 0.35
        rationale = "no type keywords; defaulted to question"
    elif top == second:
        ticket_type = winner
        confidence = 0.45
        rationale = f"ambiguous type scores {dict(scores)}"
    else:
        ticket_type = winner
        margin = top - second
        if top >= 3 or (label_type and top >= 4):
            confidence = 0.9
        elif margin >= 2 or top >= 2:
            confidence = 0.8
        else:
            confidence = 0.65
        rationale = f"{ticket_type} score={top} (bug={bug_hits} feature={feature_hits} question={question_hits})"
        if label_type:
            rationale += f"; github label={label_type}"

    severity, sev_rationale = _severity(lowered, ticket_type)
    return Classification(
        ticket_type=ticket_type,
        severity=severity,
        confidence=min(0.99, confidence),
        rationale=f"{rationale}; {sev_rationale}",
    )


def classify_ticket(ticket: Ticket, *, labels: Sequence[str] = ()) -> Classification:
    return classify_text(ticket.title, ticket.body, labels=labels)


def classify_incoming(item: IncomingItem) -> Classification:
    return classify_text(item.title, item.body, labels=item.labels)


def _type_from_labels(labels: Iterable[str]) -> Optional[str]:
    for raw in labels:
        key = (raw or "").strip().lower()
        if key in LABEL_TYPE:
            return LABEL_TYPE[key]
    return None


def _count_hits(text: str, terms: Sequence[str]) -> int:
    hits = 0
    for term in terms:
        if term == "?":
            if "?" in text:
                hits += 1
            continue
        if re.search(rf"\b{re.escape(term)}\b", text):
            hits += 1
    return hits


def _severity(text: str, ticket_type: str) -> tuple[str, str]:
    high = _count_hits(text, HIGH_SEV)
    med = _count_hits(text, MED_SEV)
    if high:
        return SEVERITY_HIGH, f"severity=high ({high} high-signal terms)"
    if ticket_type == TYPE_BUG and med:
        return SEVERITY_MEDIUM, "severity=medium (bug + error signal)"
    if med:
        return SEVERITY_MEDIUM, "severity=medium"
    if ticket_type == TYPE_FEATURE:
        return SEVERITY_LOW, "severity=low (feature request)"
    if ticket_type == TYPE_BUG:
        return SEVERITY_MEDIUM, "severity=medium (bug without crash signal)"
    return SEVERITY_LOW, "severity=low"
