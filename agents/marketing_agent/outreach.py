"""Outreach: discover public posts, score vs StreamCtx features, queue DM drafts.

Always ``content_type=dm`` and ``mode=draft_only`` — never auto-sends, even
on Twitter/Reddit. No new packages; Twitter search is skipped without a bearer.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode

from agents.marketing_agent.http import JsonHttpClient, JsonHttpError
from agents.marketing_agent.models import PublicPost, ScoredLead
from agents.marketing_agent.pending_approval import (
    CONTENT_DM,
    MODE_DRAFT_ONLY,
    STATUS_PENDING,
    PendingApprovalStore,
)
from agents.marketing_agent.safety import DuplicateContentError, SafetyGate
from agents.marketing_agent.settings import TWITTER_BEARER_TOKEN

DEFAULT_OUTREACH_RULES = Path(__file__).resolve().parent / "outreach_rules.json"

HN_SEARCH_URL = "https://hn.algolia.com/api/v1/search"  # discovery only; replies still use item?id=
REDDIT_SEARCH_URL = "https://www.reddit.com/search.json"
TWITTER_SEARCH_URL = "https://api.twitter.com/2/tweets/search/recent"

BOILERPLATE = (
    "just wanted to reach out",
    "i came across your post",
    "great post",
    "i'd love to connect",
    "hope this helps",
    "check out our",
)

FEATURE_REPLIES = {
    "compression": (
        "If earlier turns are vanishing, that is usually reuse/truncation of the "
        "message list, not the model 'forgetting.' Keeping a full checkpoint and "
        "compressing only under a budget is the lever that actually preserves them."
    ),
    "checkpoint_resume": (
        "A crash that throws away the whole transcript is a missing snapshot, not "
        "an LLM quirk. Persist the message list at step N and resume(session_id) "
        "instead of replaying from zero."
    ),
    "observability": (
        "If you cannot see which call burned the tokens or failed, wrap() on the "
        "client plus get_stats() is the boring fix — per-call rows, not a black box."
    ),
    "attribution_replay": (
        "When a later step fails, guessing the rotten turn is optional: attribute "
        "DRIFT/COMPRESSION/RECENCY on the session, then dry-run a counterfactual "
        "replay from that step to see if the diagnosis actually reproduces."
    ),
}


@dataclass(frozen=True)
class FeatureSpec:
    key: str
    label: str
    search_queries: tuple[str, ...]
    keywords: tuple[str, ...]


@dataclass(frozen=True)
class OutreachRules:
    min_score: float
    sources: tuple[str, ...]
    search_limit: int
    features: tuple[FeatureSpec, ...]

    @classmethod
    def load(cls, path: Optional[Path | str] = None) -> OutreachRules:
        rules_path = Path(path) if path is not None else default_outreach_rules_path()
        data = json.loads(rules_path.read_text(encoding="utf-8"))
        features = tuple(
            FeatureSpec(
                key=str(key),
                label=str(spec.get("label") or key),
                search_queries=tuple(str(q) for q in spec.get("search_queries") or []),
                keywords=tuple(str(k) for k in spec.get("keywords") or []),
            )
            for key, spec in (data.get("features") or {}).items()
        )
        return cls(
            min_score=float(data.get("min_score", 0.3)),
            sources=tuple(str(s) for s in data.get("sources") or ("hn", "reddit", "twitter")),
            search_limit=int(data.get("search_limit", 8)),
            features=features,
        )


def default_outreach_rules_path() -> Path:
    env = os.environ.get("MARKETING_OUTREACH_RULES")
    if env:
        return Path(env)
    return DEFAULT_OUTREACH_RULES


class Outreach:
    """Discover → score → personalized DM draft → pending_approval (draft_only)."""

    def __init__(
        self,
        store: PendingApprovalStore,
        *,
        http: Optional[JsonHttpClient] = None,
        rules: Optional[OutreachRules] = None,
        safety: Optional[SafetyGate] = None,
        twitter_bearer: Optional[str] = None,
    ) -> None:
        self.store = store
        self.http = http or JsonHttpClient()
        self.rules = rules or OutreachRules.load()
        self.safety = safety or SafetyGate.default()
        self.twitter_bearer = (
            twitter_bearer if twitter_bearer is not None else TWITTER_BEARER_TOKEN
        )

    def run(self, *, min_score: Optional[float] = None, limit: int = 20) -> list[str]:
        """Return entry_ids queued. Never calls a publish/post API."""
        threshold = self.rules.min_score if min_score is None else min_score
        posts = self.discover()
        leads = [lead for lead in (score_lead(p, self.rules) for p in posts) if lead.score >= threshold]
        leads.sort(key=lambda item: item.score, reverse=True)
        queued: list[str] = []
        for lead in leads[:limit]:
            entry_id = self.enqueue_draft(lead)
            if entry_id:
                queued.append(entry_id)
        return queued

    def discover(self) -> list[PublicPost]:
        found: list[PublicPost] = []
        seen: set[str] = set()
        for source in self.rules.sources:
            try:
                batch = self._discover_source(source)
            except JsonHttpError:
                continue
            for post in batch:
                if post.url in seen:
                    continue
                seen.add(post.url)
                found.append(post)
        return found

    def enqueue_draft(self, lead: ScoredLead) -> Optional[str]:
        draft = generate_outreach_draft(lead)
        try:
            prepared = self.safety.prepare(
                draft,
                platform=lead.post.platform,
                store=self.store,
                fingerprint=f"outreach|{lead.post.platform}|{lead.post.post_id}",
                content_type=CONTENT_DM,
                target=lead.post.url,
            )
        except DuplicateContentError:
            return None
        entry = self.store.create_entry(
            platform=lead.post.platform,
            content_type=CONTENT_DM,
            content=prepared,
            target=lead.post.url,
            mode=MODE_DRAFT_ONLY,
            status=STATUS_PENDING,
            source_fingerprint=f"outreach|{lead.post.platform}|{lead.post.post_id}",
        )
        return entry.entry_id

    def _discover_source(self, source: str) -> list[PublicPost]:
        queries = _search_queries(self.rules)
        if source == "hn":
            return self._search_hn(queries)
        if source == "reddit":
            return self._search_reddit(queries)
        if source == "twitter":
            return self._search_twitter(queries)
        return []

    def _search_hn(self, queries: list[str]) -> list[PublicPost]:
        posts: list[PublicPost] = []
        for query in queries:
            url = f"{HN_SEARCH_URL}?{urlencode({'query': query, 'hitsPerPage': self.rules.search_limit})}"
            payload = self.http.get_json(url)
            for hit in payload.get("hits") or []:
                post = _post_from_hn(hit)
                if post:
                    posts.append(post)
        return posts

    def _search_reddit(self, queries: list[str]) -> list[PublicPost]:
        posts: list[PublicPost] = []
        for query in queries:
            url = f"{REDDIT_SEARCH_URL}?{urlencode({'q': query, 'sort': 'new', 'limit': self.rules.search_limit})}"
            payload = self.http.get_json(url)
            children = ((payload.get("data") or {}).get("children")) or []
            for child in children:
                post = _post_from_reddit(child.get("data") or {})
                if post:
                    posts.append(post)
        return posts

    def _search_twitter(self, queries: list[str]) -> list[PublicPost]:
        if not self.twitter_bearer:
            return []
        posts: list[PublicPost] = []
        headers = {"Authorization": f"Bearer {self.twitter_bearer}"}
        for query in queries:
            q = f"({query}) -is:retweet lang:en"
            url = f"{TWITTER_SEARCH_URL}?{urlencode({'query': q, 'max_results': max(10, self.rules.search_limit)})}"
            payload = self.http.get_json(url, headers=headers)
            for item in payload.get("data") or []:
                post = _post_from_twitter(item)
                if post:
                    posts.append(post)
        return posts


def score_lead(post: PublicPost, rules: OutreachRules) -> ScoredLead:
    blob = f"{post.title}\n{post.body}"
    hits_by_feature: dict[str, list[str]] = {}
    for feature in rules.features:
        hits = [kw for kw in feature.keywords if _has_term(blob, kw)]
        if hits:
            hits_by_feature[feature.key] = hits
    if not hits_by_feature:
        return ScoredLead(
            post=post,
            score=0.0,
            matched_features=(),
            matched_terms=(),
            reason="no pain-point match",
            primary_feature="",
        )

    order = {feature.key: index for index, feature in enumerate(rules.features)}
    primary = min(
        hits_by_feature,
        key=lambda key: (-len(hits_by_feature[key]), order.get(key, 99)),
    )
    primary_n = len(hits_by_feature[primary])
    total_n = sum(len(v) for v in hits_by_feature.values())
    specificity = primary_n / total_n
    depth = min(1.0, primary_n / 3.0)
    score = round(min(1.0, 0.35 + 0.45 * specificity + 0.20 * depth), 2)
    terms = tuple(term for group in hits_by_feature.values() for term in group)
    features = tuple(hits_by_feature.keys())
    labels = {f.key: f.label for f in rules.features}
    reason = (
        f"Closest StreamCtx fit: {labels.get(primary, primary)} "
        f"({primary_n} term(s); specificity={specificity:.2f})."
    )
    return ScoredLead(
        post=post,
        score=score,
        matched_features=features,
        matched_terms=terms,
        reason=reason,
        primary_feature=primary,
    )


def generate_outreach_draft(lead: ScoredLead) -> str:
    """Personalized unsent DM — quotes the lead's own wording, not a form letter."""
    snippet = _snippet(lead)
    primary = lead.primary_feature or (
        lead.matched_features[0] if lead.matched_features else "observability"
    )
    reply = FEATURE_REPLIES.get(primary, FEATURE_REPLIES["observability"])
    title = lead.post.title.strip() or "(untitled)"
    who = f"@{lead.post.author}" if lead.post.author else "you"
    draft = (
        f'{who} — on "{title}" you wrote:\n'
        f'"{snippet}"\n\n'
        f"{reply}\n\n"
        f"That is StreamCtx { _feature_label(primary) }. "
        f"Source: {lead.post.url}"
    )
    lowered = draft.lower()
    for banned in BOILERPLATE:
        if banned in lowered:
            draft = re.sub(banned, "", draft, flags=re.IGNORECASE)
    return draft.strip()


