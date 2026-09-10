```python
"""LLM call tracker with SDK monkeypatching and context-diff engine."""

from __future__ import annotations

import hashlib
import threading
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from. import pricing
from.storage import get_storage
from.healer import SelfHealingEngine


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // 4)


def _extract_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
                elif "text" in block:
                    parts.append(str(block["text"]))
            elif hasattr(block, "text"):
                parts.append(str(block.text))
            else:
                parts.append(str(block))
        return "\n".join(parts)
    if hasattr(content, "text"):
        return str(content.text)
    return str(content)


def _normalize_messages(raw: Any) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    if raw is None:
        return messages
    items = raw if isinstance(raw, list) else [raw]
    for item in items:
        if isinstance(item, dict):
            role = str(item.get("role", "user"))
            content = _extract_text(item.get("content", ""))
            messages.append({"role": role, "content": content})
        elif hasattr(item, "role"):
            content = _extract_text(getattr(item, "content", ""))
            messages.append({"role": str(item.role), "content": content})
    return messages


@dataclass
class CallRecord:
    provider: str
    model: Optional[str]
    input_tokens: int
    output_tokens: int
    cost: float
    reused_tokens: int
    waste_category: Optional[str]
    messages: list[dict[str, str]]
    failed: bool = False
    healed: bool = False
    error_message: Optional[str] = None


@dataclass
class TrackerState:
    active: bool = False
    session_id: Optional[int] = None
    storage: Any = field(default_factory=get_storage)
    seen_hashes: set[str] = field(default_factory=set)
    waste_counter: Counter = field(default_factory=Counter)
    call_count: int = 0
    step_counter: int = 0
    auto_reported: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _originals: dict[str, Any] = field(default_factory=dict)
    _wrapped_clients: set[int] = field(default_factory=set)
    _last_messages: list[dict[str, str]] = field(default_factory=list)


class ContextDiffEngine:
    def __init__(self)->None:
        self._seen: dict[str,int]={}
        self._system_prompts: Counter = Counter()


    def analyze(self, messages: list[dict[str, str]]) -> tuple[int, Optional[str]]:
        reused = 0
        waste: Optional[str] = None
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if not content:
                continue
            h = _hash_text(f"{role}:{content}")
            tokens = _estimate_tokens(content)

            # NEW: system prompts are always 100% reusable across calls
            if role == "system":
                reused += tokens
                self._system_prompts[content[:120]] += 1
                waste = "repeated system prompt"
                continue   
 
            if h in self._seen:
                reused += tokens
                if role == "system":
                    if waste is None:
                        waste = "repeated user message"
            elif role == "assistant" and waste is None:
                waste ="repeated assistant context"

            self._seen[h] = tokens

        if waste is None and self._system_prompts:
            waste = "repeated system prompt"
        return reused, waste

    def biggest_waste(self) -> Optional[str]:
        if self._system_prompts:
            return "repeated system prompt"
        return None


class LLMTracker:
    def __init__(self, agent_id: str = "default"):
        self.agent_id = agent_id
        self.session_id = None
        self.state = TrackerState()
        self.diff = ContextDiffEngine()
        self.healer = SelfHealingEngine()


    def start(self) -> None:
        with self.state._lock:
            if self.state.active:
                return
            self.state.active = True
            self.state.session_id = self.state.storage.start_session()
            self._patch_sdks()

    def stop(self) -> None:
        with self.state._lock:
            if not self.state.active:
                return
            self._unpatch_sdks()
            if self.state.session_id is not None:
                self.state.storage.end_session(self.state.session_id)
            self.state.active = False

    def _ensure_session(self) -> None:
        if not self.state.active:
            self.state.active = True
            self.state.session_id = self.state.storage.start_session()

    def checkpoint(self) -> None:
        self._ensure_session()
        if self.state.session_id is None:
            return
        with self.state._lock:
            messages = list(self.state._last_messages)
            step = self.state.step_counter
        self.state.storage.save_checkpoint(
            self.state.session_id, step, messages
        )

    def resume(self, session_id: int) -> list[dict[str, str]]:
        return self.state.storage.resume_from_checkpoint(session_id)

    def get_session_id(self) -> Optional[int]:
        return self.state.session_id

    def healing_stats(self) -> dict[str, Any]:
        return self.healer.get_stats()

    def wrap(self, client: Any) -> Any:
        self._ensure_session()
        client_id = id(client)
        if client_id in self.state._wrapped_clients:
            return client

        if hasattr(client, "chat") and hasattr(client.chat, "completions"):
            key = "openai.resources.chat.completions.Completions.create"
            if key in self.state._originals:
                self.state._wrapped_clients.add(client_id)
                return client
            import openai.resources.chat.completions.completions as completions_mod
            completions = client.chat.completions
            original_create = self.state._originals.get(
                key, completions_mod.Completions.create
            )
            tracker = self

            def patched_create(*args: Any, **kwargs: Any) -> Any:
                return tracker._intercept_call(
                    lambda: original_create(completions, *args, **kwargs),
                    provider="openai",
                    kwargs=kwargs,
                )

            completions.create = patched_create
            self.state._wrapped_clients.add(client_id)
            return client

        if hasattr(client, "messages") and hasattr(client.messages, "create"):
            key = "anthropic.resources.messages.Messages.create"
            if key in self.state._originals:
                self.state._wrapped_clients.add(client_id)
                return client
            import anthropic.resources.messages.messages as messages_mod
            messages = client.messages
            original_create = self.state._originals.get(
                key, messages_mod.Messages.create
            )
            tracker = self

            def patched_create(*args: Any, **kwargs: Any) -> Any:
                return tracker._intercept_call(
                    lambda: original_create(messages, *args, **kwargs),
                    provider="anthropic",
                    kwargs=kwargs,
                )

            messages.create = patched_create
            self.state._wrapped_clients.add(client_id)
            return client

        raise TypeError(
            "streamctx.wrap() supports OpenAI and Anthropic SDK clients only."
        )

    def get_stats(self) -> dict[str, Any]:
        if self.state.session_id is None:
            return {
                "call_count": 0,
                "total_tokens": 0,
                "total_cost": 0.0,
                "reused_tokens": 0,
                "biggest_waste": None,
            }
        return self.state.storage.get_session_stats(self.state.session_id)

    def _patch_sdks(self) -> None:
        self._patch_openai()
        self._patch_anthropic()

    def _unpatch_sdks(self) -> None:
        for key, original in list(self.state._originals.items()):
            cls = self._resolve_patch_target(key)
            if cls is not None:
                setattr(cls, "create", original)
        self.state._originals.clear()
        self.state._wrapped_clients.clear()

    def _resolve_patch_target(self, key: str) -> Any:
        try:
            if key.startswith("openai."):
                import openai.resources.chat.completions.completions as mod
                return mod.Completions
            if key.startswith("anthropic."):
                import anthropic.resources.messages.messages as mod
                return mod.Messages
        except ImportError:
            return None
        return None

    def _patch_openai(self) -> None:
        try:
            import openai
        except ImportError:
            return
        if hasattr(openai, "resources") and hasattr(openai.resources, "chat"):
            completions_cls = openai.resources.chat.completions.Completions
            key = "openai.resources.chat.completions.Completions.create"
            if key not in self.state._originals:
                self.state._originals[key] = completions_cls.create
                tracker = self

                def patched_create(self_completions: Any, *args: Any, **kwargs: Any) -> Any:
                    original = tracker.state._originals[key]
                    return tracker._intercept_call(
                        lambda: original(self_completions, *args, **kwargs),
                        provider="openai",
                        kwargs=kwargs,
                    )

                completions_cls.create = patched_create

    def _patch_anthropic(self) -> None:
        try:
            import anthropic
        except ImportError:
            return
        if hasattr(anthropic, "resources") and hasattr(anthropic.resources, "messages"):
            messages_cls = anthropic.resources.messages.Messages
            key = "anthropic.resources.messages.Messages.create"
            if key not in self.state._originals:
                self.state._originals[key] = messages_cls.create
                tracker = self

                def patched_create(self_messages: Any, *args: Any, **kwargs: Any) -> Any:
                    original = tracker.state._originals[key]
                    return tracker._intercept_call(
                        lambda: original(self_messages, *args, **kwargs),
                        provider="anthropic",
                        kwargs=kwargs,
                    )

                messages_cls.create = patched_create

    def _intercept_call(
        self,
        fn: Callable[[], Any],
        provider: str,
        kwargs: dict[str, Any],
    ) -> Any:
        if not self.state.active:
            return fn()

        model = kwargs.get("model")
        messages = _normalize_messages(kwargs.get("messages"))
        if provider == "anthropic" and not messages:
            system = kwargs.get("system")
            if system:
                messages = [{"role": "system", "content": _extract_text(system)}]
            user_msgs = _normalize_messages(kwargs.get("messages"))
            messages.extend(user_msgs)

        reused, waste = self.diff.analyze(messages)

        # --- NEW: actually run compression to get real token savings ---
        from.compressor import compress_messages
        _, orig_tok, comp_tok = compress_messages(messages)
        compression_savings = max(0, orig_tok - comp_tok)
        context_savings_tokens = compression_savings + reused

        # Try LLM call — self-heal on failure
        call_failed = False
        call_healed = False
        call_error_message: Optional[str] = None
        try:
            response = fn()
            self.healer.record_success(messages, response)
        except Exception as e:
            call_failed = True
            call_error_message = str(e)[:500]  # keep it bounded
            self.healer.record_failure()
            if self.healer.can_heal():
                call_healed = True
                recovery_msgs = self.healer.get_recovery_messages(messages)
                with self.state._lock:
                    self.state._last_messages = recovery_msgs

            # Persist the failure itself before re-raising, so the
            # Attribution Engine has a record to analyze.
            failure_record = CallRecord(
                provider=provider,
                model=model,
                input_tokens=0,
                output_tokens=0,
                cost=0.0,
                reused_tokens=0,
                waste_category=None,
                messages=messages,
                failed=call_failed,
                healed=call_healed,
                error_message=call_error_message,
            )
            self._persist(failure_record)
            with self.state._lock:
                self.state.call_count += 1
                self.state.step_counter += 1
                self.state._last_messages = list(messages)
                step = self.state.step_counter
            if self.state.session_id is not None:
                self.state.storage.save_checkpoint(
                    self.state.session_id, step, messages
                )
            raise
      

        input_tokens, output_tokens = self._extract_usage(
            response, provider, messages, kwargs
        )
        cost = pricing.estimate_cost(model, input_tokens, output_tokens)

        record = CallRecord(
            provider=provider,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost=cost,
            reused_tokens=context_savings_tokens,
            waste_category=waste,
            messages=messages,
            failed=call_failed,
            healed=call_healed,
            error_message=call_error_message,
        )
        self._persist(record)

        with self.state._lock:
            self.state.step_counter += 1
            self.state._last_messages = list(messages)
            step = self.state.step_counter

        if self.state.session_id is not None:
            self.state.storage.save_checkpoint(
                self.state.session_id, step, messages
            )

        with self.state._lock:
            self.state.call_count += 1
            if waste:
                self.state.waste_counter[waste] += 1
            first_call = self.state.call_count == 1 and not self.state.auto_reported
            if first_call:
                self.state.auto_reported = True

        if first_call:
            from.reporter import print_auto_summary
            print_auto_summary(self)

        return response

    def _persist(self, record: CallRecord) -> None:
        if self.state.session_id is None:
            return
        self.state.storage.record_call(
            session_id=self.state.session_id,
            provider=record.provider,
            model=record.model,
            input_tokens=record.input_tokens,
            output_tokens=record.output_tokens,
            cost=record.cost,
            reused_tokens=record.reused_tokens,
            waste_category=record.waste_category,
            messages=record.messages,
            failed=record.failed,
            healed=record.healed,
            error_message=record.error_message,
        )

    def _extract_usage(
        self,
        response: Any,
        provider: str,
        messages: list[dict[str, str]],
        kwargs: dict[str, Any],
    ) -> tuple[int, int]:
        input_tokens = 0
        output_tokens = 0
        usage = getattr(response, "usage", None)
        if usage is not None:
            input_tokens = int(
                getattr(usage, "prompt_tokens", None)
                or getattr(usage, "input_tokens", None)
                or 0
            )
            output_tokens = int(
                getattr(usage, "completion_tokens", None)
                or getattr(usage, "output_tokens", None)
                or 0
            )
        elif isinstance(response, dict) and "usage" in response:
            u = response["usage"]
            input_tokens = int(u.get("prompt_tokens") or u.get("input_tokens") or 0)
            output_tokens = int(u.get("completion_tokens") or u.get("output_tokens") or 0)

        if input_tokens == 0:
            input_tokens = sum(_estimate_tokens(m["content"]) for m in messages)
            max_tokens = kwargs.get("max_tokens")
            if max_tokens:
                input_tokens += int(max_tokens) // 10

        if output_tokens == 0:
            if provider == "openai":
                try:
                    output_tokens = _estimate_tokens(response.choices[0].message.content)
                except (AttributeError, IndexError, TypeError):
                    output_tokens = 0
            elif provider == "anthropic":
                try:
                    output_tokens = _estimate_tokens(_extract_text(response.content))
                except (AttributeError, IndexError, TypeError):
                    output_tokens = 0

        return input_tokens, output_tokens


_trackers: dict[str, "LLMTracker"] = {}
_trackers_lock = threading.Lock()

DEFAULT_AGENT_ID = "default"


def get_tracker(agent_id: str | None = None) -> "LLMTracker":
    """
    Return the LLMTracker for a given agent_id.

    Each distinct agent_id gets its own isolated LLMTracker instance,
    so multiple agents running in the same process don't mix contexts,
    sessions, or checkpoints.

    Calling get_tracker() with no agent_id returns the default tracker
    (fully backward-compatible with single-agent usage).
    """
    key = agent_id or DEFAULT_AGENT_ID
    with _trackers_lock:
        if key not in _trackers:
            _trackers[key] = LLMTracker(agent_id=key)
        return _trackers[key]


def list_active_agents() -> list[str]:
    """Return agent_ids of all trackers currently active (started)."""
    with _trackers_lock:
        return [aid for aid, t in _trackers.items() if t.state.active]
```