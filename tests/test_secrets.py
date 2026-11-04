"""secrets_store tests — the module enforces two HARD INVARIANTS: only masked last-4
is ever exposed, and only KNOWN secret names may be injected into the environment (no
arbitrary env injection from the UI/file). Hermetic: tmp_path file + a saved/restored
os.environ so nothing leaks between tests or into the real process."""

import os

import pytest

from hvactrend import secrets_store


@pytest.fixture
def clean_env():
    saved = {k: os.environ.get(k) for k in secrets_store.SECRET_ENV.values()}
    for k in saved:
        os.environ.pop(k, None)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_set_roundtrip_masks_value_and_sets_env(tmp_path, clean_env):
    p = tmp_path / "secrets.env"
    secrets_store.set_secret("anthropic_api_key", "sk-ABCD1234", path=p)
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ABCD1234"
    st = secrets_store.status(path=p)["anthropic_api_key"]
    # NEVER the full value — masked last4 + source only
    assert st == {"set": True, "last4": "1234", "source": "env"}
    assert "sk-ABCD1234" not in str(secrets_store.status(path=p))


def test_unknown_secret_name_rejected(tmp_path, clean_env):
    with pytest.raises(KeyError):
        secrets_store.set_secret("evil_name", "x", path=tmp_path / "s.env")


def test_newline_value_rejected_and_not_written(tmp_path, clean_env):
    p = tmp_path / "s.env"
    with pytest.raises(ValueError):
        secrets_store.set_secret("anthropic_api_key", "sk-1\nEVIL=1", path=p)
    assert not p.exists()                      # nothing written -> no env-line injection
    assert "ANTHROPIC_API_KEY" not in os.environ


def test_load_into_env_ignores_non_allowlisted_lines(tmp_path, clean_env):
    p = tmp_path / "s.env"
    p.write_text("ANTHROPIC_API_KEY=sk-keep\nEVIL=1\nPATH=/pwned\n", encoding="utf-8")
    populated = secrets_store.load_into_env(path=p)
    assert populated == ["ANTHROPIC_API_KEY"]  # only known env targets injected
    assert os.environ.get("ANTHROPIC_API_KEY") == "sk-keep"
    assert "EVIL" not in os.environ            # arbitrary keys never injected


def test_env_wins_over_file_and_source_reported(tmp_path, clean_env):
    p = tmp_path / "s.env"
    p.write_text("ANTHROPIC_API_KEY=sk-fromfile\n", encoding="utf-8")
    os.environ["ANTHROPIC_API_KEY"] = "sk-fromenv"
    assert secrets_store.load_into_env(path=p) == []   # env already set -> not overwritten
    st = secrets_store.status(path=p)["anthropic_api_key"]
    assert st["source"] == "env" and st["last4"] == "menv"[-4:]


def test_clear_removes_from_file_and_env(tmp_path, clean_env):
    p = tmp_path / "s.env"
    secrets_store.set_secret("tempest_token", "tok-9999", path=p)
    secrets_store.clear_secret("tempest_token", path=p)
    assert "TEMPEST_TOKEN" not in os.environ
    assert secrets_store.status(path=p)["tempest_token"]["set"] is False
