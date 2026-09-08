"""Local heuristic fix candidates used when OpenRouter retries are exhausted."""

from __future__ import annotations

from typing import Callable

from agents.coding_agent.models import FixCandidate

FALLBACK_CONFIDENCE = 0.55
FALLBACK_REASON = "fallback heuristic, API unavailable"
FALLBACK_ATTESTATION = "LocalFallbackGenerator, no external API"
MAX_FALLBACK_CONFIDENCE = 0.7

HeuristicFn = Callable[[str, str], list[str]]


class LocalFallbackGenerator:
    """Generate degraded fix candidates without calling an external API."""

    def generate_heuristic_fixes(
        self,
        error_type: str,
        error_message: str,
        code_context: str,
        failed_call_id: str,
    ) -> list[FixCandidate]:
        snippets = _snippets_for(error_type, error_message, code_context)
        return [
            FixCandidate(
                candidate_code=snippet.strip(),
                confidence=FALLBACK_CONFIDENCE,
                reason=FALLBACK_REASON,
                attestation=FALLBACK_ATTESTATION,
                error_type=error_type or "UnknownError",
                failed_call_id=str(failed_call_id),
                generation_mode="fallback",
            )
            for snippet in snippets
        ]


def _snippets_for(error_type: str, error_message: str, code_context: str) -> list[str]:
    key = (error_type or "").strip()
    factory = _HEURISTICS.get(key, _generic_handler)
    return factory(error_message, code_context)


def _recursion_error(_error_message: str, _code_context: str) -> list[str]:
    return [
        (
            "remaining = initial\n"
            "while remaining is not None:\n"
            "    remaining = step(remaining)  # tail-call loop, no recursive self-call"
        ),
        (
            "def bounded(fn, *args, max_depth=1000):\n"
            "    if max_depth <= 0:\n"
            "        raise RecursionError('max recursion depth param exceeded')\n"
            "    return fn(*args, max_depth=max_depth - 1)"
        ),
    ]


def _attribute_error(_error_message: str, _code_context: str) -> list[str]:
    return [
        (
            "if hasattr(obj, 'attr'):\n"
            "    value = obj.attr\n"
            "else:\n"
            "    value = default"
        ),
        "value = getattr(obj, 'attr', default)",
    ]


def _key_error(_error_message: str, _code_context: str) -> list[str]:
    return [
        "value = mapping.get(key, default)",
        "mapping.setdefault(key, default)",
    ]


def _type_error(_error_message: str, _code_context: str) -> list[str]:
    return [
        (
            "if not isinstance(value, expected_type):\n"
            "    value = expected_type(value)  # type hint + isinstance check"
        ),
    ]


def _index_error(_error_message: str, _code_context: str) -> list[str]:
    return [
        (
            "if 0 <= index < len(items):\n"
            "    item = items[index]\n"
            "else:\n"
            "    item = default  # list bounds check / len() guard"
        ),
    ]


def _import_error(_error_message: str, _code_context: str) -> list[str]:
    return [
        (
            "try:\n"
            "    from optional_dep import impl as feature\n"
            "except ImportError:\n"
            "    feature = fallback_impl  # conditional / fallback import"
        ),
    ]


def _generic_handler(_error_message: str, _code_context: str) -> list[str]:
    return [
        (
            "try:\n"
            "    result = risky_call()\n"
            "except Exception:\n"
            "    logging.exception('fallback handler'); result = None"
        ),
    ]


_HEURISTICS: dict[str, HeuristicFn] = {
    "RecursionError": _recursion_error,
    "AttributeError": _attribute_error,
    "KeyError": _key_error,
    "TypeError": _type_error,
    "IndexError": _index_error,
    "ImportError": _import_error,
    "ModuleNotFoundError": _import_error,
}
