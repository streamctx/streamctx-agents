"""Score Sales Navigator leads on role, company-size, and industry fit."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from agents.presales_agent.models import Lead, LeadScore

DEFAULT_RULES_PATH = Path(__file__).resolve().parent / "scoring_rules.json"
HEADCOUNT_RANGE_RE = re.compile(
    r"(\d[\d,]*)\s*(?:-|–|—|to)\s*(\d[\d,]*)",
    re.I,
)
HEADCOUNT_PLUS_RE = re.compile(r"(\d[\d,]*)\s*\+", re.I)
HEADCOUNT_SINGLE_RE = re.compile(r"(\d[\d,]*)")


@dataclass(frozen=True)
class RoleRule:
    score: float
    label: str
    patterns: tuple[re.Pattern[str], ...]
    max_headcount: Optional[int] = None


@dataclass(frozen=True)
class SizeBucket:
    min_count: int
    max_count: Optional[int]
    score: float
    label: str


@dataclass(frozen=True)
class IndustryRule:
    score: float
    label: str
    patterns: tuple[re.Pattern[str], ...]


@dataclass(frozen=True)
class ScoringRules:
    min_score: float
    role_weight: float
    size_weight: float
    industry_weight: float
    roles: tuple[RoleRule, ...]
    unknown_role_score: float
    size_buckets: tuple[SizeBucket, ...]
    unknown_size_score: float
    industries: tuple[IndustryRule, ...]
    unknown_industry_score: float

    @classmethod
    def load(cls, path: Optional[Path | str] = None) -> ScoringRules:
        rules_path = Path(path) if path is not None else default_rules_path()
        data = json.loads(rules_path.read_text(encoding="utf-8"))
        return cls.from_mapping(data)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> ScoringRules:
        weights = data.get("weights") or {}
        roles = tuple(
            RoleRule(
                score=float(item["score"]),
                label=str(item.get("label") or ""),
                patterns=tuple(_compile(p) for p in item.get("patterns") or []),
                max_headcount=_optional_int(item.get("max_headcount")),
            )
            for item in data.get("roles") or []
        )
        buckets = tuple(
            SizeBucket(
                min_count=int(item["min"]),
                max_count=_optional_int(item.get("max")),
                score=float(item["score"]),
                label=str(item.get("label") or ""),
            )
            for item in data.get("size_buckets") or []
        )
        industries = tuple(
            IndustryRule(
                score=float(item["score"]),
                label=str(item.get("label") or ""),
                patterns=tuple(_compile(p) for p in item.get("patterns") or []),
            )
            for item in data.get("industries") or []
        )
        return cls(
            min_score=float(data.get("min_score", 0.55)),
            role_weight=float(weights.get("role", 0.5)),
            size_weight=float(weights.get("company_size", 0.25)),
            industry_weight=float(weights.get("industry", 0.25)),
            roles=roles,
            unknown_role_score=float(data.get("unknown_role_score", 0.28)),
            size_buckets=buckets,
            unknown_size_score=float(data.get("unknown_size_score", 0.45)),
            industries=industries,
            unknown_industry_score=float(data.get("unknown_industry_score", 0.4)),
        )


def default_rules_path() -> Path:
    env = os.environ.get("PRESALES_SCORING_RULES")
    if env:
        return Path(env)
    return DEFAULT_RULES_PATH


def parse_headcount_range(value: str) -> Optional[tuple[int, Optional[int]]]:
    text = (value or "").replace(",", "").strip()
    if not text:
        return None
    ranged = HEADCOUNT_RANGE_RE.search(text)
    if ranged:
        lo = int(ranged.group(1))
        hi = int(ranged.group(2))
        return (min(lo, hi), max(lo, hi))
    plus = HEADCOUNT_PLUS_RE.search(text)
    if plus:
        return (int(plus.group(1)), None)
    single = HEADCOUNT_SINGLE_RE.search(text)
    if single:
        n = int(single.group(1))
        return (n, n)
    return None


def score_lead(lead: Lead, rules: Optional[ScoringRules] = None) -> LeadScore:
    spec = rules or ScoringRules.load()
    role_score, role_note = _score_role(lead.title, lead.company_size, spec)
    size_score, size_note = _score_size(lead.company_size, spec)
    industry_score, industry_note = _score_industry(lead.industry, spec)
    total = round(
        spec.role_weight * role_score
        + spec.size_weight * size_score
        + spec.industry_weight * industry_score,
        4,
    )
    rationale = (
        f"role={role_score:.2f} ({role_note}); "
        f"size={size_score:.2f} ({size_note}); "
        f"industry={industry_score:.2f} ({industry_note})"
    )
    return LeadScore(
        score=total,
        role_score=role_score,
        size_score=size_score,
        industry_score=industry_score,
        rationale=rationale,
    )


def rank_leads(
    leads: Sequence[Lead],
    rules: Optional[ScoringRules] = None,
) -> list[tuple[Lead, LeadScore]]:
    spec = rules or ScoringRules.load()
    scored = [(lead, score_lead(lead, spec)) for lead in leads]
    scored.sort(key=lambda item: (-item[1].score, item[0].name.lower()))
    return scored


def _score_role(
    title: str,
    company_size: str,
    rules: ScoringRules,
) -> tuple[float, str]:
    hay = (title or "").strip().lower()
    if not hay:
        return rules.unknown_role_score, "title missing"
    headcount = parse_headcount_range(company_size)
    max_seen = headcount[1] if headcount and headcount[1] is not None else (
        headcount[0] if headcount else None
    )
    for rule in rules.roles:
        if not any(pat.search(hay) for pat in rule.patterns):
            continue
        if rule.max_headcount is not None:
            if max_seen is None or max_seen > rule.max_headcount:
                continue
        return rule.score, rule.label
    return rules.unknown_role_score, "role not in buyer list"


def _score_size(company_size: str, rules: ScoringRules) -> tuple[float, str]:
    parsed = parse_headcount_range(company_size)
    if parsed is None:
        return rules.unknown_size_score, "company size unknown"
    lo, hi = parsed
    point = hi if hi is not None else lo
    for bucket in rules.size_buckets:
        upper = bucket.max_count
        if point < bucket.min_count:
            continue
        if upper is None or point <= upper:
            return bucket.score, bucket.label
    return rules.unknown_size_score, "company size unmatched"


def _score_industry(industry: str, rules: ScoringRules) -> tuple[float, str]:
    hay = (industry or "").strip().lower()
    if not hay:
        return rules.unknown_industry_score, "industry missing"
    for rule in rules.industries:
        if any(pat.search(hay) for pat in rule.patterns):
            return rule.score, rule.label
    return rules.unknown_industry_score, "industry unmatched"


def _compile(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.I)


def _optional_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    return int(value)
