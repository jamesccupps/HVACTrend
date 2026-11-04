"""The setup wizard fills the template without losing its comments, and the result is a
config the program will run with."""

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _wizard():
    spec = importlib.util.spec_from_file_location("configure_t", REPO / "scripts" / "configure.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ANSWERS = {"name": "Riverside Tower", "code": "RVT", "location": "Springfield, IL",
           "ip": "192.0.2.50", "prefix": 23, "instance": 3999001, "lan": False, "port": 8000,
           "lat": "", "lon": ""}


def test_the_answers_land_and_the_comments_stay(tmp_path):
    pytest.importorskip("yaml")
    from hvactrend.config import load_config, unfilled_placeholders
    w = _wizard()
    template = (REPO / "config" / "config.example.yaml").read_text(encoding="utf-8")
    text = w.render(template, ANSWERS)
    assert "# ── BACnet" in text and "docs/commissioning.md" in text     # comments kept
    f = tmp_path / "config.yaml"
    f.write_text(text, encoding="utf-8")
    cfg = load_config(str(f))
    assert unfilled_placeholders(cfg) == []                           # ready to bind
    assert (cfg.site.name, cfg.site.code, cfg.site.location) == ("Riverside Tower", "RVT",
                                                                 "Springfield, IL")
    assert cfg.bacnet.local_address == "192.0.2.50/23"
    assert cfg.bacnet.device_instance == 3999001
    assert cfg.api.host == "127.0.0.1" and cfg.api.port == 8000
    assert cfg.weather.open_meteo.enabled is False
    assert cfg.equipment.building.name == "Riverside Tower"
    assert cfg.bacnet.safety.allow_writes is False


def test_the_network_viewer_and_the_weather_are_choices(tmp_path):
    pytest.importorskip("yaml")
    from hvactrend.config import load_config
    w = _wizard()
    template = (REPO / "config" / "config.example.yaml").read_text(encoding="utf-8")
    text = w.render(template, {**ANSWERS, "lan": True, "port": 8080, "lat": "39.78",
                               "lon": "-89.65", "name": 'He said "hi"'})
    f = tmp_path / "config.yaml"
    f.write_text(text, encoding="utf-8")
    cfg = load_config(str(f))
    assert cfg.api.host == "0.0.0.0" and cfg.api.port == 8080
    assert cfg.weather.open_meteo.enabled is True
    assert (cfg.weather.open_meteo.latitude, cfg.weather.open_meteo.longitude) == (39.78, -89.65)
    assert cfg.site.name == 'He said "hi"'                             # quoting holds


def test_an_address_is_checked():
    w = _wizard()
    assert w.valid_ipv4("192.168.1.50")
    assert not w.valid_ipv4("192.168.1") and not w.valid_ipv4("300.1.1.1")
    assert not w.valid_ipv4("a.b.c.d")


def test_windows_gets_backslashes_except_in_the_web_address():
    w = _wizard()
    assert w.windows_paths("run .venv/Scripts/python scripts/discover.py, open "
                           "http://localhost:8000 and docs/install.md") == (
        r"run .venv\Scripts\python scripts\discover.py, open http://localhost:8000 and "
        r"docs\install.md")
