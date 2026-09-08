"""CSV import from Sales Navigator exports — no LinkedIn network calls."""

from __future__ import annotations

from pathlib import Path

from agents.presales_agent.csv_import import (
    import_csv,
    mapped_columns,
    parse_sales_nav_csv,
)
from agents.presales_agent.storage import LeadStore

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "presales"
NAV_CSV = FIXTURES / "sales_navigator_export.csv"
SPARSE_CSV = FIXTURES / "sparse_export.csv"


def test_sales_nav_headers_map_to_canonical_columns():
    mapping = mapped_columns(
        [
            "First Name",
            "Last Name",
            "Title",
            "Company",
            "Company Size",
            "Industry",
            "LinkedIn Profile Url",
        ]
    )
    assert mapping["first_name"] == "First Name"
    assert mapping["title"] == "Title"
    assert mapping["linkedin_url"] == "LinkedIn Profile Url"


def test_parse_real_sales_nav_export_composes_names_and_skips_empty():
    headers, rows, warnings = parse_sales_nav_csv(NAV_CSV)
    assert "First Name" in headers
    names = {row["name"] for row in rows}
    assert "Avery Chen" in names
    assert "Sam Okoye" in names
    assert "Casey Walsh" in names
    avery = next(row for row in rows if row["name"] == "Avery Chen")
    assert avery["title"] == "CTO"
    assert avery["company"] == "Northwind AI"
    assert avery["company_size"] == "51-200"
    assert avery["industry"] == "Artificial Intelligence"
    assert "linkedin.com/in/avery-chen-example" in avery["linkedin_url"]
    assert any("empty" in note for note in warnings)


def test_missing_columns_are_empty_not_fatal():
    headers, rows, warnings = parse_sales_nav_csv(SPARSE_CSV)
    assert any("missing" in note.lower() for note in warnings)
    assert len(rows) == 2
    eng = next(row for row in rows if "Acme" in row["company"])
    assert eng["name"] == "Acme Analytics"
    assert eng["title"] == "VP of Engineering"
    assert eng["linkedin_url"] == ""
    assert eng["company_size"] == ""


def test_import_is_idempotent_on_linkedin_url(tmp_path: Path):
    db = tmp_path / "leads.db"
    store = LeadStore(db_path=db)
    try:
        first = import_csv(NAV_CSV, store=store)
        second = import_csv(NAV_CSV, store=store)
        assert first.imported >= 6
        assert second.imported == 0
        assert second.updated == first.imported
        leads = store.list_leads(order_by_score=False)
        urls = [lead.linkedin_url for lead in leads if lead.linkedin_url]
        assert len(urls) == len(set(urls))
    finally:
        store.close()
