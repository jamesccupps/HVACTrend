"""Report service: one daily or weekly summary, end to end.

    window -> summary.build_inputs (in a worker thread, own read-only connection)
           -> provider.generate (structured output: headline, summary, actions)
           -> summary.accept (an action must cite a detected item)
           -> store the report, its call record and its actions as findings

Provider-agnostic: takes any LLMProvider (real Claude, or a fake in tests). A provider
failure (no key, no credit, network, a cut-off reply) is recorded as an `error` report with
whatever the call returned. It never throws away the run or crashes the caller, and the
morning check-in says so (checkin.py), so a dead summary cannot go unnoticed for days as it
did in September.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any, Optional

from ..models import Finding, Report
from . import summary
from .provider import LLMProvider, get_provider

log = logging.getLogger("hvactrend.ai.reports")

# One report at a time, scheduled or by hand: two tabs pressing "generate", or a press during
# the morning run, were two billed calls (2026-11-06 review, M11).
REPORT_LOCK = asyncio.Lock()
# A real daily prompt is 12-15k characters. Far past that is a window or a database gone
# wrong, and a bill to match; refused before the call, never sent.
PROMPT_MAX = 200_000


async def generate_report(dal: Any, config: Any, kind: str, *,
                          now_utc: Optional[int] = None,
                          start: Optional[int] = None, end: Optional[int] = None,
                          provider: Optional[LLMProvider] = None) -> dict[str, Any]:
    now = now_utc if now_utc is not None else int(time.time())
    win_start, win_end = summary.window(kind, now, start, end)

    inputs = await _inputs(dal, config, kind, win_start, win_end)
    items = inputs["items"]
    prompt = summary.load_template(config, kind).replace("{context}", summary.render(inputs))
    if len(prompt) > PROMPT_MAX:
        raise ValueError(f"the {kind} prompt is {len(prompt):,} characters (the limit is "
                         f"{PROMPT_MAX:,}); not sent")
    system = summary.SYSTEM.format(kind="weekly" if kind == "weekly" else "daily",
                                   site=summary.site_label(config))
    prov = provider or get_provider(config, kind)
    inputs_json = summary.to_json({"items": dict(Counter(i["kind"] for i in items.values())),
                                   "prompt_chars": len(prompt)})

    try:
        result = await _generate_with_retry(prov, prompt, config, system=system,
                                            schema=summary.SCHEMA)
        if not str(result.data.get("summary") or "").strip():
            raise RuntimeError("the reply had no summary text")
    except Exception as exc:  # recorded, never a silent stub
        log.error("%s report provider failed: %s", kind, exc)
        call = dict(getattr(exc, "call", None) or {})
        call["error"] = type(exc).__name__
        report = Report(kind=kind, period_start_utc=win_start, period_end_utc=win_end,
                        created_utc=now, provider=getattr(prov, "name", None),
                        model=getattr(prov, "model", None),
                        prose=f"AI provider error: {exc}", status="error")
        rid = dal.insert_report(report, inputs_json=inputs_json, call_json=summary.to_json(call))
        return {"report_id": rid, "kind": kind, "status": "error", "error": str(exc),
                "period": [win_start, win_end], "items": len(items), "findings": [],
                "call": call}

    kept, dropped = summary.accept(result.data, items)
    text = summary.prose(result.data, kept, dropped)
    call = {**result.call, "kept_actions": len(kept), "dropped_actions": len(dropped)}
    if dropped:
        log.warning("%s report: %d action(s) cited no detected item and were dropped",
                    kind, len(dropped))
    report = Report(kind=kind, period_start_utc=win_start, period_end_utc=win_end,
                    created_utc=now, provider=result.provider or getattr(prov, "name", None),
                    model=result.model or getattr(prov, "model", None),
                    prose=text, status="complete")
    report_id = dal.insert_report(report, inputs_json=inputs_json,
                                  call_json=summary.to_json(call))

    code_to_sysid = {s["code"]: s["id"] for s in dal.list_systems()}
    stored = []
    seen_fps: set[str] = set()
    for a in kept:
        row = summary.finding_row(a, items)
        fp = row["fingerprint"]
        # two actions resting on the same items must not collapse into one ledger row
        if fp in seen_fps:
            fp = f"{fp}#{_summary_disc(row['summary'])}"
        seen_fps.add(fp)
        stored.append(dal.upsert_finding(Finding(
            summary=row["summary"] or "(no summary)", created_utc=now, report_id=report_id,
            system_id=code_to_sysid.get(row["system_code"]), fingerprint=fp,
            severity=row["severity"], timeframe=row["timeframe"],
            equipment=row["system_code"], metric=row["metric"], recommendation=None,
            evidence=row["evidence"], status="open", first_seen_utc=now, last_seen_utc=now)))
    resolved = _autoclose(dal, config, now)

    return {"report_id": report_id, "kind": kind, "status": "complete",
            "period": [win_start, win_end], "provider": report.provider,
            "model": report.model, "headline": str(result.data.get("headline") or ""),
            "prose": text, "items": len(items), "actions": kept, "dropped": len(dropped),
            "finding_ids": stored, "finding_count": len(stored),
            "resolved_count": len(resolved), "resolved": resolved, "call": call}


async def _inputs(dal: Any, config: Any, kind: str, start: int, end: int) -> dict:
    """In a worker thread on a read-only connection of its own when the database is a file
    (always, in service): reading a week of samples on the collector's loop would stall the
    BACnet polling (Phase 0.4). The collector's connection is bound to its thread."""
    path = getattr(dal, "db_path", None)
    if path is not None and Path(path).exists():
        return await asyncio.to_thread(summary.build_inputs_readonly, str(path), config,
                                       kind, start, end)
    return summary.build_inputs(dal, config, kind, start, end)


