"""Rule-based alarms: is each unit doing what it is told?

engine.py   when a breach becomes an incident, and when it is over (debounce, hysteresis)
state.py    the one operating-state gate: running, occupied, settled
catalog.py  the rules and their default thresholds (settings: `rules:` in config.yaml)
evaluate.py the plumbing from stored samples to the engine; read-only

Stdlib only, and no import of the collector or the AI layer: the watchdog runs these.
"""
