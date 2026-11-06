"""Your own devices, by network (2026-11-06): api.local_hosts may name a network, such as a
Tailscale tailnet where every device is the operator's. Those get every page; the rest of
the network still gets the trend viewer and Realtime only."""

import pytest

from .test_review_1106 import _app

TAILNET = ("api: {local_hosts: ['100.64.0.0/10', 'fd7a:115c:a1e0::/48', 'not/an/address'], "
           "allowed_hosts: [box.tail1234.ts.net]}\n")


def _client(app, host):
    from fastapi.testclient import TestClient
    return TestClient(app, client=(host, 51234))


def test_a_device_on_your_own_tailnet_gets_every_page(tmp_path, caplog):
    pytest.importorskip("fastapi")
    app = _app(tmp_path, TAILNET)
    for host in ("100.101.102.103", "fd7a:115c:a1e0::1234"):
        with _client(app, host) as c:
            assert c.get("/dashboard").status_code == 200, host
            assert c.get("/api/settings").status_code == 200, host
            assert 'class="viewer-only"' not in c.get("/trends").text, host
    with _client(app, "192.0.2.77") as c:                  # the VLAN: as before
        assert c.get("/dashboard").status_code == 404
        assert 'class="viewer-only"' in c.get("/trends").text
    with _client(app, "100.128.0.1") as c:                 # just outside 100.64.0.0/10
        assert c.get("/dashboard").status_code == 404
    assert any("not/an/address" in r.getMessage() for r in caplog.records)


def test_a_change_through_tailscale_serve_is_same_site(tmp_path):
    """`tailscale serve` passes the Host through, and the page is https: a POST from it is
    from the page's own origin, not refused as cross-site."""
    pytest.importorskip("fastapi")
    app = _app(tmp_path, TAILNET)
    with _client(app, "100.101.102.103") as c:
        h = {"host": "box.tail1234.ts.net", "origin": "https://box.tail1234.ts.net",
             "sec-fetch-site": "same-origin"}
        # it gets past the gates to the endpoint, which has no BACnet here (503)
        assert c.post("/api/discover", headers=h).status_code == 503
        assert c.post("/api/discover", headers={**h, "origin": "https://evil.example",
                                                "sec-fetch-site": "cross-site"}).status_code == 403
