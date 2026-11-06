"""Check what a report SAYS against what the data SHOWS.

WHY THIS EXISTS
---------------
Three code reviews and a 436-agent adversarial sweep never once opened a generated
report and checked a claim against the database. They audited the machinery that
produces reports — prompt assembly, sanitization, tool-use handling, retry, findings
lifecycle — and every one of those was sound while the reports themselves said things
that were not true:

  · "OA enthalpy averaged 27.8 Btu/lb — just under the 28 Btu/lb changeover"
      The changeover is 19 here. Outdoor air was ABOVE it. Correct economizer lockout,
      reported as waste, in every report for months.
  · "AC-1 running as a fleet DAT outlier at 68.7F (+8.3 sigma vs 57.5F fleet mean)"
      AC-1's setpoint is 69.0. It was 0.3F off target — near-perfect control.
  · "Five AHUs (AC-3,4,5,6,8,9,10,12) exceeded their 70F limit during the early
     afternoon peak"
      Eight units are listed, not five. Every exceedance was 08:45-09:55 in the MORNING.

The operator caught all three. That is the failure this module exists to end: a wrong
claim should be caught by the program, on the way out, not by the person reading it.

WHAT IT CAN AND CANNOT DO
-------------------------
It verifies claims that are mechanically checkable — counts against their own lists,
unit codes that exist, numeric ranges against the series, cited limits against the
configured limits, and whether the inputs the report was computed from were even fresh.
It also flags UNANCHORED TIME LANGUAGE ("early afternoon", "hours 2-5") because a report
window spans two calendar days and such a phrase is unverifiable by construction — which
is the honest verdict, not a pass.

It cannot check reasoning. A report can pass every check here and still draw a foolish
conclusion from correct numbers. This narrows the gap; it does not close it.
"""

from __future__ import annotations

import functools
import re
import time
from typing import Any, Iterable, Optional

# metric keywords -> the role whose canonical point carries that measurement
_METRIC_ROLES: list[tuple[tuple[str, ...], str, str]] = [
    (("discharge air", "discharge-air", "supply air", " dat ", "dat "), "DAT", "F"),
    (("mixed air", "mixed-air", " mat ", "mat "), "MAT", "F"),
    (("return air", "return-air", " rat ", "rat "), "RAT", "F"),
    (("duct static", "static pressure", "duct press"), "DUCT_PRESS", "in wc"),
    (("space pressure", "building pressure"), "SPACE_PRESS", "in wc"),
    (("fan speed", "vfd", "supply fan"), "FAN_SPEED", "%"),
    (("oa damper", "oa/ra damper", "outside air damper", "outdoor air damper"),
     "OA_DAMPER", "%"),
]

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14,
}

# Time language that cannot be resolved to a day. A daily window spans two calendar
# days, so these are unverifiable by construction — and every one of them has already
# been wrong in a real report.
_VAGUE_TIME = [
    (r"\bearly afternoon\b", "early afternoon"),
    (r"\blate afternoon\b", "late afternoon"),
    (r"\bthis morning\b", "this morning"),
    (r"\byesterday\b", "yesterday"),
    (r"\bovernight\b", "overnight"),
    (r"\bhours?\s+\d+\s*[-–]\s*\d+\b", "bare hour index range"),
    (r"\bduring the (?:peak|afternoon|morning)\b", "unanchored period"),
]

# "64.7-70.4F", "64.7 °F to 70.4 °F", "0.06 to 1.2 in. w.c.": the summary is told to write
# °F and in. w.c. with every number, and the old pattern wanted the F or "in.wc" straight
# after the number, so only % ranges were ever checked (2026-11-06 review, M12).
_UNIT_TXT = r"°?\s*(?:F\b|degF\b|in\.?\s?w\.?\s?c\b\.?|%)"
_RANGE_RE = re.compile(
    r"(\d+(?:\.\d+)?)(?:\s*" + _UNIT_TXT + r")?\s*(?:-|–|to)\s*(\d+(?:\.\d+)?)\s*(" + _UNIT_TXT
    + ")", re.I)
_LIMIT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*°?\s*F?\s*(?:alarm[- ]high|high limit|alarm_hi)", re.I)


def _sentences(text: str) -> list[str]:
    # a full stop ends a sentence only before something that is not lower case: "in. w.c."
    # is one unit, not two sentences
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[^a-z])|\n+", text or "")
            if s.strip()]


