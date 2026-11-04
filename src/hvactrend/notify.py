"""Alert delivery to the operator's phone (ntfy), and the daily check-in.

HOW WE GOT HERE. Teams was the first choice (2026-09-29), but a Teams Workflows webhook needs
a premium licence on this tenant. The operator then chose **ntfy**: a free, open-source push service.
The ntfy phone app subscribes to a *topic*, and anything published to that topic arrives as a
notification. No account and no cost.

THE TOPIC IS THE SECRET. ntfy topics have no password: anyone who knows the name can read
these notifications and post fake ones. So the topic is long and random (~100 bits, made in
the browser on the Settings page), stored in config/secrets.env like the API keys, shown in
full only once (when it is generated), and never logged, returned by the API or put in an
error message. Every detail that leaves this module passes through _redact().

WHO SENDS. Only the watchdog (scripts/watchdog.py: a separate SYSTEM task every five minutes
that reads the database directly) and the Settings page's "Send test message". The server
never sends on its own, so a dead or hung server cannot also silence the report that it is
dead.

WHAT IS SENT
  * New watchdog alerts: data stopped (critical), a controller not responding, poll cycles
    failing, points not answering on a controller that still does (since 2026-10-14). Only source='watchdog'. The switched-off pre-screen's rows are not news: two of
    them sat open from 2026-09-15 and would otherwise have gone out as new. A failing poll
    cycle is held back until it is still failing on the next run -- one bad cycle that
    recovers by itself is not worth a phone buzz.
  * One all-clear for every alert that was sent, when it resolves.
  * Equipment incidents from the alarm rules (run_incidents): CRITICAL as urgent, ACTION as a
    normal notification, again when one gets worse, and a quiet all-clear. Switched on
    2026-10-02 (rules.push_critical / rules.push_action).
  * A reminder once a day (run_reminders, at alerts.reminder_time): one push listing every one
    of those still on after a day. 2026-10-26, after AC-2's and AC-8's Multistack
    alarms had been on for 12 days with one push on the first: "important alarms should have
    reminders about it".
  * The daily check-in (checkin.py), at the first run after alerts.checkin_time.

WHERE IT GOES. https://ntfy.sh by default (alerts.ntfy_server). Messages pass through that
free service, which keeps them briefly and has no uptime guarantee; the daily check-in is
what tells the operator that delivery itself has stopped. Self-hosting ntfy is a config change.

Delivery state is durable (app_meta, keys 'notify.*') because the watchdog is a fresh process
every five minutes: "already sent today" cannot live in memory.
"""

from __future__ import annotations

import logging
import os
import re
import socket
import time
from dataclasses import dataclass
from typing import Any, Optional, Sequence

log = logging.getLogger("hvactrend.notify")

SECRET_NAME = "ntfy_topic"               # the Settings-page name (secrets_store.SECRET_ENV)
ENV_VAR = "HVACTREND_NTFY_TOPIC"
SOURCE = "watchdog"                      # the only alerts that are sent
_META = "notify."
_MAX_BYTES = 3900                        # ntfy.sh turns anything over 4,096 into an attachment

# ntfy allows 1-64 of these characters. A topic this program generates is 28; demanding 12
# refuses the short, guessable names a person would pick, which would be readable by anyone.
_TOPIC_RE = re.compile(r"^[-_A-Za-z0-9]{12,64}$")

_DEFAULTS = {"checkin": True, "checkin_time": "07:00", "poll_failed_hold_s": 240.0,
             "ntfy_server": "https://ntfy.sh", "reminders": True, "reminder_time": "12:00"}


def _cfg(config: Any) -> dict:
    out = dict(_DEFAULTS)
    try:
        node = config.get("alerts") if config is not None else None
        if node:
            d = node.as_dict() if hasattr(node, "as_dict") else dict(node)
            out["checkin"] = bool(d.get("checkin", out["checkin"]))
            out["checkin_time"] = str(d.get("checkin_time", out["checkin_time"]))
            out["poll_failed_hold_s"] = float(d.get("poll_failed_hold_s",
                                                    out["poll_failed_hold_s"]))
            out["ntfy_server"] = str(d.get("ntfy_server") or out["ntfy_server"])
            out["reminders"] = bool(d.get("reminders", out["reminders"]))
            out["reminder_time"] = str(d.get("reminder_time", out["reminder_time"]))
    except (AttributeError, TypeError, ValueError):
        pass
    return out


