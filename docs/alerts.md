# Alarms and notifications

HVACTrend tells you two kinds of things:

- **The logging itself is in trouble**: data has stopped, or a controller is not answering.
- **Equipment is not doing what it is told**: the alarm rules.

Both are checked by the **watchdog** (`scripts/watchdog.py`). It is a separate program run
every 5 minutes, not part of the server, so a server that has stopped cannot also stop the
message saying so. Every phone notification comes from it, through [ntfy](https://ntfy.sh).

- [Setting up your phone](#setting-up-your-phone)
- [What you will get](#what-you-will-get)
- [The alarm rules](#the-alarm-rules)
- [Tuning the rules](#tuning-the-rules)
- [The morning check-in and the daily reminder](#the-morning-check-in-and-the-daily-reminder)
- [Self-hosting ntfy](#self-hosting-ntfy)

## Setting up your phone

On the **Settings** page, under *Alerts → your phone*:

1. Install **ntfy** from the App Store or Google Play. It is free and needs no account.
2. Click **Generate a private topic**.
3. In the app, tap **+**, type the topic exactly as shown, and subscribe.
4. Click **Send test message**.

The topic works like a password. Anyone who knows it can read your alerts, which is why it is
long and random, and why it lives in `config/secrets.env`, never in `config.yaml`. With no
topic set, nothing is sent anywhere.

The watchdog must be running for anything to arrive: install the service
([install.md](install.md#run-as-a-service)), or run `scripts/watchdog.py` every 5 minutes some
other way.

## What you will get

| Notification | When | Priority |
|---|---|---|
| 🔴 **Data has stopped** | No new reading from any point for 15 min (`alerts.stale_after_s`) | urgent |
| 🟠 **Controller not responding** | One controller silent for 30 min (`alerts.device_stale_after_s`) | high |
| 🟠 **Poll failing** | A controller's last poll failed, and still fails on the next watchdog run | high |
| 🟠 **Points missing** | A controller answers, but 3 or more of its logged points have not for 3 cycles: a reset or a reloaded database | high |
| 🔴 **CRITICAL equipment alarm** | As the incident opens; again if it gets worse (`rules.push_critical`) | urgent |
| 🟠 **ACTION equipment alarm** | As the incident opens (`rules.push_action`, off until you turn it on) | normal |
| 🟠 **Came and went** | An alarm that began and ended before the rules next ran, e.g. while the computer was off | high / normal |
| ✅ **Cleared** | When an alert or alarm that was sent ends | quiet |
| ❔ **Closed, NOT cleared** | An alarm closed because nothing could judge it for 3 days: its points off or not answering, or the unit set vacant | quiet / normal |
| **Morning check-in** | Every day at 07:00 (`alerts.checkin_time`) | quiet when all is well |
| **Reminder** | Every day at 12:00, listing equipment alarms on for more than a day (`alerts.reminder_time`) | normal |

Several alarms at once arrive as one message. When three or more units alarm within half an
hour, the message says to look at what they share first: the condenser water loop, power, a
controller.

## The alarm rules

Each rule judges a unit, or a point, against its own readings, and opens an **incident**:

- after the condition has lasted long enough, which is the tier's minutes;
- and only while the unit is supposed to be meeting it.

A tier is **ACTION** (look at it today) or **CRITICAL** (look now). An incident closes when
the condition has stayed clear for the rule's clear time.

| Rule | What it watches | Default tiers | Judged only when |
|---|---|---|---|
| `sat-setpoint` | Discharge air above its setpoint | ACTION 5 °F for 30 min; CRITICAL 10 °F for 10 min | a supply fan runs, the unit is occupied, and 60 min after start-up |
| `static-setpoint` | Duct static below its setpoint | ACTION 0.25 in. w.c. for 15 min | a supply fan runs, occupied, 45 min after start-up |
| `boiler-rise` | A boiler enabled but with under 5 °F between supply and return | ACTION for 50 min | the boiler is commanded on, 10 min after |
| `sensor-sanity` | A temperature outside what is physically possible (e.g. discharge air below -40 or above 150 °F) | ACTION for 10 min | always |
| `bas-alarm` | A controller's own binary alarm point on | ACTION after 15 min; CRITICAL after 2 min for freeze, smoke, flame or fire | always |

The rules find their points by **role**: discharge air, its setpoint, fan speed and so on.
The role comes from the point's name when the model is rebuilt. If a unit's rule never runs,
the **Point Coverage** page says which point is missing, and whether it is logged.

Incidents appear on the dashboard and in the check-in. If you use the AI summary, it explains
them.

## Tuning the rules

Every threshold is a setting. Set a building-wide value under `rules`, or set one unit apart
under `rules.overrides`. A tier set to `null` is switched off.

```yaml
rules:
  sat-setpoint:
    tiers: {action: {delta: 6.0, minutes: 45}}
  overrides:
    AHU-2:                                   # one unit that runs warm by design
      sat-setpoint: {tiers: {action: null}}
    RTU-4:
      static-setpoint: {settle_min: 90}
```

**Replay a change before you rely on it.** `rules_backtest.py` runs the rules over your own
history and lists what would have fired. It sends nothing and writes nothing.

```
.venv\Scripts\python scripts\rules_backtest.py --days 30 --list
.venv\Scripts\python scripts\rules_backtest.py --rule sat-setpoint --list
```

A sensible order for a new building:
1. Log for a couple of weeks.
2. Replay the rules.
3. Adjust the thresholds or the site knowledge until what remains is real.
4. Then set `rules.push_action: true`.

Points that are known to be wrong (a filter switch that never reads right, an override held on
purpose) belong in `site_knowledge` ([commissioning.md](commissioning.md#site-knowledge)), so
they are not judged at all.

## The morning check-in and the daily reminder

**The check-in** is a summary of yesterday. It covers:
- polls completed;
- controllers that missed reads;
- open equipment alarms;
- overrides in place;
- the latest backup, and whether it was copied off the computer;
- whether the AI summary ran.

It is quiet (no sound) when all is well, and a normal notification when something wants a
look. It also proves the computer is alive: no check-in by mid-morning means it is down.

**The reminder**, at noon, lists every CRITICAL and ACTION alarm that has been on for more
than a day, with how long. An alarm is pushed once when it opens, so without the reminder
one left on for a fortnight is easy to forget.

Turn either off with `alerts.checkin: false` or `alerts.reminders: false`.

## Self-hosting ntfy

ntfy.sh is a free public service. To keep notifications on your own server, run
[ntfy](https://docs.ntfy.sh/install/) yourself, set `alerts.ntfy_server` to its address, and
point the app at it.
