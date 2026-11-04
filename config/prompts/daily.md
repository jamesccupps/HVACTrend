<!-- Daily summary prompt. Edit freely: this file is
     version-controlled and read at every run. The literal token {context} is replaced with
     the items the program found (alarm incidents, watchdog alerts, overrides, after-hours
     use, operator notes), how each air handler ran, the weather, the data health, the site
     knowledge and the previous summary. If you remove {context}, it is appended at the end.
     The fixed rules (only the program's items are faults; every action cites their ids;
     read-only) are in src/hvactrend/ai/summary.py and apply whatever this file says. -->

Write the daily summary for the window below.

Use these sections, in this order. Leave out a section with nothing to say, except the
first.

NEEDS ATTENTION
The open problems, most urgent first: what it is, since when, what the evidence points
to, and what to check. Say plainly when there is nothing.

CAME AND WENT
Alarms that started and cleared inside the window, in a line each. Say whether one keeps
coming back.

OVERRIDES AND AFTER-HOURS USE
Points held at an operator level, and how long. After-hours sessions, in a line each.

HOW THE UNITS RAN
Only what helps explain the items above, or a unit that clearly stood apart from the
others. A unit that ran normally needs no line.

DATA
One line on polling and the program itself, unless something went wrong.

Keep it short: about 300 words. The operator knows the building; do not explain what an air
handler or an economizer is.

{context}
