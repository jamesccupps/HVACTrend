# Commissioning: from a network to logged units

HVACTrend needs to know three things about a building:
- which controllers exist;
- which unit each point belongs to;
- which points to log.

Discovery finds the first; you tell it the second in `config.yaml`; you choose the third on
the Point Selection page.

- [1. Discover the controllers](#1-discover-the-controllers)
- [2. The equipment map](#2-the-equipment-map)
- [3. Enumerate and group](#3-enumerate-and-group)
- [4. Choose what to log](#4-choose-what-to-log)
- [5. Roles](#5-roles)
- [Site knowledge](#site-knowledge)

## 1. Discover the controllers

With the server stopped, since only one program can use the BACnet port:

```
.venv\Scripts\python scripts\discover.py --identify     # who is there: name, vendor, model
.venv\Scripts\python scripts\discover.py --devices-only # just instances and addresses
.venv\Scripts\python scripts\discover.py --directed 192.0.2.10   # ask one address directly
```

Discovery sends Who-Is and reads; it never writes. Broadcasts reach your own subnet. A
controller across a router answers to `--directed`, or through a BBMD if your network has one.
MS/TP controllers behind a BACnet router answer like any other.

## 2. The equipment map

The `equipment` section of `config/config.yaml` says which unit each point belongs to. Use
whichever form fits each controller; you can mix them.

### One controller, one unit

Typical of unitary and AHU controllers:

```yaml
equipment:
  device_systems:
    1001: "AHU-1"
    1002: "AHU-2"
    1003: {system: "Penthouse unit", kind: ahu}
```

### One controller, several units

Typical of a plant controller: assign its points by name, with a glob (`*` matches anything,
case is ignored). The first match wins.

```yaml
equipment:
  point_systems:
    - {device: 2001, match: "*BLR1*", system: "BLR-1", kind: plant}
    - {device: 2001, match: "*BLR2*", system: "BLR-2", kind: plant}
    - {device: 2001, match: "*CT1*",  system: "CT-1",  kind: plant}
    - {device: 2001, match: "*CWP*",  system: "CWP",   kind: plant}
```

### A Trane Tracer SC

The SC flattens each unit's points into itself, with a `|dac-N` suffix. Map N to a unit:

```yaml
equipment:
  sc_dac_map:
    3001: {1: "AHU-3", 2: "AHU-4", 3: "AHU-5"}
```

### `kind`

`kind` decides which alarm rules apply:
- `ahu` / `hv`: air handlers (discharge air, duct static);
- `plant`: boilers, towers, pumps (boiler rise);
- anything else gets no unit rules.

Leave it out and it is guessed from the name:
- `AHU-1`, `RTU-2`, `AC-3`, `MAU-1` → `ahu`;
- `HV1` → `hv`;
- `BLR-1`, `CT-1`, `CHWP`, `Chiller 1` → `plant`.

Also: `equipment.building` names the building, and `equipment.targets` limits which
controllers are enumerated (default: every one the map names).

## 3. Enumerate and group

```
.venv\Scripts\python scripts\commission.py              # every controller in the map
.venv\Scripts\python scripts\commission.py --only 2001  # one
```

It reads each controller's object list in batches; a supervisory controller with thousands of
objects takes a few minutes. It then:
- stores every object;
- groups it per the map;
- classifies its role (below);
- prints a count per unit.

Points the map does not place are kept, under no unit. Re-run it whenever you change the
map; it never changes which points you chose to log.

## 4. Choose what to log

Start the server and open **Point Selection**:
1. Pick a unit on the left and tick the points to log.
   - **Read visible values** reads them live, so you can confirm a point is the one you
     think.
   - The search box filters by name; *loggable only* hides configuration objects.
2. Click **⟳ Rebuild model** when you are done. It classifies roles for the dashboard and the
   alarm rules, and also runs daily by itself.

A good first set per air handler:
- discharge, mixed and return air temperatures, and the discharge setpoint;
- supply fan speed and status;
- outdoor air damper;
- heating and cooling valves;
- duct static and its setpoint;
- occupancy.

For plant equipment:
- supply and return temperatures and setpoints;
- enables and statuses;
- pump and fan speeds;
- the alarm points.

To have one suggested, run the auto-selector:

```
.venv\Scripts\python scripts\select_trend_points.py              # dry run: what it would pick
.venv\Scripts\python scripts\select_trend_points.py --commit     # apply to units with nothing logged yet
.venv\Scripts\python scripts\select_trend_points.py --system AHU-1 --commit   # add its picks to one unit
```

It only adds; it never unticks what you chose (unless you pass `--prune`).

Every 2 minutes, each logged point costs one read; about 1,000 points is comfortable. The
**Point Coverage** page shows, per unit, which alarm rules can judge it and which points
would complete the rest.

In the trend viewer, the ✎ next to a point renames it or adds a note. That's useful when the
controller's name is cryptic. A rename also helps the role classifier.

## 5. Roles

Each logged point gets a **role**: `DAT` (discharge air temperature), `DAT_ACTIVE_SP`,
`MAT`, `RAT`, `OAT`, `FAN_SPEED`, `FAN_STATUS`, `OA_DAMPER`, `DUCT_PRESS`, `BLR_SUP_TEMP`,
and so on. The role is classified from the point's name, then your rename, then its BACnet
description. The roles drive:
- the dashboard's columns;
- the viewer's *By metric* list and its setpoint pairing;
- the alarm rules.

The classifier knows the naming of common controller families: Trane, Siemens tags, JCI, and
plain English ("Discharge Air Temp", "Boiler 1 Supply Water Temp"). If a point gets the
wrong role, or none:
- rename it in the viewer, then rebuild the model;
- and please open an issue with the name, so the classifier can learn it.

## Site knowledge

Things about your building's points that the data cannot show, in `config.yaml`:

```yaml
site_knowledge:
  ignore_points:                     # never offered, never judged
    - {match: "Filter Status*", systems: "AHU-*", why: "the filter switches are not reliable"}
  rules_not_possible:                # "not possible here", not a gap
    - {rule: damper-feedback, systems: "AHU-*", why: "no damper end switches"}
  expected_overrides:                # held on purpose: shown, but not an alarm
    - {match: "DHW Call", systems: "BLR-1", value: "OFF", why: "domestic hot water not connected"}
```

- `match` is a glob on the BACnet object name.
- `systems` is a glob, or a list, on the unit code. Both ignore case.
- Every `why` is shown wherever the point would otherwise appear.
- A malformed entry is an error at start-up, not a silent no-op.