@functools.lru_cache(maxsize=8)
def _unit_res(codes: frozenset) -> tuple:
    """(single, run) patterns for THIS plant's unit codes. They knew only AC-n and HVn, one
    building's naming: at a site of AHU-1 and RTU-2 the count and unknown-unit checks never
    fired (review M12). A code ending in a number lends its prefix to any number, so a unit
    that does not exist ("AC-99") is still seen, and then reported."""
    prefixes, plain = set(), set()
    for c in codes:
        m = re.match(r"^(.*[A-Za-z][^\d]*?)(\d{1,3})$", c)
        if m:
            prefixes.add(m.group(1))
        elif re.search(r"[A-Za-z]", c):
            plain.add(c)
    alts = ([re.escape(p) + r"\d{1,3}" for p in sorted(prefixes, key=len, reverse=True)]
            + [re.escape(c) for c in sorted(plain, key=len, reverse=True)])
    single = re.compile(r"\b(" + "|".join(alts) + r")\b", re.I) if alts else None
    # The compact fleet form the model actually writes: "AC-3,4,5,6,8,9,10,12" or
    # "AC-1/3/4/5". A naive \bAC-\d+\b finds only the first and reports "lists 1 unit",
    # which turns a correct FAIL into a misleading one.
    run = (re.compile(r"\b(" + "|".join(re.escape(p) for p in sorted(prefixes, key=len,
                                                                    reverse=True))
                      + r")(\d{1,3})((?:\s*[,/]\s*\d{1,3})+)", re.I) if prefixes else None)
    return single, run


def units_in(s: str, codes: Iterable[str]) -> list[str]:
    """Every unit code a sentence names, including the compact run form."""
    single, run = _unit_res(frozenset(str(c).upper() for c in codes))
    found: list[str] = []
    if run:
        for m in run.finditer(s):
            head = m.group(1).upper()
            found.append(f"{head}{m.group(2)}")        # as written: AHU-01 is a code too
            for n in re.findall(r"\d{1,3}", m.group(3)):
                found.append(f"{head}{n}")
    if single:
        for u in single.findall(s):
            found.append(u.upper())
    return list(dict.fromkeys(found))


def _role_for(sentence: str) -> Optional[tuple[str, str]]:
    low = f" {sentence.lower()} "
    for keys, role, unit in _METRIC_ROLES:
        if any(k in low for k in keys):
            return role, unit
    return None


def _canon(dal: Any, code: str, role: str) -> Optional[int]:
    rows = dal._conn.execute(
        "SELECT p.id FROM points p JOIN systems s ON s.id=p.system_id "
        "WHERE s.code=? AND p.role=? AND p.role_canonical=1 AND p.enabled=1",
        (code.upper(), role)).fetchone()
    return rows[0] if rows else None


def _extent(dal: Any, pid: int, start: int, end: int) -> Optional[tuple]:
    r = dal._conn.execute(
        "SELECT MIN(v_min), MAX(v_max), AVG(v_avg), COUNT(*) FROM samples_5m "
        "WHERE point_id=? AND bucket_utc BETWEEN ? AND ?", (pid, start, end)).fetchone()
    return r if r and r[3] else None


def _check(name: str, status: str, claim: str, detail: str, **extra) -> dict:
    out = {"check": name, "status": status, "claim": claim.strip()[:240], "detail": detail}
    out.update(extra)
    return out


def verify_report(dal: Any, report_id: int, config: Any = None) -> dict:
    """Verify one stored report's claims against the data it was built from. `config` says
    which inputs there were to be stale (the baselines, the weather); without it those
    checks are left out."""
    row = dal._conn.execute(
        "SELECT id, kind, period_start_utc, period_end_utc, prose, created_utc "
        "FROM reports WHERE id=?", (report_id,)).fetchone()
    if not row:
        return {"report_id": report_id, "error": "no such report"}
    rid, kind, start, end, prose, created = (row[0], row[1], row[2], row[3], row[4],
                                             row[5])
    findings = [dict(zip(("id", "summary", "recommendation", "severity"), r))
                for r in dal._conn.execute(
                    "SELECT id, summary, recommendation, severity FROM findings "
                    "WHERE report_id=?", (rid,))]
    known = {r[0].upper() for r in dal._conn.execute("SELECT code FROM systems")}

    checks: list[dict] = []
    text_blocks = [("prose", prose or "")] + [
        (f"finding#{f['id']}", f"{f.get('summary') or ''} {f.get('recommendation') or ''}")
        for f in findings]

    for origin, text in text_blocks:
        for s in _sentences(text):
            checks.extend(_verify_sentence(dal, s, origin, start, end, known))

    checks.extend(_verify_inputs_were_fresh(dal, rid, end, config))

    fails =[c for c in checks if c["status"] == "fail"]
    warns = [c for c in checks if c["status"] == "warn"]
    return {
        "report_id": rid, "kind": kind,
        "window": [start, end],
        "window_local": [time.strftime("%a %b %d %H:%M", time.localtime(start)),
                         time.strftime("%a %b %d %H:%M", time.localtime(end))],
        "spans_days": time.strftime("%j", time.localtime(start))
                      != time.strftime("%j", time.localtime(end)),
        "checked": len(checks), "failed": len(fails), "warned": len(warns),
        "verdict": "FAIL" if fails else ("WARN" if warns else "PASS"),
        "checks": checks,
    }