def target(config: Any = None) -> Optional[dict]:
    """Where to deliver, or None when no topic is set -- and then nothing is ever sent."""
    topic = (os.environ.get(ENV_VAR) or "").strip()
    if not topic:
        return None
    return {"server": _cfg(config)["ntfy_server"].rstrip("/"), "topic": topic}


# ── the message ──────────────────────────────────────────────────────────────────────
@dataclass
class Message:
    """One notification, independent of the channel. Plain text: a phone notification
    renders it as written, and a later channel (email) can render the same fields."""
    title: str
    priority: int = 3                    # ntfy: 1 min .. 5 max ("urgent")
    lines: Sequence[str] = ()
    facts: Sequence["tuple[str, str]"] = ()
    footer: str = ""

    def text(self) -> str:
        parts = list(self.lines)
        if self.facts:
            if parts:
                parts.append("")
            parts += [f"{k}: {v}" for k, v in self.facts]
        if self.footer:
            parts += ["", self.footer]
        s = "\n".join(parts) or self.title
        return s.encode("utf-8")[:_MAX_BYTES].decode("utf-8", "ignore")


# ── transport ────────────────────────────────────────────────────────────────────────
_URL_RE = re.compile(r"https?://[^\s'\"<>]+", re.I)


def _redact(text: Any, *secrets: Optional[str]) -> str:
    """Make a detail string safe to log, store and show: no topic, no URL."""
    s = str(text or "")
    for sec in secrets:
        if sec:
            s = s.replace(sec, "<topic>")
    s = _URL_RE.sub("<url>", s)
    return " ".join(s.split())[:300]


_HINTS = {
    400: " (ntfy rejected the message)",
    403: " (the topic is reserved by someone else: generate a new one)",
    413: " (the message was too large)",
    429: " (rate-limited by ntfy; will retry)",
}


