"""Deterministic building & unit health scoring.

Every report run scores each system 0-100 (letter grade A-F) and rolls the fleet up
to a building score — from the SAME deterministic pre-screen signals the LLM sees, so
the number is reproducible and fully explainable (each deduction names the anomaly
that cost the points). This is the layer that lets a report say "AC-8 held a C all
week" and the status tab trend a grade over time, without another model call.

Pure function of the built context (systems + anomalies) — no I/O, no LLM — so the
`/api/health` pulse can recompute it on demand between scheduled reports. Read-only.
"""

from __future__ import annotations

from typing import Any, Optional

# Points deducted per attributed anomaly, by severity. Tuned so a single CRITICAL
# (a dropped controller) alone drops a unit to failing, while a lone INFO barely
# moves it. The LLM findings are a richer second layer; health stays deterministic.
SEV_PENALTY = {"critical": 40.0, "high": 24.0, "medium": 10.0, "low": 4.0, "info": 1.0}

# score >= threshold -> grade (first match wins; evaluated high to low).
GRADE_BANDS = ((90.0, "A"), (80.0, "B"), (70.0, "C"), (60.0, "D"), (0.0, "F"))

# Systems that aren't real equipment (unmapped points land here) are scored but kept
# OUT of the building rollup so they don't skew the fleet grade.
_NON_EQUIP = frozenset({"other", "?", ""})

_MAX_DEDUCTIONS_SHOWN = 8


def grade_for(score: float) -> str:
    for threshold, letter in GRADE_BANDS:
        if score >= threshold:
            return letter
    return "F"


def _dedupe_anomalies(anoms: list[dict]) -> list[dict]:
    """Collapse anomalies that describe the same (point, kind, metric) to their most
    severe instance, so one flaky point flagged by several detectors doesn't stack
    penalties for what is really one problem."""
    best: dict[tuple, dict] = {}
    for a in anoms:
        key = (a.get("point_id"), a.get("kind"), a.get("metric"))
        cur = best.get(key)
        if cur is None or _rank(a.get("severity")) > _rank(cur.get("severity")):
            best[key] = a
    return list(best.values())


def _rank(sev: Optional[str]) -> int:
    order = ["info", "low", "medium", "high", "critical"]
    try:
        return order.index((sev or "info").lower())
    except ValueError:
        return 0


def score_systems(systems: list[dict], anomalies: list[dict]) -> dict[str, Any]:
    """Score every system + roll up the building.

    `systems`  : the context's per-system dicts ({code, name, kind, points:[...]}).
    `anomalies`: the context's anomaly dicts (severity as str, system_code, kind, ...).
    Returns {"building": {...}, "systems": [...]} — callers stamp/persist.
    """
    by_sys: dict[str, list[dict]] = {}
    for a in _dedupe_anomalies(anomalies):
        by_sys.setdefault(a.get("system_code") or "?", []).append(a)

    sys_rows: list[dict] = []
    for s in systems:
        code = s.get("code") or "?"
        anoms = sorted(by_sys.get(code, []),
                       key=lambda a: _rank(a.get("severity")), reverse=True)
        deductions = []
        total = 0.0
        for a in anoms:
            pen = SEV_PENALTY.get((a.get("severity") or "info").lower(), 1.0)
            total += pen
            deductions.append({
                "severity": a.get("severity"), "kind": a.get("kind"),
                "metric": a.get("metric"), "penalty": pen,
                "detail": a.get("detail"),
            })
        score = max(0.0, min(100.0, round(100.0 - total, 1)))
        sys_rows.append({
            "code": code, "name": s.get("name") or code, "kind": s.get("kind"),
            "score": score, "grade": grade_for(score),
            "anomaly_count": len(anoms),
            "worst_severity": anoms[0].get("severity") if anoms else None,
            "deductions": deductions[:_MAX_DEDUCTIONS_SHOWN],
            "point_count": len(s.get("points") or []),
        })

    sys_rows.sort(key=lambda r: (r["score"], r["code"]))   # worst first

    equip = [r for r in sys_rows if r["code"].lower() not in _NON_EQUIP]
    building = _rollup(equip)
    return {"building": building, "systems": sys_rows}


def _rollup(equip: list[dict]) -> dict[str, Any]:
    """Building health from the equipment units: the fleet mean (overall posture),
    plus the worst units and severity tallies so a single failing unit is never
    hidden behind a healthy average."""
    if not equip:
        return {"score": 100.0, "grade": "A", "n_systems": 0,
                "grades": {}, "worst": [], "critical_units": [], "note": "no equipment scored"}
    mean = round(sum(r["score"] for r in equip) / len(equip), 1)
    grades: dict[str, int] = {}
    for r in equip:
        grades[r["grade"]] = grades.get(r["grade"], 0) + 1
    worst = [{"code": r["code"], "score": r["score"], "grade": r["grade"]}
             for r in equip[:3]]
    critical_units = [r["code"] for r in equip if r.get("worst_severity") == "critical"]
    return {
        "score": mean, "grade": grade_for(mean), "n_systems": len(equip),
        "min_score": equip[0]["score"], "grades": grades,
        "worst": worst, "critical_units": critical_units,
    }


def score_context(ctx: dict[str, Any]) -> dict[str, Any]:
    """Convenience: score straight off a built context dict."""
    return score_systems(ctx.get("systems") or [], ctx.get("anomalies") or [])


def render_health(health: dict[str, Any]) -> list[str]:
    """Compact text block for the prompt — the grades the model should narrate."""
    b = health.get("building") or {}
    L = [f"## Plant health (deterministic, this window)",
         f"Building: {b.get('score')}/100 ({b.get('grade')}) across "
         f"{b.get('n_systems', 0)} units."]
    crit = b.get("critical_units") or []
    if crit:
        L.append(f"CRITICAL units: {', '.join(crit)}.")
    worst = b.get("worst") or []
    if worst:
        L.append("Lowest: " + ", ".join(f"{w['code']} {w['score']}({w['grade']})"
                                        for w in worst) + ".")
    L.append("Per-unit grades (worst first):")
    for r in health.get("systems") or []:
        tag = f" — {r['anomaly_count']} flagged" if r["anomaly_count"] else " — clean"
        L.append(f"  {r['code']}: {r['score']}/100 {r['grade']}{tag}")
    return L
