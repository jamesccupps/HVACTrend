"""Read-only guardrail tests. Stdlib only — no hardware, no bacpypes3.

These are the tests that must NEVER regress: they assert the tool cannot emit a
write or a destructive service under default (or even overridden) config.
"""

import pytest

from hvactrend import safety
from hvactrend.bacnet import readonly
from hvactrend.bacnet.application import ReadOnlyBACnetApplication
from hvactrend.bacnet.readonly import WriteBlockedError, classify_service, guard_service


def test_reads_and_discovery_allowed():
    for svc in ("ReadPropertyRequest", "ReadPropertyMultipleRequest",
                "ReadRangeRequest", "WhoIsRequest", "IAmRequest"):
        guard_service(svc)  # must not raise
    # never sent, so not on the list: the stack checks every APDU against it (review M6)
    for svc in ("AtomicReadFileRequest", "WhoHasRequest"):
        with pytest.raises(WriteBlockedError):
            guard_service(svc)


def test_property_writes_blocked_by_default():
    for svc in ("WritePropertyRequest", "WritePropertyMultipleRequest", "WriteGroupRequest"):
        with pytest.raises(WriteBlockedError):
            guard_service(svc)  # allow_writes defaults False


def test_property_writes_gated_not_silent():
    # A knowing lab override CAN enable property writes...
    guard_service("WritePropertyRequest", allow_writes=True)  # no raise
    # ...but the default and the flag=False path stay blocked.
    with pytest.raises(WriteBlockedError):
        guard_service("WritePropertyRequest", allow_writes=False)


def test_destructive_services_never_allowed_even_with_flags():
    # These must be blocked UNCONDITIONALLY, regardless of any flag.
    for svc in ("ReinitializeDeviceRequest", "DeviceCommunicationControlRequest",
                "AtomicWriteFileRequest", "CreateObjectRequest", "DeleteObjectRequest",
                "ConfirmedPrivateTransferRequest"):
        with pytest.raises(WriteBlockedError):
            guard_service(svc, allow_writes=True, allow_cov=True)


def test_cov_gated_off_by_default():
    with pytest.raises(WriteBlockedError):
        guard_service("SubscribeCOVRequest")               # off by default
    guard_service("SubscribeCOVRequest", allow_cov=True)   # explicit enable ok


def test_unknown_service_fails_closed():
    with pytest.raises(WriteBlockedError):
        guard_service("SomeFutureVendorRequest")


def test_classification_matches_tiers():
    assert classify_service("ReadPropertyRequest") == "allowed"
    assert classify_service("WritePropertyRequest") == "gated-write"
    assert classify_service("SubscribeCOVRequest") == "gated-cov"
    assert classify_service("ReinitializeDeviceRequest") == "never"
    assert classify_service("MysteryRequest") == "unknown"


def test_application_exposes_no_write_method():
    # The read-only façade must not carry a write surface at all.
    for forbidden in ("write_property", "write_property_multiple", "write_group"):
        assert not hasattr(ReadOnlyBACnetApplication, forbidden)


def test_write_service_sets_are_disjoint():
    sets = [readonly.ALLOWED_SERVICES, readonly.GATED_WRITE_SERVICES,
            readonly.GATED_COV_SERVICES, readonly.NEVER_ALLOWED_SERVICES]
    for i, a in enumerate(sets):
        for b in sets[i + 1:]:
            assert not (a & b), f"service tiers overlap: {a & b}"


def test_application_exposes_no_raw_application_accessor():
    # The write-capable underlying bacpypes3 Application must never be handed out past
    # the guard (a `.raw` accessor was a latent read-only bypass — removed).
    assert not hasattr(ReadOnlyBACnetApplication, "raw")


# ── startup fail-fast: assert_read_only_default (the boot-time posture check) ──
def test_assert_read_only_default_passes_on_safe_config():
    safety.assert_read_only_default({"bacnet": {"safety": {"allow_writes": False}}})


def test_assert_read_only_default_allows_lab_override_with_warning(caplog):
    import logging
    with caplog.at_level(logging.WARNING):
        safety.assert_read_only_default({"bacnet": {"safety": {"allow_writes": True}}})
    assert any("allow_writes=true" in r.message for r in caplog.records)


def test_assert_read_only_default_raises_if_destructive_block_regresses(monkeypatch):
    # If a regression made guard_service stop blocking a never-allowed service, the
    # boot assertion MUST fail loudly rather than let the app start "safe".
    monkeypatch.setattr(safety, "guard_service", lambda *a, **k: None)
    with pytest.raises(AssertionError, match="SAFETY REGRESSION"):
        safety.assert_read_only_default({"bacnet": {"safety": {"allow_writes": False}}})