def post(tgt: dict, msg: Message, *, timeout: float = 10.0) -> "tuple[bool, str]":
    """Publish one message. Returns (ok, detail); the detail is already safe to log and show.

    JSON publishing to the server root, not a POST to /<topic> with headers: HTTP headers
    cannot carry an emoji or any non-Latin-1 title, and it keeps the topic out of the URL,
    which is what exception texts echo."""
    server, topic = str(tgt.get("server") or ""), str(tgt.get("topic") or "")
    if not server.lower().startswith("https://"):
        return False, "the ntfy server must be an https:// URL"
    if not _TOPIC_RE.match(topic):
        return False, "the ntfy topic must be 12-64 letters, digits, '-' or '_'"
    body = {"topic": topic, "title": msg.title, "message": msg.text(),
            "priority": max(1, min(5, int(msg.priority)))}
    try:
        import httpx  # lazy: keeps this module importable without the dependency
        r = httpx.post(server.rstrip("/") + "/", json=body, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 -- delivery must never raise into the watchdog
        return False, _redact(f"{type(exc).__name__}: {exc}", topic)
    if 200 <= r.status_code < 300:
        return True, f"HTTP {r.status_code}"
    detail = _redact(r.text[:200], topic)
    return False, f"HTTP {r.status_code}{_HINTS.get(r.status_code, '')}" + (
        f": {detail}" if detail else "")


def record(dal: Any, ok: bool, detail: str, now_utc: int, *, kind: str) -> None:
    """Remember the outcome, for the Settings page and the next run."""
    if ok:
        dal.set_meta(_META + "last_ok_utc", int(now_utc))
        dal.set_meta(_META + "last_ok_kind", kind)
    else:
        dal.set_meta(_META + "last_error_utc", int(now_utc))
        dal.set_meta(_META + "last_error", f"{kind}: {detail}")
        log.warning("alert delivery failed (%s): %s", kind, detail)   # detail is redacted


def send(dal: Any, msg: Message, now_utc: int, *, tgt: dict, kind: str) -> "tuple[bool, str]":
    ok, detail = post(tgt, msg)
    record(dal, ok, detail, now_utc, kind=kind)
    return ok, detail


def status(dal: Any, config: Any = None) -> dict:
    """Delivery state for the Settings page. Never includes the topic."""
    m = dal.meta_with_prefix(_META)

    def _int(k: str) -> Optional[int]:
        v = m.get(_META + k)
        try:
            return int(v) if v is not None else None
        except ValueError:
            return None
    c = _cfg(config)
    return {"channel": "ntfy", "server": c["ntfy_server"],
            "configured": target(config) is not None,
            "last_ok_utc": _int("last_ok_utc"), "last_ok_kind": m.get(_META + "last_ok_kind"),
            "last_error_utc": _int("last_error_utc"), "last_error": m.get(_META + "last_error"),
            "checkin_date": m.get(_META + "checkin_date"),
            "checkin": c["checkin"], "checkin_time": c["checkin_time"],
            "reminder_date": m.get(_REMINDER + "date"), "reminder_count": _int("reminder.count"),
            "reminders": c["reminders"], "reminder_time": c["reminder_time"]}


# ── what the messages say ────────────────────────────────────────────────────────────
def _stamp(now_utc: int) -> str:
    return time.strftime("%a %d %b %H:%M", time.localtime(now_utc)) + " · " + socket.gethostname()


def _dur(s: float) -> str:
    s = max(0, int(s))
    if s < 3600:
        return f"{max(1, round(s / 60))} min"
    h, m = divmod(round(s / 60), 60)
    return f"{h} h {m} min" if h < 48 else f"{round(s / 86400)} days"


_ICON = {"critical": "\U0001f534", "high": "\U0001f7e0"}          # red / orange circle
_OPEN = {"collector-stale": "Data has stopped",
         "device-stale": "{dev} is not responding",
         "device-poll-failed": "{dev}: poll cycles failing",
         "device-points-missing": "{dev}: some points not answering"}
_CLEAR = {"collector-stale": "Data is flowing again",
          "device-stale": "{dev} is responding again",
          "device-poll-failed": "{dev} is polling normally again",
          "device-points-missing": "{dev}: every point answering again"}


def _headline(a: dict, names: dict, table: dict) -> str:
    code = a.get("system_code")
    dev = names.get(str(code)) or (f"device {code}" if code else "a controller")
    tpl = table.get(a.get("kind"))
    return tpl.format(dev=dev) if tpl else str(a.get("message") or a.get("kind"))


def alerts_message(new: Sequence[dict], cleared: Sequence[dict], now_utc: int,
                   names: dict) -> Message:
    worst = "critical" if any(a["severity"] == "critical" for a in new) else "high"
    if new and len(new) + len(cleared) == 1:        # one alarm: headline in the title
        a = new[0]
        return Message(f"{_ICON.get(worst)} HVACTrend: {_headline(a, names, _OPEN)}",
                       priority=5 if worst == "critical" else 4,
                       lines=[str(a["message"])], footer=_stamp(now_utc))
    lines = []
    for a in new:
        lines.append(f"{_ICON.get(a['severity'], _ICON['high'])} {_headline(a, names, _OPEN)}"
                     f" — {a['message']}")
    for a in cleared:
        took = int(a["resolved_utc"]) - int(a["created_utc"])
        lines.append(f"✅ {_headline(a, names, _CLEAR)} — the alert was open "
                     f"{_dur(took)}, from "
                     f"{time.strftime('%H:%M', time.localtime(int(a['created_utc'])))}.")
    if new:
        title = f"{_ICON.get(worst)} HVACTrend: {len(new)} new alert" + (
            "s" if len(new) > 1 else "") + (f", {len(cleared)} cleared" if cleared else "")
        priority = 5 if worst == "critical" else 4
    elif len(cleared) == 1:
        title = f"✅ HVACTrend: {_headline(cleared[0], names, _CLEAR)}"
        priority = 3
    else:
        title = f"✅ HVACTrend: {len(cleared)} alerts cleared"
        priority = 3
    return Message(title, priority=priority, lines=lines, footer=_stamp(now_utc))


def checkin_message(s: dict) -> Message:
    n = len(s["attention"])
    title, priority = {
        # A normal morning is deliberately quiet (priority 2: no sound). Its job is to be
        # THERE; the absence is the signal, and a daily buzz would train it to be ignored.
        "ok": ("✅ HVACTrend check-in: all normal", 2),
        "attention": (f"⚠️ HVACTrend check-in: {n} thing"
                      f"{'s' if n != 1 else ''} to look at", 4),
        "critical": ("\U0001f534 HVACTrend check-in: data has stopped", 5),
    }[s["state"]]
    return Message(title, priority=priority, lines=[f"• {x}" for x in s["attention"]],
                   facts=s["facts"],
                   footer=f"Yesterday, {s['day']} · sent {_stamp(s['built_utc'])}")


def test_message(now_utc: int) -> Message:
    return Message("✅ HVACTrend test message", priority=3,
                   lines=["Alerts and the daily check-in will arrive here."],
                   footer=_stamp(now_utc) + " · sent from the Settings page")


# ── policy: what the watchdog sends, and when ───────────────────────────────────────
def _local_date(now_utc: int) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(now_utc))