def _verify_sentence(dal: Any, s: str, origin: str, start: int, end: int,
                     known: set) -> list[dict]:
    out: list[dict] = []
    units = units_in(s, known)

    # 1) a stated count must match the list it introduces. "Five AHUs (AC-3,4,5,6,8,
    #    9,10,12)" lists eight. This is pure arithmetic and the model gets it wrong.
    m = re.search(r"\b(" + "|".join(_NUMBER_WORDS) + r"|\d{1,2})\+?\s+"
                  r"(?:AHUs?|units?|systems?)\b", s, re.I)
    if m and units:
        word = m.group(1).lower()
        stated = _NUMBER_WORDS.get(word, int(word) if word.isdigit() else None)
        listed = len(dict.fromkeys(units))          # de-duped, order-preserving
        plus = "+" in m.group(0)
        if stated is not None and listed and not plus and stated != listed:
            out.append(_check(
                "count_vs_list", "fail", s,
                f"says {stated} but lists {listed} units ({', '.join(dict.fromkeys(units))})",
                origin=origin, stated=stated, listed=listed))

    # 2) every unit code cited must exist
    for u in dict.fromkeys(units):
        if u not in known:
            out.append(_check("unknown_unit", "fail", s,
                              f"cites {u}, which is not a system in this plant",
                              origin=origin, unit=u))

    # 3) unanchored time language — unverifiable by construction on a 2-day window
    for pat, label in _VAGUE_TIME:
        if re.search(pat, s, re.I):
            out.append(_check(
                "unanchored_time", "warn", s,
                f"'{label}' cannot be resolved to a day; the window spans two calendar "
                f"days. Cite the local day and hour instead.", origin=origin,
                phrase=label))
            break

    # 4) a numeric range attributed to ONE unit and ONE metric must be consistent with
    #    that series. Two rules learned by running this against a real report, where a
    #    naive version produced four false FAILs out of six:
    #
    #    (a) ATTRIBUTION. Only check a sentence naming exactly one unit and one metric
    #        family. "Five AHUs (AC-3,4,5,6,8,9,10,12) … ranging 70.5-80.2F" is a range
    #        across EIGHT units; charging it to AC-3 alone invents a failure. Likewise a
    #        sentence describing fans AND duct static AND discharge temp gives no way to
    #        know which one a number belongs to.
    #    (b) CONTAINMENT, not equality. A claim usually describes an EXCURSION ("cycled
    #        from 0% to 63.5-72.8%"), not the window's min/max. Requiring the claim to
    #        EQUAL the extent flagged a true statement as false. The right test is that
    #        the cited values are consistent with what the data contains — a claim of
    #        85F on a point that never passed 70 still fails, which is the case that
    #        matters.
    rng = _RANGE_RE.search(s)
    role_unit = _role_for(s)
    metric_families = sum(1 for keys, _r, _u in _METRIC_ROLES
                          if any(k in f" {s.lower()} " for k in keys))
    if rng and role_unit and len(units) == 1 and metric_families == 1:
        role, unit_txt = role_unit
        code = units[0]
        pid = _canon(dal, code, role)
        if pid:
            ext = _extent(dal, pid, start, end)
            if ext:
                lo_c, hi_c = float(rng.group(1)), float(rng.group(2))
                lo_a, hi_a = ext[0], ext[1]
                tol = max(0.6, abs(hi_a - lo_a) * 0.06)   # prose rounding + bucketing
                ok = (lo_c >= lo_a - tol and hi_c <= hi_a + tol)
                out.append(_check(
                    "numeric_range", "pass" if ok else "fail", s,
                    f"{code} {role} cites {lo_c}-{hi_c}{unit_txt}; the point ranged "
                    f"{round(lo_a,1)}-{round(hi_a,1)} over the window"
                    + ("" if ok else f" — cited values fall OUTSIDE the observed data "
                                     f"(tolerance {tol:.1f})"),
                    origin=origin, unit=code, role=role,
                    claimed=[lo_c, hi_c], actual=[round(lo_a, 1), round(hi_a, 1)]))

    # 5) a cited alarm limit must match what is configured on that unit
    lim = _LIMIT_RE.search(s)
    if lim and role_unit and len(dict.fromkeys(units)) == 1:
        role, _u = role_unit
        pid = _canon(dal, units[0], role)
        if pid:
            r = dal._conn.execute("SELECT alarm_hi FROM points WHERE id=?", (pid,)).fetchone()
            if r and r[0] is not None:
                claimed, actual = float(lim.group(1)), float(r[0])
                ok = abs(claimed - actual) <= 0.6
                out.append(_check(
                    "cited_limit", "pass" if ok else "fail", s,
                    f"{units[0]} {role} alarm-high cited {claimed}, configured {actual}",
                    origin=origin, unit=units[0], claimed=claimed, actual=actual))
    return out


