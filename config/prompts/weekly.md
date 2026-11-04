<!-- Weekly summary prompt. Edit freely: this file is
     version-controlled and read at every run. The literal token {context} is replaced with
     the week's items (alarm incidents, watchdog alerts, overrides, after-hours use,
     operator notes), how each air handler ran over the week, the weather, the data health,
     the site knowledge and the previous weekly summary. If you remove {context}, it is
     appended at the end. The fixed rules are in src/hvactrend/ai/summary.py. -->

Write the weekly summary for the seven days below.

Use these sections, in this order. Leave out a section with nothing to say, except the
first.

THE WEEK IN BRIEF
Three or four sentences: how the building ran, and the one or two things that matter.

STILL OPEN
Every problem still open at the end of the week, oldest first: how long, what the
evidence points to, and what to check.

RECURRING
Alarms that came and went more than once, with how many times, and what they have in
common (time of day, outdoor temperature, the same unit).

OVERRIDES AND AFTER-HOURS USE
Points held at an operator level and for how long. After-hours use by unit, with the
hours.

HOW THE UNITS RAN
Which units held their setpoints and which did not, from the unit picture. Only units
that stand apart, or that explain an item.

DATA
Polling and the program itself, in a line, unless something went wrong.

Keep it under about 600 words. The operator knows the building; do not explain the equipment.

{context}