def _due(dal: Any, now_utc: int, at: str, key: str, fallback: "tuple[int, int]") -> bool:
    """True from `at` (local) until today's date is stored under `key`."""
    lt = time.localtime(now_utc)
    try:
        hh, mm = (int(x) for x in str(at).split(":", 1))
    except ValueError:
        hh, mm = fallback
    if (lt.tm_hour, lt.tm_min) < (hh, mm):
        return False
    return dal.get_meta(key) != _local_date(now_utc)


def checkin_due(dal: Any, now_utc: int, at: str = "07:00") -> bool:
    """True from `at` (local) until today's check-in has been delivered."""
    return _due(dal, now_utc, at, _META + "checkin_date", (7, 0))


def _device_names(dal: Any) -> dict:
    try:
        return {str(d["device_instance"]): d.get("name") or f"device {d['device_instance']}"
                for d in dal.poll_health_latest()}
    except Exception:  # noqa: BLE001 -- a missing name must not cost the alert
        return {}


def _push_tiers(config: Any) -> set:
    """The incident tiers that go to the phone as they open: rules.push_critical and
    rules.push_action. Both on since 2026-10-02: AC-6 lost all cooling for three hours that
    day and nothing reached the operator's phone, because these were built switched off until they had
    seen the 30-day replay."""
    node = config.get("rules") if config else None
    node = node.as_dict() if hasattr(node, "as_dict") else (node or {})
    if not node.get("enabled", True):
        # Rules off: nothing opens or closes, so nothing is news. The daily reminder used to
        # go on listing the frozen incidents, "still on" for ever (2026-10-27 review).
        return set()
    return {tier for tier, key in (("critical", "push_critical"), ("action", "push_action"))
            if node.get(key, False)}


_INCIDENT_ICON = {"critical": "\U0001f534", "action": "\U0001f7e0"}    # red / orange circle
_ACTION_AFTER = _META + "incident_action_after_utc"
_GONE_AFTER = _META + "incident_gone_after_utc"


def _overrides_by_unit(dal: Any, config: Any = None) -> dict:
    """Unit code -> its overrides, described (bacnet/priority.py); {} when unknown. The operator,
    2026-10-06: an alarm on overridden equipment still goes out, and names the override.
    One held on purpose (site_knowledge.expected_overrides) is not news, so is not named."""
    try:
        if not dal.has_table("point_command"):
            return {}
        from . import coverage
        from .bacnet import priority
        facts = coverage.site_facts(config.get("site_knowledge") if config else None)
        out: dict = {}
        for r in priority.mark_expected(dal.overrides(), facts):
            if r.get("system_code") and not r["expected"]:
                out.setdefault(r["system_code"], []).append(priority.describe(r))
        return out
    except Exception:  # noqa: BLE001 -- naming an override must never cost the alarm
        return {}


def incident_name(r: dict) -> str:
    """rules/catalog.name(), imported HERE and not at the top. The watchdog imports this module
    before its fence, so the top-level import that came in with the names (10-26) let a broken
    rules package stop the watchdog loading at all: no "data has stopped" alert, no pushes, no
    check-in (2026-10-27 review). Without the catalog the rule's own title still reads."""
    try:
        from .rules import catalog
    except Exception:  # noqa: BLE001 -- any failure to import, a SyntaxError included
        return str(r.get("title") or r.get("rule") or "incident")
    return catalog.name(r)


