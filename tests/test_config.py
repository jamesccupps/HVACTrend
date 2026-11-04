"""Config loader tests. Skips gracefully if PyYAML isn't installed."""

import pytest

pytest.importorskip("yaml", reason="PyYAML not installed (pip install -e .)")

from hvactrend.config import (  # noqa: E402
    Config,
    ConfigError,
    _Node,
    apply_overrides,
    is_settable,
    load_config,
    unfilled_placeholders,
)

_EXAMPLE = "config/config.example.yaml"


# ── DB-override guardrail (Settings tab) — protects the read-only HARD INVARIANT ──
def test_is_settable_refuses_readonly_and_auth_and_bind_keys():
    # bacnet.* is not a settable domain at all -> the read-only posture can never be
    # flipped from the DB/UI. api.auth.* / host / port and the LLM key are denied too.
    for denied in ("bacnet.safety.allow_writes", "bacnet.local_address",
                   "api.auth.enabled", "api.auth.token", "api.host", "api.port",
                   "ai.providers.claude.api_key"):
        assert is_settable(denied) is False, denied
    # a normal scalar leaf inside a settable domain IS overridable
    assert is_settable("ai.routing.daily") is True


def test_apply_overrides_cannot_flip_read_only_or_auth():
    cfg = load_config(_EXAMPLE)
    applied = apply_overrides(cfg, {
        "bacnet.safety.allow_writes": True,     # the attack: enable BACnet writes from DB
        "api.auth.enabled": False,              # or disable the auth gate
        "api.host": "0.0.0.0",
        "ai.providers.claude.api_key": "sk-leak",
    })
    assert applied == []                        # every one refused
    assert cfg.bacnet.safety.allow_writes is False   # posture unchanged
    assert cfg.bacnet.safety.allow_cov is False


def test_apply_overrides_applies_existing_scalar_leaf_only():
    cfg = Config(_Node({"ai": {"routing": {"daily": "claude"}}, "storage": {"path": "x"}}))
    applied = apply_overrides(cfg, {
        "ai.routing.daily": "ollama",           # existing scalar leaf -> applied
        "ai.routing.brand_new": "x",            # new key -> refused (no injection)
        "ai.routing": {"daily": "z"},           # dict subtree clobber -> refused
        "storage.path.sub": "y",                # traverse a scalar as dict -> refused, no raise
    })
    assert applied == ["ai.routing.daily"]
    assert cfg.ai.routing.daily == "ollama"
    assert "brand_new" not in cfg.ai.routing.as_dict()


def test_example_config_loads_and_is_read_only():
    cfg = load_config(_EXAMPLE)
    assert cfg.bacnet.safety.allow_writes is False
    assert cfg.bacnet.safety.allow_cov is False
    assert cfg.bacnet.poll.interval_s == 120


def test_example_config_is_safe_as_shipped():
    """What a new installation gets before anyone changes anything: the web UI on this
    computer only, other computers (once allowed) limited to the read-only viewer, nothing
    sent anywhere and no AI, and the old statistical pre-screen off."""
    cfg = load_config(_EXAMPLE)
    assert cfg.api.host == "127.0.0.1"
    assert cfg.api.remote_viewer_only is True
    assert cfg.ai.enabled is False
    assert cfg.alerts.enabled is False
    assert cfg.rules.push_action is False
    assert cfg.storage.backup.enabled is True
    assert "template_dir" in cfg.ai.prompts.as_dict()


def test_example_config_flags_unfilled_placeholders():
    cfg = load_config(_EXAMPLE)
    unfilled = unfilled_placeholders(cfg)
    # The template intentionally leaves these for deploy-time — must be detected
    # so the collector refuses to bind BACnet with a bogus identity.
    assert "bacnet.local_address" in unfilled
    assert "bacnet.device_instance" in unfilled
    # ...and nothing else: any placeholder anywhere stops the BACnet bind
    assert sorted(unfilled) == ["bacnet.device_instance", "bacnet.local_address"]


def test_bad_safety_type_rejected(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "bacnet:\n"
        "  safety:\n"
        "    allow_writes: yes-please\n"   # not a bool
        "  poll: { interval_s: 120 }\n"
        "storage: { path: x.db }\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_config(bad)


def test_missing_storage_rejected(tmp_path):
    bad = tmp_path / "bad2.yaml"
    bad.write_text("bacnet:\n  poll: { interval_s: 120 }\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(bad)
