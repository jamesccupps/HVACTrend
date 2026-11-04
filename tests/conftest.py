"""Suite-wide safety: no test may reach the operator's phone.

create_app()'s lifespan loads config/secrets.env into os.environ -- the REAL file, found
relative to the repo -- and since 2026-09-29 one of those secrets is the ntfy topic the operator's phone
subscribes to. Once it is set, any test that exercised delivery without faking the transport
would publish to that topic and buzz their phone. So every test gets an empty secrets file and
no topic in its environment; a test that needs one sets it explicitly.
"""

import pytest


@pytest.fixture(autouse=True)
def _no_real_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("HVACTREND_SECRETS", str(tmp_path / "no-secrets.env"))
    monkeypatch.delenv("HVACTREND_NTFY_TOPIC", raising=False)
