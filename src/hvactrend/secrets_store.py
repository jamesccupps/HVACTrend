"""Operator secrets (API keys / tokens) kept OUT of the plaintext settings table
and out of version control.

Stored in a gitignored KEY=VALUE file and loaded into the process environment at
startup, so existing env-based consumers keep working unchanged — e.g. the
Anthropic provider reads ``os.environ["ANTHROPIC_API_KEY"]`` (ai/provider.py) and
never needs to know a UI wrote it.

Design rules:
  * Only KNOWN secret names are accepted (no arbitrary env injection from the UI).
  * Values are NEVER returned to callers — the API gets a masked status only
    (set? + last4 + source).
  * An explicitly-set environment variable WINS over the file (so a setx / SYSTEM
    env stays authoritative); the file fills gaps (the path a SYSTEM task uses).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

# UI/API name  ->  the environment variable the app actually reads.
SECRET_ENV: dict[str, str] = {
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "tempest_token": "TEMPEST_TOKEN",
    # The UI auth token (api.auth.enabled gate). It belongs here rather than only in a
    # real env var because the server is moving to an unattended SYSTEM task: a
    # per-USER `setx` is invisible to SYSTEM, so the token has to live in the file the
    # process reads from its own working directory. An explicitly-set env var still
    # wins (see load_into_env), so a machine-level env var stays authoritative.
    # NOTE the ordering constraint: set this while api.auth is still OFF — once it is
    # ON, POST /api/settings/secrets is itself gated and would 503 with no token set.
    "ui_token": "HVACTREND_UI_TOKEN",
    # The ntfy TOPIC that alerts and the daily check-in are published to (notify.py). It is
    # a secret, not a setting: ntfy topics have no password, so anyone who knows the name
    # can read the notifications and post fake ones. Generated on the Settings page (~100
    # random bits). The WATCHDOG reads it too -- a separate process, which is why it has to
    # live in this file rather than only in the server's memory. (A Teams webhook was the
    # first choice, 2026-09-29; Teams Workflows needs a premium licence on this tenant.)
    "ntfy_topic": "HVACTREND_NTFY_TOPIC",
}


MAX_SECRET_LEN = 4096


def _default_path() -> Path:
    # Relative to the process cwd (serve.py chdir's to the repo root), matching how
    # config.storage.path ("data/…") is resolved. Overridable for tests/deploys.
    return Path(os.environ.get("HVACTREND_SECRETS", "config/secrets.env"))


def _read_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")     # partition -> value may itself contain '='
        out[key.strip()] = val.strip()
    return out


def load_into_env(path: Optional[Path] = None) -> list[str]:
    """Load the secrets file into ``os.environ`` WITHOUT overwriting already-set vars.

    Returns the env-var names that were populated from the file.
    """
    path = path or _default_path()
    populated: list[str] = []
    allowed = set(SECRET_ENV.values())      # never inject arbitrary env vars from the file
    for env_name, value in _read_file(path).items():
        if env_name in allowed and value and not os.environ.get(env_name):
            try:
                os.environ[env_name] = value
            except ValueError:      # an over-long value saved before the check: skip it,
                continue            # never stop the watchdog that loads this file
            populated.append(env_name)
    return populated


def set_secret(name: str, value: str, path: Optional[Path] = None) -> str:
    """Persist a KNOWN secret to the file and apply it to this process's env now.

    Raises KeyError for an unknown secret name. Returns the env-var name written.
    """
    if name not in SECRET_ENV:
        raise KeyError(name)
    # Checked BEFORE the file is written (2026-10-27 review):
    #  - every character str.splitlines() breaks on, not only \n and \r (\x0b, \x0c,
    #    \x1c-\x1e, \x85, \u2028, \u2029): reading the file back split on them, cutting
    #    the value short or injecting a second KEY=VALUE line;
    #  - a length no key or topic has: past Windows' 32,767-character environment limit the
    #    value was saved and THEN failed to load, and the watchdog, which loads this file on
    #    every run, died on it from then on.
    if (len(("x" + value + "x").splitlines()) != 1
            or any(ord(c) < 32 or 0x7f <= ord(c) <= 0x9f for c in value)):
        raise ValueError("secret value must be one line of printable characters")
    if len(value) > MAX_SECRET_LEN:
        raise ValueError(f"secret value is over {MAX_SECRET_LEN} characters")
    env_name = SECRET_ENV[name]
    path = path or _default_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    data = _read_file(path)
    data[env_name] = value
    body = "".join(f"{k}={v}\n" for k, v in sorted(data.items()))
    path.write_text(body, encoding="utf-8")
    try:
        os.chmod(path, 0o600)                  # best-effort tighten (POSIX; benign on Windows)
    except OSError:
        pass

    os.environ[env_name] = value               # take effect immediately for this process
    return env_name


def clear_secret(name: str, path: Optional[Path] = None) -> None:
    """Remove a known secret from the file AND this process's environment, so the
    masked status reflects the clear immediately (not only after a restart)."""
    if name not in SECRET_ENV:
        raise KeyError(name)
    env_name = SECRET_ENV[name]
    path = path or _default_path()
    data = _read_file(path)
    if env_name in data:
        del data[env_name]
        body = "".join(f"{k}={v}\n" for k, v in sorted(data.items()))
        path.write_text(body, encoding="utf-8")
    os.environ.pop(env_name, None)


def status(path: Optional[Path] = None) -> dict[str, dict[str, Any]]:
    """Masked status for the UI. NEVER returns a secret value.

    Per known secret: ``{set: bool, last4: str|None, source: 'env'|'file'|None}``.
    """
    path = path or _default_path()
    file_vals = _read_file(path)
    out: dict[str, dict[str, Any]] = {}
    for name, env_name in SECRET_ENV.items():
        env_val = os.environ.get(env_name)
        val = env_val or file_vals.get(env_name)
        out[name] = {
            "set": bool(val),
            "last4": (val[-4:] if val and len(val) >= 4 else None),
            "source": ("env" if env_val else ("file" if file_vals.get(env_name) else None)),
        }
    return out