# ── provider retry (transient errors get a few backed-off attempts) ──────────
_RETRYABLE_NAMES = ("timeout", "connection", "ratelimit", "overloaded",
                    "apiconnection", "apitimeout", "internalserver",
                    "serviceunavailable", "remoteprotocol")


def _is_retryable(exc: Exception) -> bool:
    """A transient failure worth retrying (network blip, 429, 5xx) vs a permanent one
    (bad key, no credit, 4xx, a cut-off reply) that will just fail again."""
    sc = getattr(exc, "status_code", None)
    if sc is None:
        sc = getattr(exc, "status", None)
    if isinstance(sc, int):
        if sc in (408, 409, 425, 429) or 500 <= sc < 600:
            return True
        if 400 <= sc < 500:
            return False
    name = type(exc).__name__.lower()
    return any(k in name for k in _RETRYABLE_NAMES)


def _retry_cfg(config: Any) -> tuple[int, float]:
    attempts, backoff = 3, 2.0
    ai = _cfg(config, "ai", {}) or {}
    r = _cfg(ai, "retry", {}) or {}
    try:
        attempts = max(1, int(_cfg(r, "attempts", attempts)))
        backoff = float(_cfg(r, "backoff_s", backoff))
    except (TypeError, ValueError):
        pass
    return attempts, backoff


async def _generate_with_retry(prov: LLMProvider, prompt: str, config: Any, *,
                               system: Optional[str] = None,
                               schema: Optional[dict] = None):
    attempts, backoff = _retry_cfg(config)
    last: Optional[Exception] = None
    for i in range(attempts):
        try:
            return await prov.generate(prompt, system=system, schema=schema)
        except Exception as exc:  # noqa: BLE001 — classify, then re-raise or back off
            last = exc
            if i == attempts - 1 or not _is_retryable(exc):
                raise
            wait = backoff * (2 ** i)
            log.warning("provider attempt %d/%d failed (%s: %s); retrying in %.0fs",
                        i + 1, attempts, type(exc).__name__, exc, wait)
            await asyncio.sleep(wait)
    assert last is not None
    raise last


def _autoclose(dal: Any, config: Any, now: int) -> list[dict]:
    if not hasattr(dal, "age_out_findings"):
        return []
    ai = _cfg(config, "ai", {}) or {}
    fcfg = _cfg(ai, "findings", {}) or {}
    try:
        days = float(_cfg(fcfg, "autoclose_days", 3))
    except (TypeError, ValueError):
        days = 3.0
    if days <= 0:
        return []
    # Floor the window at the longest report cadence. A weekly-only finding must not be
    # auto-closed by an intervening daily run: with a 3-day window it would be "resolved"
    # before the next weekly run and then re-inserted fresh, losing its history.
    days = max(days, max(summary.WINDOWS.values()) / 86400.0)
    try:
        return dal.age_out_findings(int(now - days * 86400))
    except Exception as exc:
        log.warning("finding auto-close skipped: %s", exc)
        return []


def _fingerprint(system_code: Optional[str], metric: Optional[str],
                 summary_text: Optional[str]) -> str:
    """Stable dedupe/track key: system + metric (fallback to leading summary words).
    Normalized so the same issue matches across runs despite wording drift."""
    key = metric or summary_text or ""
    key = re.sub(r"[^a-z0-9 ]+", "", key.lower())
    key = " ".join(key.split()[:6])
    return f"{(system_code or '?').lower()}:{key}".strip(": ")


def _summary_disc(summary_text: Optional[str]) -> str:
    """Short discriminator from the full normalized summary: keeps two same-run findings
    that share a key from colliding, while staying stable across runs."""
    norm = " ".join(re.sub(r"[^a-z0-9 ]+", "", (summary_text or "").lower()).split())
    return hashlib.md5(norm.encode("utf-8")).hexdigest()[:6]


def _cfg(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)
