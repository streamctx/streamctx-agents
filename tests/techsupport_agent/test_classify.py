"""Classification of sample support tickets (bug, how-to, unrelated)."""

from __future__ import annotations

from agents.techsupport_agent.classify import classify_text
from agents.techsupport_agent.models import (
    SEVERITY_HIGH,
    SEVERITY_LOW,
    SEVERITY_MEDIUM,
    TYPE_BUG,
    TYPE_FEATURE,
    TYPE_QUESTION,
)

HOW_TO = (
    "How do I run the coding agent pipeline?",
    "I cloned the repo. How do I run the coding agent pipeline and review pending_approval?",
)
BUG = (
    "Bug: sandbox pytest fails with TypeError in wrap()",
    "Traceback: TypeError: wrap() got an unexpected keyword. This is a regression "
    "in the coding agent sandbox. The process crashes on start.",
)
UNRELATED = (
    "Istio sidecar drops HTTP/2 when an eBPF kprobe is attached",
    "Our GKE cluster's Istio sidecar drops HTTP/2 streams when we attach a custom "
    "eBPF kprobe to the CNI. Completely unrelated to StreamCtx but the cluster is on fire.",
)
FEATURE = (
    "Please add dark mode to the dashboard",
    "Feature request: it would be nice to have a dark mode toggle. Enhancement / wishlist.",
)


def test_how_to_is_question_low_severity():
    result = classify_text(*HOW_TO)
    assert result.ticket_type == TYPE_QUESTION
    assert result.severity == SEVERITY_LOW
    assert result.confidence >= 0.6


def test_bug_report_is_bug_with_elevated_severity():
    result = classify_text(*BUG, labels=("bug",))
    assert result.ticket_type == TYPE_BUG
    assert result.severity in {SEVERITY_MEDIUM, SEVERITY_HIGH}
    assert result.confidence >= 0.6


def test_unrelated_complex_issue_stays_low_confidence_or_question():
    result = classify_text(*UNRELATED)
    assert result.ticket_type in {TYPE_QUESTION, TYPE_BUG}
    assert result.confidence < 0.6 or result.ticket_type == TYPE_QUESTION


def test_feature_request_from_label_and_body():
    result = classify_text(*FEATURE, labels=("enhancement",))
    assert result.ticket_type == TYPE_FEATURE
    assert result.severity == SEVERITY_LOW
    assert result.confidence >= 0.6
