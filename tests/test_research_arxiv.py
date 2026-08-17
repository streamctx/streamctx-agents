"""Unit tests for Stage 1 arXiv Atom parsing and keyword filtering."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from agents.research_agent.arxiv import (
    arxiv_search_url,
    canonicalize_arxiv_url,
    fetch_arxiv_papers,
    matches_keywords,
    papers_to_source_items,
    parse_arxiv_atom,
)
from agents.research_agent.models import SOURCE_TYPE_ARXIV
from agents.research_agent.settings import ResearchConfig

ATOM = """\
<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2401.11111v1</id>
    <title>Silent Failures in Multi-Agent Memory Management</title>
    <published>2026-08-16T00:00:00Z</published>
    <summary>We measure agent reliability when context compression drops tool results.</summary>
    <link href="http://arxiv.org/abs/2401.11111v1" rel="alternate" type="text/html"/>
    <category term="cs.AI"/>
    <category term="cs.MA"/>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2401.22222v2</id>
    <title>A Survey of Image Classifiers</title>
    <published>2026-08-15T00:00:00Z</published>
    <summary>Convolutional nets on ImageNet. No agents.</summary>
    <link href="http://arxiv.org/abs/2401.22222v2" rel="alternate" type="text/html"/>
    <category term="cs.AI"/>
  </entry>
</feed>
"""


def test_parse_arxiv_atom_canonicalizes_abs_url_and_strips_version():
    papers = parse_arxiv_atom(ATOM)
    assert len(papers) == 2
    assert papers[0].html_url == "https://arxiv.org/abs/2401.11111"
    assert papers[0].arxiv_id == "2401.11111"
    assert "Multi-Agent Memory" in papers[0].title
    assert papers[0].categories == ("cs.AI", "cs.MA")
    assert papers[0].published.startswith("2026-08-16")


def test_canonicalize_arxiv_url():
    assert canonicalize_arxiv_url("http://arxiv.org/abs/2401.1v3") == "https://arxiv.org/abs/2401.1"
    assert canonicalize_arxiv_url("https://arxiv.org/abs/2401.1") == "https://arxiv.org/abs/2401.1"


def test_matches_keywords_is_case_insensitive():
    assert matches_keywords("Agent Reliability under load", ("agent reliability",))
    assert not matches_keywords("a survey of image classifiers", ("multi-agent failure",))


def test_arxiv_search_url_includes_category_and_quoted_phrases():
    url = arxiv_search_url(
        "cs.AI",
        ("agent reliability", "checkpoint"),
        max_results=25,
    )
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    assert parsed.netloc == "export.arxiv.org"
    search = query["search_query"][0]
    assert "cat:cs.AI" in search
    assert 'all:"agent reliability"' in search
    assert "all:checkpoint" in search
    assert query["sortBy"] == ["submittedDate"]
    assert query["max_results"] == ["25"]


def test_fetch_arxiv_papers_filters_keywords_and_dedupes_across_categories():
    urls: list[str] = []

    def fetch(url: str) -> str:
        urls.append(url)
        return ATOM

    spec = ResearchConfig(
        poll_delay_seconds=3.0,
        arxiv_categories=("cs.AI", "cs.MA"),
        arxiv_keywords=("agent reliability", "memory management", "multi-agent"),
    )
    sleeps: list[float] = []
    papers = fetch_arxiv_papers(spec, fetch_fn=fetch, sleep_fn=sleeps.append)
    assert len(urls) == 2
    assert "cat:cs.AI" in parse_qs(urlparse(urls[0]).query)["search_query"][0]
    assert "cat:cs.MA" in parse_qs(urlparse(urls[1]).query)["search_query"][0]
    assert sleeps == [3.0]
    assert [paper.html_url for paper in papers] == ["https://arxiv.org/abs/2401.11111"]
    items = papers_to_source_items(papers, excerpt_max_chars=80)
    assert items[0].source_type == SOURCE_TYPE_ARXIV
    assert "context compression" in items[0].content_excerpt