def incident_message(rows: Sequence[dict], now_utc: int, *, cleared: bool = False,
                     overrides: Optional[dict] = None) -> Message:
    """One message for incidents that belong together (one group, or one unit). CRITICAL is
    urgent (priority 5), ACTION a normal notification (3). An all-clear is quiet (2: no
    sound), except the end of a critical one (3)."""
    units = sorted({r["system_code"] for r in rows})
    first = rows[0]
    critical = any(r["tier"] == "critical" for r in rows)
    if cleared:
        title = (f"\u2705 HVACTrend: {first['system_code']}: {incident_name(first)} cleared"
                 if len(rows) == 1 else f"\u2705 HVACTrend: {len(rows)} equipment alarms cleared")
        return Message(title, priority=3 if critical else 2,
                       lines=[f"\u2022 {r['system_code']}: {incident_name(r)}" for r in rows[:10]],
                       footer=_stamp(now_utc))
    icon = _INCIDENT_ICON["critical" if critical else "action"]
    if len(units) >= 3:
        title = f"{icon} HVACTrend: {len(units)} units at once"
    else:
        title = f"{icon} HVACTrend: {first['system_code']}: {incident_name(first)}"
    # a row pushed before (at a lower tier) is here because it got worse: say so
    lines = [f"\u2022 {'Now ' + r['tier'] + ': ' if r.get('notified_tier') else ''}"
             f"{r.get('detail') or r['title']}" for r in rows[:10]]
    if len(units) >= 3:
        lines.append("All together points at something they share (the condenser water loop, "
                     "power, a controller) before the units themselves.")
    over = [o for u in units for o in (overrides or {}).get(u, [])]
    if over:
        lines.append("Overridden now: " + "; ".join(over[:5])
                     + (f"; and {len(over) - 5} more" if len(over) > 5 else ""))
    return Message(title, priority=5 if critical else 3, lines=lines, footer=_stamp(now_utc))


def gone_message(rows: Sequence[dict], now_utc: int) -> Message:
    """Incidents that began AND ended before the rules next ran, so were never sent while on:
    said once, afterwards, and needing no all-clear. High (4) with a CRITICAL one rather than
    urgent: it is over."""
    critical = any(r["tier"] == "critical" for r in rows)
    icon = _INCIDENT_ICON["critical" if critical else "action"]
    first = rows[0]
    title = (f"{icon} HVACTrend: {first['system_code']}: {incident_name(first)} came and went"
             if len(rows) == 1 else f"{icon} HVACTrend: {len(rows)} equipment alarms came and went")
    lines = []
    for r in rows[:10]:
        a = int(r["started_utc"])
        b = int(r.get("resolved_utc") or a)
        lines.append(f"\u2022 {'CRITICAL: ' if r['tier'] == 'critical' else ''}"
                     f"{r['system_code']}: {incident_name(r)}, "
                     f"{time.strftime('%a %d %b %H:%M', time.localtime(a))} for {_dur(b - a)}"
                     + ("" if (r.get("resolve_reason") or "cleared") == "cleared"
                        else ", then nothing to judge it by"))
    if len(rows) > 10:
        lines.append(f"\u2022 and {len(rows) - 10} more")
    return Message(title, priority=4 if critical else 3, lines=lines,
                   footer="It was over before the alarm rules next ran, so it was not sent "
                          "while on. " + _stamp(now_utc))


def unjudged_message(rows: Sequence[dict], now_utc: int) -> Message:
    """Pushed incidents that closed because nothing could judge them for IDLE_CLOSE_S (a point
    switched off or not answering, a unit set vacant), not because they cleared. They used to
    go out as "\u2705 ... cleared" (2026-10-27 review): a false all-clear, and if the fault was
    still there, a duplicate alarm when the readings came back."""
    critical = any(r["tier"] == "critical" for r in rows)
    first = rows[0]
    title = (f"\u2754 HVACTrend: {first['system_code']}: {incident_name(first)} closed, "
             "NOT cleared" if len(rows) == 1
             else f"\u2754 HVACTrend: {len(rows)} equipment alarms closed, NOT cleared")
    lines = [f"\u2022 {r['system_code']}: {incident_name(r)}: nothing to judge it by since "
             f"{time.strftime('%a %d %b %H:%M', time.localtime(int(r.get('resolved_utc') or now_utc)))}"
             for r in rows[:10]]
    return Message(title, priority=3 if critical else 2, lines=lines,
                   footer="Its readings stopped, were switched off or the unit was set vacant, "
                          "so whether it cleared is not known. " + _stamp(now_utc))


