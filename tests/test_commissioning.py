"""The equipment map: which unit each discovered point belongs to, and what kind of unit."""

from hvactrend import commissioning as c

EQ = {
    "point_systems": [
        {"device": 2001, "match": "*CT1*", "system": "CT-1", "kind": "plant"},
        {"device": 2001, "match": "*pump*", "system": "CWP"},
    ],
    "device_systems": {1001: "AHU-1", "1002": {"system": "Penthouse unit", "kind": "ahu"}},
    "sc_dac_map": {3001: {1: "AHU-3", "2": "Lobby"}},
}


def test_the_first_rule_that_places_a_point_wins():
    assert c.system_for(2001, "SITE.CT1.FAN.SPD", EQ) == ("CT-1", "plant")
    assert c.system_for(2001, "CW Pump 2 Status", EQ) == ("CWP", "plant")   # glob, any case
    assert c.system_for(2001, "Outdoor Air Temp", EQ) == (None, None)       # left unplaced
    assert c.system_for(1001, "Discharge Air Temp", EQ) == ("AHU-1", "ahu")
    assert c.system_for(1002, "anything", EQ) == ("Penthouse unit", "ahu")  # explicit kind


def test_a_tracer_sc_places_points_by_their_dac_suffix():
    assert c.system_for(3001, "Discharge Air Temp|dac-1", EQ, dac_number=1) == ("AHU-3", "ahu")
    assert c.system_for(3001, "Space Temp|dac-2", EQ, dac_number=2) == ("Lobby", "ahu")
    assert c.system_for(3001, "SC Setting", EQ, dac_number=None) == (None, None)


def test_the_kind_comes_from_common_names_when_not_given():
    for name, kind in (("AHU-1", "ahu"), ("RTU 4", "ahu"), ("AC-12", "ahu"), ("MAU-1", "ahu"),
                       ("HV1", "hv"), ("BLR-2", "plant"), ("CT-1", "plant"), ("CHWP", "plant"),
                       ("Chiller 1", "plant"), ("Lobby", None), ("Penthouse unit", None)):
        assert c.infer_kind(name) == kind, name


def test_every_unit_and_every_controller_the_map_names():
    assert c.all_systems(EQ) == {"CT-1": "plant", "CWP": "plant", "AHU-1": "ahu",
                                 "Penthouse unit": "ahu", "AHU-3": "ahu", "Lobby": "ahu"}
    assert c.targets(EQ) == [2001, 1001, 1002, 3001]
    assert c.targets({**EQ, "targets": [1001]}) == [1001]
    assert c.targets({}) == []
