"""Parse LinkedIn Sales Navigator CSV exports. No network calls."""

from __future__ import annotations

import csv
import hashlib
import io
import re
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence, TextIO, Union

from agents.presales_agent.models import ImportResult, PIPELINE_IMPORTED
from agents.presales_agent.storage import LeadStore

SOURCE_COLUMNS = (
    "name",
    "title",
    "company",
    "company_size",
    "industry",
    "linkedin_url",
)

COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "name": ("name", "full name", "fullname", "lead name", "formatted name"),
    "first_name": ("first name", "firstname", "given name"),
    "last_name": ("last name", "lastname", "surname", "family name"),
    "title": ("title", "job title", "position", "headline", "jobtitle"),
    "company": (
        "company",
        "company name",
        "account",
        "account name",
        "company name for emails",
        "organization",
    ),
    "company_size": (
        "company size",
        "company_size",
        "employees",
        "company headcount",
        "company headcount range",
        "headcount",
        "employee count",
        "# employees",
        "number of employees",
    ),
    "industry": ("industry", "company industry", "company_industry", "account industry"),
    "linkedin_url": (
        "linkedin url",
        "linkedin_url",
        "person linkedin url",
        "linkedin profile url",
        "profile url",
        "person linkedin url",
        "linkedin profile",
        "member url",
        "profile linkedin url",
    ),
}

CsvInput = Union[str, Path, TextIO]


def _normalize_header(value: str) -> str:
    text = (value or "").replace("\ufeff", "").strip().lower()
    text = text.replace("_", " ").replace("-", " ")
    return re.sub(r"\s+", " ", text)


def _build_header_map(fieldnames: Sequence[str]) -> dict[str, str]:
    normalized = {_normalize_header(name): name for name in fieldnames if name}
    mapping: dict[str, str] = {}
    for canonical, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in normalized:
                mapping[canonical] = normalized[alias]
                break
    return mapping


def _cell(row: Mapping[str, str], header: Optional[str]) -> str:
    if not header:
        return ""
    return str(row.get(header) or "").strip()


def _compose_name(row: Mapping[str, str], headers: Mapping[str, str]) -> str:
    name = _cell(row, headers.get("name"))
    if name:
        return name
    first = _cell(row, headers.get("first_name"))
    last = _cell(row, headers.get("last_name"))
    return " ".join(part for part in (first, last) if part).strip()


def lead_fingerprint(
    *,
    name: str,
    company: str,
    linkedin_url: str,
) -> str:
    url = (linkedin_url or "").strip().lower().rstrip("/")
    if url:
        return "url:" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
    key = f"{(name or '').strip().lower()}|{(company or '').strip().lower()}"
    return "name:" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


def _open_csv(source: CsvInput) -> tuple[TextIO, bool]:
    if hasattr(source, "read"):
        return source, False  # type: ignore[return-value]
    path = Path(source)
    handle = path.open("r", encoding="utf-8-sig", newline="")
    return handle, True


def iter_csv_rows(source: CsvInput) -> tuple[list[str], list[dict[str, str]]]:
    """Return ``(original_headers, row dicts keyed by original headers)``."""
    handle, owns = _open_csv(source)
    try:
        sample = handle.read(4096)
        handle.seek(0)
        dialect = csv.excel
        if sample:
            try:
                guessed = csv.Sniffer().sniff(sample, delimiters=",\t;")
                delimiter = getattr(guessed, "delimiter", ",")
                if isinstance(delimiter, str) and len(delimiter) == 1:
                    dialect = guessed
            except (csv.Error, ValueError, TypeError):
                pass
        reader = csv.DictReader(handle, dialect=dialect)
        headers = list(reader.fieldnames or [])
        rows = [{k: (v if v is not None else "") for k, v in row.items()} for row in reader]
        return headers, rows
    finally:
        if owns:
            handle.close()


def parse_sales_nav_row(
    row: Mapping[str, str],
    headers: Mapping[str, str],
) -> Optional[dict[str, str]]:
    name = _compose_name(row, headers)
    title = _cell(row, headers.get("title"))
    company = _cell(row, headers.get("company"))
    company_size = _cell(row, headers.get("company_size"))
    industry = _cell(row, headers.get("industry"))
    linkedin_url = _cell(row, headers.get("linkedin_url"))
    if not name and not company and not linkedin_url:
        return None
    if not name:
        name = company or linkedin_url or "(unnamed lead)"
    return {
        "name": name,
        "title": title,
        "company": company,
        "company_size": company_size,
        "industry": industry,
        "linkedin_url": linkedin_url,
    }


def parse_sales_nav_csv(source: CsvInput) -> tuple[list[str], list[dict[str, str]], list[str]]:
    """Parse a Sales Navigator export. Missing columns become empty strings."""
    raw_headers, rows = iter_csv_rows(source)
    mapping = _build_header_map(raw_headers)
    warnings: list[str] = []
    missing = [col for col in SOURCE_COLUMNS if col not in mapping]
    if "name" not in mapping and "first_name" in mapping and "last_name" in mapping:
        missing = [col for col in missing if col != "name"]
    if "name" not in mapping and not (
        "first_name" in mapping and "last_name" in mapping
    ):
        warnings.append(
            "CSV is missing a name column (and First Name/Last Name); "
            "rows without company or LinkedIn URL will be skipped."
        )
    elif missing:
        warnings.append("CSV is missing columns: " + ", ".join(missing))

    parsed: list[dict[str, str]] = []
    for index, row in enumerate(rows, start=2):
        item = parse_sales_nav_row(row, mapping)
        if item is None:
            warnings.append(f"row {index}: empty — skipped")
            continue
        parsed.append(item)
    return raw_headers, parsed, warnings


def import_csv(
    source: CsvInput,
    *,
    store: LeadStore,
) -> ImportResult:
    """Insert/update leads from a CSV. Never hits LinkedIn."""
    _headers, parsed, warnings = parse_sales_nav_csv(source)
    imported = 0
    updated = 0
    skipped = 0
    lead_ids: list[str] = []
    errors = list(warnings)
    for item in parsed:
        fingerprint = lead_fingerprint(
            name=item["name"],
            company=item["company"],
            linkedin_url=item["linkedin_url"],
        )
        try:
            lead, created = store.upsert_lead(
                name=item["name"],
                title=item["title"],
                company=item["company"],
                company_size=item["company_size"],
                industry=item["industry"],
                linkedin_url=item["linkedin_url"] or None,
                source_fingerprint=fingerprint,
                pipeline_status=PIPELINE_IMPORTED,
            )
        except Exception as exc:
            skipped += 1
            errors.append(f"{item['name']}: {exc}")
            continue
        lead_ids.append(lead.lead_id)
        if created:
            imported += 1
        else:
            updated += 1
    skipped += sum(1 for note in warnings if note.startswith("row "))
    return ImportResult(
        imported=imported,
        skipped=skipped,
        updated=updated,
        errors=tuple(errors),
        lead_ids=tuple(lead_ids),
    )


def import_csv_text(text: str, *, store: LeadStore) -> ImportResult:
    return import_csv(io.StringIO(text), store=store)


def mapped_columns(fieldnames: Iterable[str]) -> dict[str, str]:
    return _build_header_map(list(fieldnames))
