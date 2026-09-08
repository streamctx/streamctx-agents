"""
StreamCtx Agents — Shared Configuration
Core safety rule: agents PROPOSE, humans EXECUTE.
DRY_RUN defaults to True everywhere. It is only ever flipped to False
by an explicit human action (CLI flag / approval-queue click) — never
by agent logic itself.
"""

import os

# --- Safety defaults ---
DRY_RUN_DEFAULT = True  # never hardcode False here

# --- Paths ---
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS_DIR = os.path.join(BASE_DIR, "logs")
AUDIT_LOG_PATH = os.path.join(LOGS_DIR, "audit_log.jsonl")

# --- Approval statuses (used by audit_log.py) ---
STATUS_PENDING = "pending_approval"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_EDITED_APPROVED = "edited_then_approved"

# --- Per-agent identity (used when calling get_tracker()) ---
AGENT_IDS = {
    "coding": "coding-agent",
    "marketing": "marketing-agent",
    "competitor": "competitor-agent",
    "research": "research-agent",
    "presales": "presales-agent",
    "techsupport": "techsupport-agent",
    "legal": "legal-compliance-agent",
}

# --- Daily/weekly pacing limits (marketing agent) ---
MAX_COMMUNITY_POSTS_PER_DAY = 2


# --- OpenRouter / LLM settings ---
import os
from dotenv import load_dotenv

load_dotenv()  # reads .env file into environment

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_MODEL = "google/gemma-4-26b-a4b-it:free"

# --- Marketing agent settings ---
MARKETING_DRAFTS_DIR = os.path.join(BASE_DIR, "agents", "marketing_agent", "drafts")


# --- Competitor agent settings ---
# Tracked names come from agents/competitor_agent/competitors.json (add/remove there).
COMPETITOR_RESEARCH_DIR = os.path.join(BASE_DIR, "agents", "competitor_agent", "research")


def _competitor_names_from_config():
    path = os.path.join(BASE_DIR, "agents", "competitor_agent", "competitors.json")
    try:
        import json

        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        names = [
            str(row["name"]).strip()
            for row in (data.get("competitors") or [])
            if row.get("name")
        ]
        if names:
            return names
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return ["LangSmith", "Langfuse", "Helicone", "Braintrust", "Laminar", "Latitude"]


COMPETITORS_TRACKED = _competitor_names_from_config()