def run_incidents(dal: Any, config: Any, now_utc: int, *, tgt: Optional[dict] = None) -> dict:
    """Push equipment incidents as they open (one message per group, or per unit), again when
    one gets worse (action -> critical), and an all-clear for each one that was pushed.

    Which tiers: `rules.push_critical` and `rules.push_action`. ACTION incidents already open
    when ACTION pushes were switched on are not replayed (the daily check-in lists them), and
    the watermark is dropped while they are off, so switching them back on never replays what
    opened in between."""
    out: dict = {"enabled": False, "pushed": 0, "cleared": 0, "errors": []}
    tiers = _push_tiers(config)
    if not dal.has_table("incidents"):
        return out
    if "action" not in tiers and dal.get_meta(_ACTION_AFTER) is not None:
        dal.set_meta(_ACTION_AFTER, None)
    if not tiers:
        return out
    out["enabled"] = True
    tgt = tgt or target(config)
    if not tgt:
        return out
    after = None
    if "action" in tiers:
        after = dal.get_meta(_ACTION_AFTER)
        if after is None:
            after = int(now_utc)
            dal.set_meta(_ACTION_AFTER, after)
    ov = _overrides_by_unit(dal, config)
    groups: dict = {}
    for r in dal.incidents_to_push():
        if r["tier"] not in tiers or (r["tier"] == "action" and int(r["opened_utc"]) < int(after)):
            continue
        groups.setdefault(r.get("group_key") or r["system_code"], []).append(r)
    for rows in groups.values():
        ok, detail = send(dal, incident_message(rows, now_utc, overrides=ov), now_utc, tgt=tgt,
                          kind="incident")
        if not ok:                      # nothing marked: the next run retries
            out["errors"].append(detail)
            continue                    # one rejected message must not hold back the rest
        for r in rows:
            dal.update_incident(r["id"], notified_utc=int(now_utc), notified_tier=r["tier"])
        out["pushed"] += len(rows)
    # An episode that began and ended before the rules next ran was recorded already closed and
    # never pushed (2026-10-27 review). Said once, as "came and went". Only those ending after
    # this was switched on: the gate keeps the history from replaying.
    gone_after = dal.get_meta(_GONE_AFTER)
    if gone_after is None:
        gone_after = int(now_utc)
        dal.set_meta(_GONE_AFTER, gone_after)
    gone = [r for r in dal.incidents_never_pushed(int(gone_after))
            if r["tier"] in tiers
            and not (r["tier"] == "action" and int(r["opened_utc"]) < int(after))]
    if gone:
        ok, detail = send(dal, gone_message(gone, now_utc), now_utc, tgt=tgt, kind="incident-gone")
        if ok:
            for r in gone:
                dal.update_incident(r["id"], notified_utc=int(now_utc), notified_tier=r["tier"],
                                    cleared_notified_utc=int(now_utc))
            out["pushed"] += len(gone)
        else:
            out["errors"].append(detail)
    clears = dal.incident_clears_to_push()
    cleared = [r for r in clears if (r.get("resolve_reason") or "cleared") == "cleared"]
    unjudged = [r for r in clears if (r.get("resolve_reason") or "cleared") != "cleared"]
    for rows, msg, kind in ((cleared, lambda rs: incident_message(rs, now_utc, cleared=True),
                             "incident-clear"),
                            (unjudged, lambda rs: unjudged_message(rs, now_utc),
                             "incident-unjudged")):
        if not rows:
            continue
        ok, detail = send(dal, msg(rows), now_utc, tgt=tgt, kind=kind)
        if ok:
            for r in rows:
                dal.update_incident(r["id"], cleared_notified_utc=int(now_utc))
            out["cleared"] += len(rows)
        else:
            out["errors"].append(detail)
    return out


_REMINDER = _META + "reminder."
_REMIND_AFTER_S = 86400          # not about an incident that opened, or was pushed, within a day


def _on_for(r: dict, now_utc: int) -> str:
    return (f"{_dur(now_utc - int(r['started_utc']))} (since "
            f"{time.strftime('%a %d %b %H:%M', time.localtime(int(r['started_utc'])))})")


def reminder_message(rows: Sequence[dict], now_utc: int) -> Message:
    """The daily reminder: every important alarm still on, worst first, each with how long.
    A normal notification (3), or high (4) when one is CRITICAL: it is the second telling,
    not the first, so it does not need the urgent one's sound."""
    critical = any(r["tier"] == "critical" for r in rows)
    icon = _INCIDENT_ICON["critical" if critical else "action"]
    if len(rows) == 1:
        r = rows[0]
        title = (f"{icon} HVACTrend: {r['system_code']}: {incident_name(r)} still on, "
                 f"{_dur(now_utc - int(r['started_utc']))}")
    else:
        title = f"{icon} HVACTrend: {len(rows)} alarms still on"
    lines = [f"\u2022 {'CRITICAL: ' if r['tier'] == 'critical' else ''}{r['system_code']}: "
             f"{incident_name(r)}, {_on_for(r, now_utc)}" for r in rows[:12]]
    if len(rows) > 12:
        lines.append(f"\u2022 and {len(rows) - 12} more")
    return Message(title, priority=4 if critical else 3, lines=lines,
                   footer="A reminder once a day while an alarm stays on. " + _stamp(now_utc))


