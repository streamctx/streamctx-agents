"""Config-driven safety checks that run before pending_approval inserts.

Self-promo filter: HN/Reddit only (rewrite, then block if still hot).
Duplicate guard: stagger near-identical stories across platforms on the same UTC day.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from agents.marketing_agent.models import PendingApprovalEntry, Story
from agents.marketing_agent.pending_approval import PendingApprovalStore

DEFAULT_RULES_PATH = Path(__file__).resolve().parent / "safety_rules.json"
NowFn = Callable[[], datetime]


class SafetyError(ValueError):
    """Base class for pre-queue safety refusals."""


class SelfPromoError(SafetyError):
    """HN/Reddit copy still reads as a pitch after rewrite."""


class DuplicateContentError(SafetyError):
    """Near-identical story already queued on another platform today."""


@dataclass(frozen=True)
class PromoPhrase:
    pattern: str
    weight: float


@dataclass(frozen=True)
class RewriteRule:
    pattern: str
    replace: str


@dataclass(frozen=True)
class SelfPromoRules:
    platforms: tuple[str, ...]
    threshold: float
    max_product_mentions: int
    extra_mention_weight: float
    on_fail: str
    product_names: tuple[str, ...]
    product_mention_rewrite: str
    phrases: tuple[PromoPhrase, ...]
    rewrite_replacements: tuple[RewriteRule, ...]


@dataclass(frozen=True)
class DuplicateRules:
    enabled: bool
    similarity_threshold: float
    min_normalized_chars: int
    block_same_platform: bool
    compare_statuses: tuple[str, ...]


@dataclass(frozen=True)
class SafetyRules:
    self_promo: SelfPromoRules
    duplicate: DuplicateRules

    @classmethod
    def load(cls, path: Optional[Path | str] = None) -> SafetyRules:
        rules_path = Path(path) if path is not None else default_rules_path()
        data = json.loads(rules_path.read_text(encoding="utf-8"))
        return cls.from_mapping(data)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> SafetyRules:
        promo = data.get("self_promo") or {}
        dup = data.get("duplicate") or {}
        phrases = tuple(
            PromoPhrase(pattern=str(item["pattern"]), weight=float(item.get("weight", 1.0)))
            for item in promo.get("phrases") or []
        )
        replacements = tuple(
            RewriteRule(pattern=str(item["pattern"]), replace=str(item.get("replace", "")))
            for item in promo.get("rewrite_replacements") or []
        )
        return cls(
            self_promo=SelfPromoRules(
                platforms=tuple(str(p) for p in promo.get("platforms") or ("hn", "reddit")),
                threshold=float(promo.get("threshold", 2.0)),
                max_product_mentions=int(promo.get("max_product_mentions", 1)),
                extra_mention_weight=float(promo.get("extra_mention_weight", 1.0)),
                on_fail=str(promo.get("on_fail") or "rewrite"),
                product_names=tuple(
                    str(n).lower() for n in promo.get("product_names") or ("streamctx",)
                ),
                product_mention_rewrite=str(promo.get("product_mention_rewrite") or "it"),
                phrases=phrases,
                rewrite_replacements=replacements,
            ),
            duplicate=DuplicateRules(
                enabled=bool(dup.get("enabled", True)),
                similarity_threshold=float(dup.get("similarity_threshold", 0.82)),
                min_normalized_chars=int(dup.get("min_normalized_chars", 40)),
                block_same_platform=bool(dup.get("block_same_platform", False)),
                compare_statuses=tuple(
                    str(s)
                    for s in dup.get("compare_statuses")
                    or ("pending", "approved", "published")
                ),
            ),
        )


def default_rules_path() -> Path:
    env = os.environ.get("MARKETING_SAFETY_RULES")
    if env:
        return Path(env)
    return DEFAULT_RULES_PATH


@dataclass
class SafetyGate:
    """Run self-promo + duplicate checks; never writes the queue itself."""

    rules: SafetyRules
    now_fn: NowFn = field(default=lambda: datetime.now(timezone.utc))

    @classmethod
    def default(cls) -> SafetyGate:
        return cls(rules=SafetyRules.load())

    def prepare(
        self,
        content: str,
        *,
        platform: str,
        store: PendingApprovalStore,
        fingerprint: Optional[str] = None,
        content_type: Optional[str] = None,
        target: Optional[str] = None,
    ) -> str:
        text = content
        promo = self.rules.self_promo
        # Personalized DMs are always human-reviewed; do not run the public-post
        # self-promo filter or the cross-platform story stagger on them.
        if content_type == "dm":
            _assert_dm_target_free(store, target)
            return text
        if platform in promo.platforms:
            text, score, reasons = review_self_promo(text, promo)
            if score >= promo.threshold:
                raise SelfPromoError(
                    f"{platform} content flagged as self-promo "
                    f"(score={score:.1f} >= {promo.threshold}): {'; '.join(reasons)}"
                )
        assert_not_duplicate(
            text,
            platform=platform,
            store=store,
            rules=self.rules.duplicate,
            fingerprint=fingerprint,
            today=self.now_fn().date().isoformat(),
        )
        return text


def story_fingerprint(story: Story) -> str:
    blob = " ".join(
        [story.headline, *list(story.key_facts or []), story.proof_point or ""]
    )
    return normalize_text(blob)


def normalize_text(text: str) -> str:
    lowered = (text or "").lower()
    lowered = re.sub(r"https?://\S+", " ", lowered)
    lowered = re.sub(r"[^a-z0-9\s]+", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def review_self_promo(content: str, rules: SelfPromoRules) -> tuple[str, float, list[str]]:
    text = content
    score, reasons = _score_self_promo(text, rules)
    if score >= rules.threshold and rules.on_fail == "rewrite":
        text = rewrite_self_promo(text, rules)
        score, reasons = _score_self_promo(text, rules)
    return text, score, reasons


def rewrite_self_promo(content: str, rules: SelfPromoRules) -> str:
    text = content
    for rule in rules.rewrite_replacements:
        text = re.sub(rule.pattern, rule.replace, text, flags=re.IGNORECASE)
    text = _cap_product_mentions(text, rules)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def assert_not_duplicate(
    content: str,
    *,
    platform: str,
    store: PendingApprovalStore,
    rules: DuplicateRules,
    fingerprint: Optional[str],
    today: str,
) -> None:
    if not rules.enabled:
        return
    incoming_content = normalize_text(content)
    incoming_fp = normalize_text(fingerprint) if fingerprint else incoming_content
    if len(incoming_fp) < rules.min_normalized_chars and len(incoming_content) < rules.min_normalized_chars:
        return

    existing = store.list_on_utc_day(today, statuses=rules.compare_statuses)
    for entry in existing:
        if entry.platform == platform and not rules.block_same_platform:
            continue
        other_fp = normalize_text(entry.source_fingerprint or "")
        other_content = normalize_text(entry.content)
        content_hit = _similar(incoming_content, other_content, rules)
        fingerprint_hit = _similar(incoming_fp, other_fp or other_content, rules)
        if content_hit or fingerprint_hit:
            raise DuplicateContentError(
                f"Near-identical story already queued today on {entry.platform} "
                f"(entry_id={entry.entry_id}). Stagger to another UTC day."
            )


def _score_self_promo(content: str, rules: SelfPromoRules) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []
    for phrase in rules.phrases:
        matches = re.findall(phrase.pattern, content, flags=re.IGNORECASE)
        if matches:
            score += phrase.weight * len(matches)
            reasons.append(phrase.pattern)
    mentions = _product_mention_count(content, rules.product_names)
    extra = mentions - rules.max_product_mentions
    if extra > 0:
        score += extra * rules.extra_mention_weight
        reasons.append(f"product_mentions={mentions}>{rules.max_product_mentions}")
    return score, reasons


def _product_mention_count(content: str, names: Sequence[str]) -> int:
    total = 0
    for name in names:
        total += len(re.findall(re.escape(name), content, flags=re.IGNORECASE))
    return total


def _cap_product_mentions(content: str, rules: SelfPromoRules) -> str:
    remaining = rules.max_product_mentions
    text = content

    def _repl(match: re.Match[str]) -> str:
        nonlocal remaining
        if remaining > 0:
            remaining -= 1
            return match.group(0)
        return rules.product_mention_rewrite

    for name in sorted(rules.product_names, key=len, reverse=True):
        text = re.sub(re.escape(name), _repl, text, flags=re.IGNORECASE)
    return text


def _similar(left: str, right: str, rules: DuplicateRules) -> bool:
    if not left or not right:
        return False
    if left == right and len(left) >= rules.min_normalized_chars:
        return True
    if min(len(left), len(right)) < rules.min_normalized_chars:
        return False
    return SequenceMatcher(None, left, right).ratio() >= rules.similarity_threshold


def _assert_dm_target_free(store: PendingApprovalStore, target: Optional[str]) -> None:
    if not target or not str(target).strip():
        raise DuplicateContentError("Outreach DM requires a target post URL or handle.")
    needle = str(target).strip()
    for status in ("pending", "approved", "published"):
        for entry in store.list_by_status(status):
            if entry.content_type == "dm" and (entry.target or "").strip() == needle:
                raise DuplicateContentError(
                    f"Outreach DM already queued for {needle} (entry_id={entry.entry_id})."
                )
