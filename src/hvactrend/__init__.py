"""HVACTrend — read-only BACnet trend collector + viewer + AI analysis.

Hard guardrail: BACnet is STRICTLY READ-ONLY. See bacnet.readonly.
"""

def _version() -> str:
    """From the installed package's metadata, so pyproject.toml is the one place a version
    is written. A hand-kept copy here had drifted a release behind (2026-11-06 review)."""
    try:
        from importlib.metadata import PackageNotFoundError, version
        try:
            return version("hvactrend")
        except PackageNotFoundError:           # run from a source tree, never installed
            return "0+unknown"
    except ImportError:
        return "0+unknown"


__version__ = _version()
