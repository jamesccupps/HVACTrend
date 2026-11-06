"""LLM provider abstraction.

Routable per report type (config.ai.routing). The summary (ai/summary.py) builds the input
and checks the output; this module only makes the call, so the rest is provider-agnostic.

A provider returns the model's STRUCTURED reply (a dict matching the schema it was given)
plus what the call itself reported: stop reason, token usage, request id. Heavy SDKs
(anthropic) are imported lazily so importing this module costs nothing.
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(slots=True)
class ProviderResult:
    data: dict[str, Any]                          # the structured reply, parsed JSON
    provider: str = ""
    model: str = ""
    call: dict[str, Any] = field(default_factory=dict)   # stop_reason, usage, request_id


class ProviderError(RuntimeError):
    """A call that came back but cannot be used: cut off at max_tokens, declined, or not
    the JSON asked for. Carries what the call reported, so the error report records it."""

    def __init__(self, message: str, call: Optional[dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.call = call or {}


class LLMProvider(ABC):
    name: str = "base"
    model: str = ""

    @abstractmethod
    async def generate(self, prompt: str, *, system: Optional[str] = None,
                       schema: Optional[dict[str, Any]] = None,
                       effort: Optional[str] = None) -> ProviderResult:
        """Run one completion whose reply must match `schema` (a JSON schema)."""


class OllamaProvider(LLMProvider):
    """Local Ollama HTTP API: an unbuilt stub."""

    name = "ollama"

    def __init__(self, base_url: str, model: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model

    async def generate(self, prompt: str, *, system: Optional[str] = None,
                       schema: Optional[dict[str, Any]] = None,
                       effort: Optional[str] = None) -> ProviderResult:
        raise NotImplementedError("OllamaProvider.generate is not built.")


class ClaudeProvider(LLMProvider):
    """Anthropic Claude. The key is ANTHROPIC_API_KEY, which the operator enters on the Settings
    page (config/secrets.env, never committed).

    The reply is STRUCTURED OUTPUT (`output_config.format`, a JSON schema): its text is the
    JSON. This replaced forced tool use, which claude-opus-5-5 and claude-sonnet-5-5 reject
    with a 400. The call is STREAMED: thinking counts toward max_tokens,
    and the SDK refuses a long request that is not streamed. A reply cut off at max_tokens or
    declined is an error, never a silently short report; 6 of 31 "complete" reports once had
    no narrative and nothing said why. `client` is injectable for tests.
    """

    name = "claude"

    def __init__(self, model: str, api_key: Optional[str] = None, *, client: Any = None,
                 max_tokens: int = 32000, effort: Optional[str] = None) -> None:
        self.model = model
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.max_tokens = max_tokens
        self.effort = effort
        self._client = client

    def _get_client(self) -> Any:
        if self._client is None:
            if not self.api_key:
                raise RuntimeError(
                    "ANTHROPIC_API_KEY not set: enter the key on the Settings page.")
            import anthropic  # lazy: importing this module stays dependency-free
            # max_retries=0: reports._generate_with_retry is the one retry layer. The SDK's
            # own default (2) ran inside it, so one failure could be billed up to 9 times
            # (2026-11-06 review, M11).
            self._client = anthropic.AsyncAnthropic(api_key=self.api_key, max_retries=0)
        return self._client

    async def generate(self, prompt: str, *, system: Optional[str] = None,
                       schema: Optional[dict[str, Any]] = None,
                       effort: Optional[str] = None) -> ProviderResult:
        if schema is None:
            raise ValueError("ClaudeProvider expects a JSON schema (structured output).")
        client = self._get_client()
        effort = effort or self.effort
        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": schema}}
        if effort:
            output_config["effort"] = effort
        kw: dict[str, Any] = {"model": self.model, "max_tokens": self.max_tokens,
                              "output_config": output_config,
                              "messages": [{"role": "user", "content": prompt}]}
        if system:
            kw["system"] = system
        async with client.messages.stream(**kw) as stream:
            msg = await stream.get_final_message()
            request_id = getattr(stream, "request_id", None)
        call = call_meta(msg, request_id, effort)
        if call.get("stop_reason") == "max_tokens":
            raise ProviderError(f"the reply was cut off at max_tokens ({self.max_tokens}); "
                                "raise ai.providers.claude.max_tokens", call)
        if call.get("stop_reason") == "refusal":
            raise ProviderError("the model declined to write this report", call)
        text = "".join(getattr(b, "text", "") or "" for b in getattr(msg, "content", []) or []
                       if getattr(b, "type", None) == "text")
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise ProviderError(f"the reply was not the JSON asked for ({exc})", call) from None
        if not isinstance(data, dict):
            raise ProviderError("the reply was not a JSON object", call)
        return ProviderResult(data=data, provider=self.name,
                              model=call.get("model") or self.model, call=call)


def call_meta(msg: Any, request_id: Optional[str], effort: Optional[str]) -> dict[str, Any]:
    """What a finished call reported: stop reason, token usage, request id, model, effort."""
    u = getattr(msg, "usage", None)
    usage = {}
    for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
              "cache_creation_input_tokens"):
        v = getattr(u, k, None) if u is not None else None
        if isinstance(v, int):
            usage[k] = v
    return {"stop_reason": getattr(msg, "stop_reason", None),
            "request_id": request_id or getattr(msg, "_request_id", None),
            "model": getattr(msg, "model", None), "effort": effort, "usage": usage}


def get_provider(config: Any, report_kind: str) -> LLMProvider:
    """Route to a provider by report type per config.ai.routing.

    report_kind: 'daily' | 'weekly' | 'manual'. `effort` may be one level for every kind or
    a map ({daily: medium, weekly: high}); an unlisted kind takes the daily one.
    """
    ai = _get(config, "ai", default={}) or {}
    routing = _get(ai, "routing", default={}) or {}
    # unknown kinds (e.g. 'manual') fall back to the daily route, then a sane default
    provider_name = _get(routing, report_kind,
                         default=_get(routing, "daily", default="claude"))
    providers = _get(ai, "providers", default={}) or {}

    if provider_name == "ollama":
        cfg = _get(providers, "ollama", default={}) or {}
        return OllamaProvider(
            base_url=_get(cfg, "base_url", default="http://localhost:11434"),
            model=_get(cfg, "model", default="qwen2.5:14b"),
        )
    if provider_name == "claude":
        cfg = _get(providers, "claude", default={}) or {}
        model = _get(cfg, "model", default=None) or "claude-opus-5-5"
        if report_kind == "weekly":
            # `or`: a weekly_model_override of null means "the same model", not "no model"
            model = _get(ai, "weekly_model_override", default=None) or model
        try:
            max_tokens = int(_get(cfg, "max_tokens", default=32000) or 32000)
        except (TypeError, ValueError):
            max_tokens = 32000
        effort = _get(cfg, "effort", default=None)
        if effort is not None and not isinstance(effort, str):
            effort = _get(effort, report_kind, default=None) or _get(effort, "daily",
                                                                     default=None)
        return ClaudeProvider(model=model, max_tokens=max_tokens, effort=effort or None)

    raise ValueError(f"unknown AI provider {provider_name!r} for report_kind={report_kind!r}")


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    val = getattr(obj, key, default)
    return val