def _feature_label(key: str) -> str:
    mapping = {
        "compression": "context compression",
        "checkpoint_resume": "checkpoint/resume",
        "observability": "call-level observability",
        "attribution_replay": "attribution + counterfactual replay",
    }
    return mapping.get(key, key)


def _snippet(lead: ScoredLead, max_chars: int = 180) -> str:
    blob = (lead.post.body or lead.post.title or "").strip()
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", blob) if s.strip()]
    for term in lead.matched_terms:
        for sentence in sentences:
            if term.lower() in sentence.lower():
                return _clip(sentence.strip("\"'"), max_chars)
    return _clip(blob, max_chars)


def _clip(text: str, max_chars: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rsplit(" ", 1)[0].rstrip(",;:") + "…"


def _has_term(text: str, keyword: str) -> bool:
    hay = text.lower()
    needle = keyword.lower()
    if " " in needle:
        return needle in hay
    return re.search(rf"\b{re.escape(needle)}\b", hay) is not None


def _search_queries(rules: OutreachRules) -> list[str]:
    queries: list[str] = []
    for feature in rules.features:
        if feature.search_queries:
            queries.append(feature.search_queries[0])
    return queries or ["llm agent context loss"]


def _post_from_hn(hit: dict[str, Any]) -> Optional[PublicPost]:
    post_id = str(hit.get("objectID") or "").strip()
    if not post_id:
        return None
    title = str(hit.get("title") or hit.get("story_title") or "").strip()
    body = _strip_html(str(hit.get("comment_text") or hit.get("story_text") or ""))
    if not title and not body:
        return None
    return PublicPost(
        platform="hn",
        post_id=post_id,
        url=f"https://news.ycombinator.com/item?id={post_id}",
        author=str(hit.get("author") or ""),
        title=title or "(hn comment)",
        body=body,
        created_at=str(hit.get("created_at") or "") or None,
    )


def _post_from_reddit(data: dict[str, Any]) -> Optional[PublicPost]:
    post_id = str(data.get("id") or "").strip()
    permalink = str(data.get("permalink") or "").strip()
    if not post_id:
        return None
    url = permalink if permalink.startswith("http") else f"https://www.reddit.com{permalink}"
    title = str(data.get("title") or "").strip()
    body = str(data.get("selftext") or data.get("body") or "").strip()
    if not title and not body:
        return None
    return PublicPost(
        platform="reddit",
        post_id=post_id,
        url=url,
        author=str(data.get("author") or ""),
        title=title or "(reddit post)",
        body=body,
        created_at=str(data.get("created_utc") or "") or None,
    )


def _post_from_twitter(item: dict[str, Any]) -> Optional[PublicPost]:
    post_id = str(item.get("id") or "").strip()
    text = str(item.get("text") or "").strip()
    if not post_id or not text:
        return None
    return PublicPost(
        platform="twitter",
        post_id=post_id,
        url=f"https://twitter.com/i/web/status/{post_id}",
        author=str(item.get("author_id") or ""),
        title=text.split("\n", 1)[0][:80],
        body=text,
    )


def _strip_html(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", text).strip()