def input_freshness_now(dal: Any, end_utc: int) -> dict:
    """Age of the inputs a report is ABOUT to be computed from. Called at generation
    time and stored, because it cannot be reconstructed afterwards — once the nightly
    rebuild runs, the old `baseline.updated_utc` is gone and a later check reads the
    NEW age (it produced a nonsensical -0.2 h on the first run of this module)."""
    out: dict = {}
    b = dal._conn.execute("SELECT MAX(updated_utc) FROM baseline").fetchone()
    if b and b[0]:
        out["baseline_age_h"] = round((time.time() - b[0]) / 3600.0, 1)
    w = dal._conn.execute("SELECT MAX(ts_utc) FROM weather WHERE ts_utc<=?",
                          (end_utc,)).fetchone()
    out["weather_age_h"] = (round((end_utc - w[0]) / 3600.0, 1)
                            if w and w[0] else None)
    return out


def _weather_on(config: Any) -> bool:
    w = config.get("weather") if config else None
    w = w.as_dict() if hasattr(w, "as_dict") else (w or {})
    if not float(w.get("refresh_interval_s", 3600) or 0):
        return False
    return any(bool((w.get(src) or {}).get("enabled")) for src in ("open_meteo", "tempest"))


def _verify_inputs_were_fresh(dal: Any, report_id: int, end: int,
                              config: Any = None) -> list[dict]:
    """Was the report computed from CURRENT inputs? A report against 49-hour-old
    baselines is wrong in a way no sentence-level check can see: every sigma, every
    'vs normal' and every drift projection is measured against stale normals. That
    happened — the model rebuild was silently deferred a full day after every reboot —
    and nothing in the report said so.

    Reads the freshness RECORDED AT GENERATION TIME. Reports written before that was
    recorded report 'unknown' rather than a number computed from today's state, which
    would be measuring the wrong thing and calling it evidence.

    Each input is checked only when the program has it. With `ai.baselines` off (the
    default) nothing reads baselines, yet every report was warned "predates tracking", so
    scripts/verify_report.py could never exit 0; and a plant with no weather source failed
    every report for having no weather (2026-11-06 review, M12)."""
    from .model import baselines_on
    out: list[dict] = []
    if baselines_on(config):
        rec = {}
        try:
            row = dal._conn.execute(
                "SELECT inputs_json FROM reports WHERE id=?", (report_id,)).fetchone()
            if row and row[0]:
                import json
                rec = json.loads(row[0])
        except (Exception,):          # column absent on a DB predating the migration
            rec = {}
        age_h = rec.get("baseline_age_h")
        if age_h is None:
            out.append(_check(
                "baseline_freshness", "warn", "(report inputs)",
                "input freshness was not recorded for this report (predates tracking) — "
                "cannot confirm the learned baselines were current when it ran"))
        else:
            out.append(_check(
                "baseline_freshness", "fail" if age_h > 36 else
                ("warn" if age_h > 26 else "pass"),
                "(report inputs)",
                f"learned baselines were {age_h:.1f} h old when this report ran "
                f"(rebuild interval is daily; every sigma is measured against these)",
                age_hours=age_h))
    if not _weather_on(config):
        return out
    w = dal._conn.execute(
        "SELECT MAX(ts_utc) FROM weather WHERE ts_utc<=?", (end,)).fetchone()
    if w and w[0]:
        age_h = (end - w[0]) / 3600.0
        out.append(_check(
            "weather_freshness", "fail" if age_h > 6 else
            ("warn" if age_h > 2 else "pass"), "(report inputs)",
            f"newest weather row was {age_h:.1f} h before the window end "
            f"(the summary's weather line reads this)", age_hours=round(age_h, 1)))
    else:
        out.append(_check("weather_freshness", "fail", "(report inputs)",
                          "NO weather data in or before this window — the summary had no "
                          "weather to go on"))
    return out