def run_reminders(dal: Any, config: Any, now_utc: int, *, tgt: Optional[dict] = None) -> dict:
    """Once a day, from alerts.reminder_time (12:00: after the morning, so it lists what is
    STILL on), one push listing every open incident of a tier that is pushed, that opened and
    was last pushed more than a day ago. Those already open before ACTION pushes were switched
    on are listed too: they were never pushed, which is the more reason to say so.

    An incident pushed when it opened is otherwise never mentioned again until it clears
    (run_incidents). On 2026-10-26 AC-2's and AC-8's Multistack alarms had been on for 12 days
    after one push each, and the operator asked for reminders."""
    out: dict = {"reminded": 0, "errors": []}
    cfg = _cfg(config)
    tiers = _push_tiers(config)
    if not cfg["reminders"] or not tiers or not dal.has_table("incidents"):
        return out
    tgt = tgt or target(config)
    if not tgt or not _due(dal, now_utc, cfg["reminder_time"], _REMINDER + "date", (12, 0)):
        return out
    rows = [r for r in dal.open_incidents() if r["tier"] in tiers
            and int(now_utc) - max(int(r["opened_utc"]), int(r.get("notified_utc") or 0))
            >= _REMIND_AFTER_S]
    rows.sort(key=lambda r: (r["tier"] != "critical", int(r["started_utc"])))
    if rows:
        ok, detail = send(dal, reminder_message(rows, now_utc), now_utc, tgt=tgt,
                          kind="reminder")
        if not ok:                      # nothing marked: the next run retries
            out["errors"].append(detail)
            return out
    dal.set_meta(_REMINDER + "date", _local_date(now_utc))     # also "nothing to remind today"
    dal.set_meta(_REMINDER + "count", len(rows))
    out["reminded"] = len(rows)
    return out


def run(dal: Any, config: Any, now_utc: int, *, tgt: Optional[dict] = None,
        dests: Optional[list] = None) -> dict:
    """The watchdog's delivery step, after detection. A no-op until a topic is set."""
    tgt = tgt or target(config)
    out: dict = {"configured": bool(tgt), "alerts": 0, "cleared": 0, "checkin": None,
                 "errors": []}
    if not tgt:
        return out
    cfg = _cfg(config)
    hold = {"device-poll-failed": cfg["poll_failed_hold_s"]}

    # 1) new alerts, each held back until it has persisted long enough for its kind
    new = [a for a in dal.alerts_needing_notification(source=SOURCE)
           if int(a["last_seen_utc"]) - int(a["created_utc"]) >= hold.get(a["kind"], 0)]

    # 2) all-clears owed for alerts that WERE sent. The watermark starts at the first run
    #    with a topic, so switching delivery on never replays old history.
    mark = dal.get_meta(_META + "cleared_after_utc")
    if mark is None:
        dal.set_meta(_META + "cleared_after_utc", int(now_utc))
        mark = now_utc
    cleared = dal.resolved_alerts_since(SOURCE, int(mark))

    if new or cleared:
        ok, detail = send(dal, alerts_message(new, cleared, now_utc, _device_names(dal)),
                          now_utc, tgt=tgt, kind="alert")
        if ok:
            dal.mark_alerts_notified([a["id"] for a in new], now_utc)
            if cleared:
                dal.set_meta(_META + "cleared_after_utc",
                             max(int(a["resolved_utc"]) for a in cleared))
            out["alerts"], out["cleared"] = len(new), len(cleared)
        else:                           # nothing marked: the next run retries all of it
            out["errors"].append(detail)

    # 3) the daily check-in (also the dead-man's switch: no check-in = the box is down)
    if cfg["checkin"] and checkin_due(dal, now_utc, cfg["checkin_time"]):
        from . import checkin
        summary = checkin.build(dal, config, now_utc, dests=dests)
        ok, detail = send(dal, checkin_message(summary), now_utc, tgt=tgt, kind="checkin")
        if ok:
            dal.set_meta(_META + "checkin_date", _local_date(now_utc))
            out["checkin"] = summary["state"]
        else:
            out["errors"].append(detail)
    return out
