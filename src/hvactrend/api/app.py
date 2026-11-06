"""FastAPI app.

This process OWNS the event loop. The lifespan loads config + DAL, creates the ONE
shared read-only BACnet application (single UDP bind), and launches the collector
as a task on the same loop. Both the collector's polling and the browser's live
preview reads use that SAME `app.state.bacnet` — never a second stack.

Resilience: if BACnet can't bind (e.g. wrong NIC, or HVACTREND_NO_BACNET=1),
the app still serves the cached inventory for browsing; only the live-read endpoints
return 503. Selecting points for logging is a local DB write, not a BACnet write —
the BACnet side stays strictly read-only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG = os.environ.get("HVACTREND_CONFIG", "config/config.yaml")
_STATIC = Path(__file__).parent / "static"


def create_app(config_path: str = _DEFAULT_CONFIG, *, enable_bacnet: bool = True):
    """Build the FastAPI application (FastAPI imported lazily).

    enable_bacnet=False (or env HVACTREND_NO_BACNET=1) skips the BACnet bind for
    browse-only use and tests; live-read endpoints then return 503.
    """
    bind_bacnet = enable_bacnet and os.environ.get("HVACTREND_NO_BACNET") != "1"
    from fastapi import Body, FastAPI, Header, HTTPException
    from fastapi.responses import HTMLResponse

    from ..bacnet import priority as _priority
    from ..bacnet.application import close_application, get_application
    from ..bacnet.discovery import DiscoveredDevice, enumerate_device
    from ..collector.service import Collector
    from ..config import load_config
    from ..discovery_service import run_discovery
    from ..safety import assert_read_only_default
    from ..storage.sqlite_dal import SQLiteDAL

    @asynccontextmanager
    async def lifespan(app: "FastAPI"):
        from .. import secrets_store
        from ..config import apply_overrides
        config = load_config(config_path)
        # Operator secrets (API keys/tokens) into the environment BEFORE any provider
        # use, so env-based consumers (Anthropic provider) keep resolving unchanged.
        secrets_store.load_into_env()

        # Open storage first so DB-backed setting overrides (the Settings tab) apply to
        # the loaded config BEFORE the read-only safety re-check below.
        dal = SQLiteDAL(config.storage.path, config.get("storage").get("schema_file"))
        dal.apply_schema()
        applied = apply_overrides(config, dal.all_settings())

        _setup_logging(config)                    # so the unattended box isn't blind
        if applied:
            logger.info("applied %d DB setting override(s): %s",
                        len(applied), ", ".join(sorted(applied)))
        assert_read_only_default(config)          # fail fast if safety posture tampered

        # Warn loudly if the settings/secrets/backup surface is network-reachable
        # without auth.
        _api = config.get("api")
        _host = (_api.get("host", "127.0.0.1") if _api else "127.0.0.1")
        _auth = (_api.get("auth") if _api else None)
        if _host not in ("127.0.0.1", "localhost", "::1") and not (_auth and _auth.get("enabled", False)):
            logger.warning("The web UI listens on %s without a login. Other computers get the "
                           "read-only trend viewer only (api.remote_viewer_only); settings, "
                           "secrets and backups stay on this computer. Set api.auth.enabled "
                           "and HVACTREND_UI_TOKEN to require a token as well.", _host)

        _install_bacnet_noise_filter()

        app.state.config = config
        app.state.dal = dal
        app.state.bacnet = None
        app.state.collector = None
        app.state.collector_task = None
        app.state.bind_requested = bind_bacnet
        app.state.bind_error = None
        app.state.bind_task = None
        app.state.shutting_down = False
        _bn = config.get("bacnet")
        _bind_retry_s = float((_bn.get("bind_retry_s", 10) if _bn else 10) or 10)
        _relaunch_s = float((_bn.get("collector_relaunch_s", 30) if _bn else 30) or 30)

        # ⚠️ A failed bind used to degrade to browse-only FOREVER. `_assert_bindable`
        # raises the loud error it was written for, this lifespan caught it and logged a
        # WARNING, and the process stayed UP -- so Task Scheduler's restart-on-failure never
        # fired, nothing ever retried, and /health answered "ok" while not one sample was
        # being recorded. The watchdog only wrote an alert row and nothing delivered it, so
        # the failure reached nobody (since 2026-09-29 the watchdog sends it to the operator's
        # phone -- notify.py). The likeliest trigger is a boot race: the "At startup"
        # task runs before the vEthernet adapter holds its DHCP address.
        # Now: the viewer still comes up immediately in browse-only mode, a background task
        # keeps retrying the bind with backoff, and /health answers 503 the whole time.
        # 2026-09-22 audit, H-2.
        def _launch_collector() -> None:
            # A collector that cannot even be CONSTRUCTED (its configuration) is said as
            # that, with the stack still bound. It used to be caught as "BACnet application
            # unavailable", which threw the bound stack away, and the retry task then died
            # on the same error outside its try (2026-10-27 review).
            try:
                collector = Collector(app.state.bacnet, dal, config)
            except Exception as exc:  # noqa: BLE001
                app.state.bind_error = f"the collector could not start: {exc}"[:300]
                logger.exception("the collector could NOT START (BACnet is bound); collection "
                                 "is NOT running. Check the settings saved on the Settings page.")
                return
            app.state.collector = collector
            t = asyncio.create_task(collector.run(), name="hvactrend-collector")
            app.state.collector_task = t
            t.add_done_callback(_on_collector_done)

        def _on_collector_done(t: "asyncio.Task") -> None:
            # run() never returns on its own, so ending here means it DIED. A live web
            # server in front of a dead recorder is the exact shape of failure this whole
            # finding is about, so relaunch rather than leave it.
            if app.state.shutting_down or t.cancelled():
                return
            logger.error("collector task ENDED unexpectedly (%r); relaunching in %.0f s",
                         t.exception(), _relaunch_s)
            asyncio.get_running_loop().call_later(_relaunch_s, _relaunch)

        def _relaunch() -> None:
            if not app.state.shutting_down and app.state.bacnet is not None:
                _launch_collector()

        async def _bind_until_bound(delay: float) -> None:
            while not app.state.shutting_down:
                await asyncio.sleep(delay)
                try:
                    app.state.bacnet = await get_application(config)
                except Exception as exc:          # still not bindable -- keep saying so
                    app.state.bind_error = str(exc)[:300]
                    delay = min(delay * 2, 300.0)
                    logger.error("BACnet still not bindable (%s); collection is NOT "
                                 "running. Next attempt in %.0f s.", exc, delay)
                    continue
                app.state.bind_error = None
                _launch_collector()
                logger.info("BACnet bound on retry; collector task launched.")
                return

        # THE single BACnet application (one UDP bind). Optional so the browser
        # still works for cached-inventory review if the NIC isn't available.
        if bind_bacnet:
            try:
                app.state.bacnet = await get_application(config)
            except Exception as exc:  # binding failed — browse-only UNTIL a retry binds
                app.state.bacnet = None
                app.state.bind_error = str(exc)[:300]
                logger.error("BACnet application unavailable; collection is NOT running. "
                             "Retrying every %.0f s (backing off to 300 s).",
                             _bind_retry_s, exc_info=True)
                app.state.bind_task = asyncio.create_task(
                    _bind_until_bound(_bind_retry_s), name="hvactrend-bind-retry")
            else:
                _launch_collector()
                if app.state.collector is not None:
                    logger.info("BACnet app bound; collector task launched on the shared loop.")
        try:
            yield
        finally:
            app.state.shutting_down = True      # stop the retry/relaunch machinery first
            if app.state.bind_task is not None:
                app.state.bind_task.cancel()
            if app.state.collector is not None:
                app.state.collector.stop()
            if app.state.collector_task is not None:
                app.state.collector_task.cancel()
                try:
                    await app.state.collector_task
                except asyncio.CancelledError:
                    pass
            await close_application()
            dal.close()

    from .. import __version__
    app = FastAPI(title="HVACTrend", version=__version__, lifespan=lifespan)

    def _bacnet():
        b = app.state.bacnet
        if b is None:
            raise HTTPException(status_code=503,
                                detail="BACnet stack not available (browse-only mode)")
        return b

    def _valid_rollup(rollup: Optional[str]) -> Optional[str]:
        roll = _norm_rollup(rollup)
        if roll not in (None, "5m", "1h"):
            raise HTTPException(status_code=400, detail="rollup must be raw|5m|1h")
        return roll

    # ── remote = trend viewer only ────────────────────────────────────────────
    # A tech on the HVAC VLAN should be able to type the box's address and read trends.
    # They have no reason to see point selection, the unit dashboard, AI reports or
    # settings, so remote clients are served the viewer and the five endpoints it needs,
    # and nothing else. This is SCOPE, not a security boundary -- anyone on that VLAN can
    # already speak BACnet to the controllers directly, which is far more access than this
    # program has (it cannot write at all). Local requests are unaffected.
    _VIEWER_GET = frozenset({"/", "/trends", "/health",
                             "/api/extent", "/api/logged", "/api/weather",
                             # read-only operator log, drawn on the trend viewer.
                             # GET only -- POST/DELETE are not in _VIEWER_POST, so a
                             # remote client can read the notes and never write one.
                             "/api/annotations",
                             # saved views: read-only remotely, for the same reason
                             # annotations are -- a view built at the desk is what a
                             # tech in the mechanical room actually wants.
                             "/api/views",
                             # the OVR marks, refreshed once a minute: the same facts
                             # /api/logged already carries, without its per-point counts
                             "/api/overrides"})
    # read-only despite the verb: ids in a body. /api/export is the viewer's "Export data".
    _VIEWER_POST = frozenset({"/api/series", "/api/export"})

    def _local_hosts() -> frozenset:
        """Addresses that count as "on the box". Loopback is not enough: browsing to this
        machine's OWN VLAN address (192.0.2.5) from the console arrives with that address as
        the client, and the operator would silently get the cut-down viewer on their own
        server. Extra entries can be added via api.local_hosts."""
        import socket
        out = {"127.0.0.1", "::1", "localhost", "testclient"}
        try:
            out.update(socket.gethostbyname_ex(socket.gethostname())[2])
        except OSError:                       # no DNS / odd network stack — loopback only
            pass
        extra = _cfg("api", "local_hosts", default=None) or []
        if isinstance(extra, (list, tuple)):
            out.update(str(h) for h in extra)
        return frozenset(out)

    # Filled on first request, not here: _cfg() is defined further down in create_app, and
    # resolving this eagerly would read it before it exists.
    _LOCAL_HOSTS: set = set()

    def _is_local(client_host: str) -> bool:
        if not _LOCAL_HOSTS:
            _LOCAL_HOSTS.update(_local_hosts())
        return client_host in _LOCAL_HOSTS

    @app.middleware("http")
    async def _remote_viewer_only(request, call_next):
        """Everything a remote client sees is decided here.

        Kept in ONE place, and out of the route signatures, for a concrete reason: this
        module does `from __future__ import annotations` and imports FastAPI lazily, so a
        `request: Request` annotation on a route resolves to a bare string that FastAPI
        reads as a QUERY PARAMETER -- every such route starts returning 422. Middleware
        takes the request positionally and sidesteps that entirely.
        """
        if not bool(_cfg("api", "remote_viewer_only", default=True)):
            return await call_next(request)
        client = request.client.host if request.client else ""
        if _is_local(client):
            return await call_next(request)

        from fastapi.responses import HTMLResponse as _HTML, JSONResponse, RedirectResponse
        path = request.url.path.rstrip("/") or "/"
        method = request.method.upper()

        if method == "GET" and path == "/":
            # A tech types the bare address; send them to what they came for.
            return RedirectResponse("/trends", status_code=307)

        if method == "GET" and path == "/trends":
            # Serve the viewer with its nav and rename controls hidden. Renaming is not a
            # cosmetic concern: display_name FEEDS THE ROLE CLASSIFIER, so a visitor
            # renaming a point would quietly change what the analysis measures. The write
            # endpoint is blocked below regardless; this stops the page offering a control
            # that cannot work.
            html = (_STATIC / "trends.html").read_text(encoding="utf-8")
            return _HTML(html.replace("<body>", '<body class="viewer-only">', 1))

        allowed = (method == "GET" and path in _VIEWER_GET) or \
                  (method == "POST" and path in _VIEWER_POST)
        if not allowed:
            # 404, not 403: a remote client has no need to learn what else is here, and a
            # 403 invites someone to go looking for a token that does not exist.
            return JSONResponse({"detail": "not found"}, status_code=404)
        return await call_next(request)

    # ── the outer gate: which names, which sites, how much (2026-11-06 review) ──
    # Registered AFTER _remote_viewer_only, so it runs FIRST (Starlette: last added =
    # outermost). The viewer-only gate above trusts where a request comes FROM; on its own
    # that let any web page opened on this computer act as its operator by DNS rebinding:
    # its own name re-pointed at 127.0.0.1 makes its requests local AND same-origin, and
    # it could change or delete the phone-alert topic or download the database (H1).
    _BODY_MAX = 512 * 1024      # the largest real body is a saved view or a long sequence text
    _HOSTS: dict = {"names": frozenset(), "at": -1e9}
    _REFUSED: set = set()

    def _allowed_hosts() -> frozenset:
        """The names this server answers to: loopback, this computer's names and addresses,
        and api.allowed_hosts (a DNS alias the techs use). `testserver` is the test
        client's; no public DNS name can be it."""
        import socket
        names = {"localhost", "127.0.0.1", "::1", "testserver", *_local_hosts()}
        try:
            names.update({socket.gethostname(), socket.getfqdn()})
        except OSError:
            pass
        extra = _cfg("api", "allowed_hosts", default=None) or []
        if isinstance(extra, (list, tuple)):
            names.update(str(h) for h in extra)
        return frozenset(str(n).strip().lower().rstrip(".") for n in names if n)

    def _host_ok(host_header: str) -> bool:
        h = (host_header or "").strip().lower()
        if h.startswith("["):                              # [::1]:8000
            name = h[1:h.find("]")] if "]" in h else h
        else:
            name = h.rsplit(":", 1)[0] if h.count(":") == 1 else h
        name = name.rstrip(".")
        if name in _HOSTS["names"]:
            return True
        # First request, or this computer's addresses changed (DHCP): look again, at most
        # once a minute, so a stream of foreign names cannot make every request resolve.
        now = time.monotonic()
        if now - _HOSTS["at"] > 60:
            _HOSTS.update(names=_allowed_hosts(), at=now)
            return name in _HOSTS["names"]
        return False

    def _cross_site(request) -> Optional[str]:
        """Why a request that changes something must be refused, or None. A page on another
        site can POST here with no preflight (a form, a no-cors fetch) and, with login off,
        start billed AI reports, building-wide sweeps or backup rotations (M1). Browsers say
        where a request comes from; curl and scripts on this computer send neither header."""
        site = (request.headers.get("sec-fetch-site") or "").lower()
        if site and site not in ("same-origin", "none"):
            return f"a {site} request"
        origin = request.headers.get("origin")
        if origin is not None:
            from urllib.parse import urlsplit
            if origin == "null" or urlsplit(origin).netloc.lower() != \
                    (request.headers.get("host") or "").lower():
                return "a request from another origin"
        return None

    @app.middleware("http")
    async def _gate(request, call_next):
        from fastapi.responses import JSONResponse
        if not _host_ok(request.headers.get("host", "")):
            host = (request.headers.get("host") or "")[:100]
            if host not in _REFUSED and len(_REFUSED) < 50:   # once per name, so it can be found
                _REFUSED.add(host)
                logger.warning("refused a request for the host name %r: if that is this server, "
                               "add it to api.allowed_hosts in config.yaml", host)
            return JSONResponse({"detail": "unknown host name; add it to api.allowed_hosts "
                                           "in config.yaml if it is this server's"},
                                status_code=421)
        method = request.method.upper()
        if method not in ("GET", "HEAD", "OPTIONS"):
            why = _cross_site(request)
            if why:
                return JSONResponse({"detail": f"refused {why}"}, status_code=403)
            # Read and parsed whole, on the loop, before any route can cap it (M10).
            length = request.headers.get("content-length")
            try:
                too_big = length is not None and int(length) > _BODY_MAX
            except ValueError:
                too_big = True
            if too_big:
                return JSONResponse({"detail": "request body too large"}, status_code=413)
            if length is None and "chunked" in (request.headers.get("transfer-encoding") or ""):
                return JSONResponse({"detail": "send a Content-Length"}, status_code=411)
        response = await call_next(request)
        # No other site may frame these pages (clickjacking the Settings page), sniff a type,
        # or learn the page a link was followed from.
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    # ── page ─────────────────────────────────────────────────────────────────
    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return (_STATIC / "index.html").read_text(encoding="utf-8")

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard_page() -> str:
        return (_STATIC / "dashboard.html").read_text(encoding="utf-8")

    @app.get("/trends", response_class=HTMLResponse)
    async def trends() -> str:
        return (_STATIC / "trends.html").read_text(encoding="utf-8")

    @app.get("/reports", response_class=HTMLResponse)
    async def reports_page() -> str:
        return (_STATIC / "reports.html").read_text(encoding="utf-8")

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page() -> str:
        return (_STATIC / "settings.html").read_text(encoding="utf-8")

    @app.get("/coverage", response_class=HTMLResponse)
    async def coverage_page() -> str:
        return (_STATIC / "coverage.html").read_text(encoding="utf-8")

    @app.get("/health")
    async def health() -> Any:
        """503 whenever collection was ASKED FOR and is not running -- a bind that has not
        succeeded yet, or a collector task that died. This used to answer a flat 200 "ok"
        with `collector_running: false` sitting right beside it, which is a health check
        that reports the web server rather than the thing the program exists to do."""
        from fastapi.responses import JSONResponse
        task = getattr(app.state, "collector_task", None)
        running = task is not None and not task.done()
        requested = bool(getattr(app.state, "bind_requested", False))
        body = {"status": "ok" if (running or not requested) else "degraded",
                "bacnet": getattr(app.state, "bacnet", None) is not None,
                "collector_running": running,
                "bind_error": getattr(app.state, "bind_error", None)}
        return JSONResponse(body, status_code=503) if (requested and not running) else body

    @app.get("/api/poll-health")
    async def poll_health() -> dict[str, Any]:
        """Per-device last-cycle result + DB size / sample extent — 'trust the data'
. Local DB only; works even in browse-only mode.

        The sample count is a full index walk (10-23 s at 43.8 M rows, 2026-10-20), so it
        runs in a worker thread on its own read-only connection. It used to run here, on the
        loop the BACnet collector polls from."""
        import os
        from ..storage.sqlite_dal import samples_extent_readonly
        ext = await asyncio.to_thread(samples_extent_readonly, app.state.dal.db_path)
        try:
            db_bytes = os.path.getsize(app.state.config.storage.path)
        except OSError:
            db_bytes = None
        task = app.state.collector_task
        return {"devices": app.state.dal.poll_health_latest(),
                "samples": ext.get("n"), "oldest_sample_utc": ext.get("min_ts"),
                "newest_sample_utc": ext.get("max_ts"), "db_bytes": db_bytes,
                "bacnet": app.state.bacnet is not None,
                "collector_running": task is not None and not task.done()}

    @app.post("/api/model/rebuild")
    async def model_rebuild(authorization: str = Header(default=""),
                            x_auth_token: str = Header(default="")) -> dict[str, Any]:
        """Re-classify point roles and apply the plant limits, so a newly enabled point
        reaches the alarm rules, the dashboard and the by-metric list without waiting for
        the daily job (ai/model.py; the baselines only with ai.baselines). Local DB only,
        no BACnet. About 2 s."""
        _require_auth(authorization, x_auth_token)   # CPU-heavy full-DB recompute
        from ..ai import model
        return {"status": "ok", **model.rebuild(app.state.dal, app.state.config)}

    # ── settings / secrets / backup (Settings tab, plan §11) ─────────────────
    def _cfg(*path: str, default: Any = None) -> Any:
        # getattr, not attribute access: the callers' `default=` is often a FAIL-CLOSED
        # choice (_remote_viewer_only defaults to True), and that intent can only hold if a
        # missing config falls back to it instead of raising. app.state.config is set by the
        # lifespan, so a request that arrives before it -- a TestClient used without its
        # context manager, a crashed startup -- would otherwise 500 rather than fail closed.
        node: Any = getattr(app.state, "config", None)
        if node is None:
            return default
        for p in path:
            node = node.get(p) if hasattr(node, "get") else None
            if node is None:
                return default
        return node

    def _require_auth(authorization: str = "", x_auth_token: str = "") -> None:
        """Gate sensitive mutations. When api.auth.enabled, require a bearer token that
        matches env HVACTREND_UI_TOKEN; otherwise allow (trusted-VLAN default, warned at
        startup). The read-only BACnet posture is enforced separately and never settable."""
        if not bool(_cfg("api", "auth", "enabled", default=False)):
            return
        import hmac
        token = os.environ.get("HVACTREND_UI_TOKEN")
        if not token:
            raise HTTPException(status_code=503,
                                detail="api.auth.enabled but HVACTREND_UI_TOKEN is not set")
        sent = authorization[7:] if authorization.lower().startswith("bearer ") else x_auth_token
        if not hmac.compare_digest(sent, token):
            raise HTTPException(status_code=401, detail="unauthorized")

    @app.get("/api/settings")
    async def get_settings(authorization: str = Header(default=""),
                           x_auth_token: str = Header(default="")) -> dict[str, Any]:
        _require_auth(authorization, x_auth_token)   # discloses config + secret last4 + overrides
        from .. import secrets_store
        from ..config import is_settable
        eff: dict[str, Any] = {}

        def _flatten(d: dict, prefix: str = "") -> None:
            for k, v in d.items():
                key = f"{prefix}.{k}" if prefix else k
                if isinstance(v, dict):
                    _flatten(v, key)
                elif is_settable(key):
                    eff[key] = v

        _flatten(app.state.config.as_dict())
        from .. import notify
        return {"effective": eff,
                "overrides": sorted(app.state.dal.all_settings().keys()),
                "secrets": secrets_store.status(),
                "notify": notify.status(app.state.dal, app.state.config),
                "note": "Settings apply on next server restart; API keys apply immediately."}

    @app.put("/api/settings")
    async def put_settings(payload: dict = Body(...),
                           authorization: str = Header(default=""),
                           x_auth_token: str = Header(default="")) -> dict[str, Any]:
        _require_auth(authorization, x_auth_token)
        from ..config import is_settable
        updates = payload.get("settings")
        if updates is None and "key" in payload:
            updates = {payload["key"]: payload.get("value")}
        if not isinstance(updates, dict):
            raise HTTPException(status_code=400,
                                detail="body: {settings:{key:value}} or {key,value}")
        from ..config import setting_problem
        applied, rejected, why = [], [], {}
        for key, value in updates.items():
            # The VALUE is checked too, against the type of what it replaces: a string where a
            # number was meant used to be stored, then stopped the collector at the next
            # restart, reported as a BACnet failure (2026-10-27 review).
            problem = setting_problem(app.state.config, key, value) if is_settable(key) \
                else "not settable here"   # e.g. anything under bacnet.*
            if problem:
                rejected.append(key)
                why[key] = problem
            else:
                app.state.dal.set_setting(key, value)
                applied.append(key)
        return {"applied": applied, "rejected": rejected, "why": why,
                "restart_required": bool(applied)}

    @app.delete("/api/settings/{key}")
    async def revert_setting(key: str, authorization: str = Header(default=""),
                             x_auth_token: str = Header(default="")) -> dict[str, Any]:
        _require_auth(authorization, x_auth_token)
        app.state.dal.delete_setting(key)
        return {"ok": True, "reverted": key, "restart_required": True}

    @app.post("/api/settings/secrets")
    async def set_secret_ep(payload: dict = Body(...),
                            authorization: str = Header(default=""),
                            x_auth_token: str = Header(default="")) -> dict[str, Any]:
        _require_auth(authorization, x_auth_token)
        from .. import secrets_store
        name, value = payload.get("name"), payload.get("value")
        if not name or value in (None, ""):
            raise HTTPException(status_code=400, detail="body: {name, value}")
        try:
            secrets_store.set_secret(str(name), str(value))
        except KeyError:
            raise HTTPException(status_code=400,
                                detail=f"unknown secret; allowed: {sorted(secrets_store.SECRET_ENV)}")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"ok": True, "secrets": secrets_store.status()}

    @app.delete("/api/settings/secrets/{name}")
    async def clear_secret_ep(name: str, authorization: str = Header(default=""),
                              x_auth_token: str = Header(default="")) -> dict[str, Any]:
        _require_auth(authorization, x_auth_token)
        from .. import secrets_store
        try:
            secrets_store.clear_secret(name)
        except KeyError:
            raise HTTPException(status_code=400, detail="unknown secret")
        return {"ok": True, "secrets": secrets_store.status()}

    @app.post("/api/notify/test")
    async def notify_test(authorization: str = Header(default=""),
                          x_auth_token: str = Header(default="")) -> dict[str, Any]:
        """Publish a test notification to the ntfy topic, so delivery can be SEEN to work.

        ntfy answers 200 once it has accepted the message; whether it shows up depends on the
        phone being subscribed to the topic, which is exactly what this button checks. The
        HTTP call runs in a worker thread (up to 10 s) so it cannot stall polling on the
        shared loop. Local-only: the remote-viewer middleware admits no write route. The
        response never carries the topic -- every detail notify.post returns is redacted."""
        _require_auth(authorization, x_auth_token)
        from .. import notify
        tgt = notify.target(app.state.config)
        if not tgt:
            raise HTTPException(status_code=400,
                                detail="No ntfy topic is set: generate one on this page first.")
        now = int(time.time())
        ok, detail = await asyncio.to_thread(notify.post, tgt, notify.test_message(now))
        notify.record(app.state.dal, ok, detail, now, kind="test")
        return {"ok": ok, "detail": detail,
                "notify": notify.status(app.state.dal, app.state.config)}

    @app.post("/api/backup")
    async def make_backup(authorization: str = Header(default=""),
                          x_auth_token: str = Header(default="")) -> dict[str, Any]:
        _require_auth(authorization, x_auth_token)
        bdir = Path(_cfg("storage", "backup", "dir", default="data/backups"))
        keep = int(_cfg("storage", "backup", "keep", default=7) or 0)
        bdir.mkdir(parents=True, exist_ok=True)
        name = "hvactrend." + time.strftime("%Y%m%d_%H%M%S") + ".db"
        dest = bdir / name
        # Same contract as the scheduled backup: VERIFY, and never prune behind an archive
        # that has not been verified. This was the only backup path in the program that
        # wrote without checking -- and the only one that deleted a verified archive in the
        # same breath. It is also the one a human triggers when they are already worried.
        from ..collector.service import _BACKUP_LOCK, _backup_db_file, _prune_backups
        src_path = getattr(app.state.dal, "db_path", None) or _cfg("storage", "path")
        # One copy at a time, across the scheduled job and this route. Without this check
        # the copy function would refuse anyway, and the caller would report the refusal as
        # "failed verification" -- the wrong alarm for a button press. (A8)
        if _BACKUP_LOCK.locked():
            raise HTTPException(status_code=409,
                                detail="a database backup is already running; try again later")

        # FREE-SPACE PRECONDITION, as the scheduled path has. Every copy lands on the same
        # volume as the live database, so a backup is the fastest way to fill the disk the
        # collector is writing to -- and a human clicking this is usually already worried,
        # which is the worst moment to take the volume down. (audit L-3: the F-04 pattern,
        # where the manual route skipped a check the scheduled one does.)
        import shutil
        min_free = int(_cfg("storage", "backup", "min_free_bytes", default=0) or 0)
        try:
            free = shutil.disk_usage(str(bdir)).free
            need = int(Path(str(src_path)).stat().st_size * 1.1)
        except OSError:
            free = need = 0
        if free and (free < need or (min_free and free - need < min_free)):
            raise HTTPException(
                status_code=507,
                detail=("not enough free space: %.1f GB free, the copy needs %.1f GB and "
                        "the configured floor is %.1f GB"
                        % (free / 1e9, need / 1e9, min_free / 1e9)))

        rows = await asyncio.to_thread(_backup_db_file, str(src_path), str(dest))
        if rows is None:
            # Nothing to unlink: a failed copy never reaches a name the glob can see.
            raise HTTPException(status_code=500,
                                detail="backup failed verification and was discarded")
        # Shared with the scheduled path, so the shrink guard and mtime ordering apply
        # here too, and off the loop because it reads the previous archive.
        removed = await asyncio.to_thread(_prune_backups, bdir, keep, dest, rows)
        return {"ok": True, "file": name, "bytes": dest.stat().st_size,
                "samples": rows, "removed": removed}

    @app.get("/api/views")
    async def list_views(kind: str = "trend") -> dict[str, Any]:
        """Saved chart views. Readable by a remote tech on purpose -- a view built at the
        desk is exactly what should reach the phone in the mechanical room, which was the
        whole problem with keeping them in localStorage."""
        out = {}
        for r in app.state.dal.list_views(kind):
            try:
                out[r["name"]] = json.loads(r["spec"])
            except (ValueError, TypeError):
                continue
            out[r["name"]]["_updated_utc"] = r["updated_utc"]
        return {"views": out}

    @app.put("/api/views/{name}")
    async def put_view(name: str, payload: dict = Body(...),
                       authorization: str = Header(default=""),
                       x_auth_token: str = Header(default="")) -> dict[str, Any]:
        _require_auth(authorization, x_auth_token)
        name = (name or "").strip()
        if not name or len(name) > 120:
            raise HTTPException(status_code=400, detail="name must be 1-120 chars")
        spec = payload.get("spec", payload)
        if not isinstance(spec, dict):
            raise HTTPException(status_code=400, detail="spec must be an object")
        blob = json.dumps(spec, separators=(",", ":"))
        if len(blob) > 200_000:
            raise HTTPException(status_code=400, detail="spec too large")
        app.state.dal.save_view(name, blob)
        return {"ok": True, "name": name}

    @app.delete("/api/views/{name}")
    async def delete_view(name: str,
                          authorization: str = Header(default=""),
                          x_auth_token: str = Header(default="")) -> dict[str, Any]:
        _require_auth(authorization, x_auth_token)
        if not app.state.dal.delete_view(name):
            raise HTTPException(status_code=404, detail="unknown view")
        return {"deleted": name}

    @app.get("/api/backups")
    async def list_backups(authorization: str = Header(default=""),
                           x_auth_token: str = Header(default="")) -> dict[str, Any]:
        _require_auth(authorization, x_auth_token)
        bdir = Path(_cfg("storage", "backup", "dir", default="data/backups"))
        out = []
        if bdir.exists():
            for f in sorted(bdir.glob("hvactrend.*.db"), reverse=True):
                st = f.stat()
                out.append({"name": f.name, "bytes": st.st_size, "mtime_utc": int(st.st_mtime)})
        return {"backups": out}

    @app.get("/api/backups/{name}")
    async def download_backup(name: str, authorization: str = Header(default=""),
                              x_auth_token: str = Header(default="")):
        _require_auth(authorization, x_auth_token)
        from fastapi.responses import FileResponse
        if ("/" in name or "\\" in name or not name.startswith("hvactrend.")
                or not name.endswith(".db")):
            raise HTTPException(status_code=400, detail="bad backup name")
        f = Path(_cfg("storage", "backup", "dir", default="data/backups")) / name
        if not f.exists():
            raise HTTPException(status_code=404, detail="not found")
        return FileResponse(str(f), filename=name, media_type="application/octet-stream")

    @app.post("/api/weather/refresh")
    async def weather_refresh(authorization: str = Header(default=""),
                              x_auth_token: str = Header(default="")) -> dict[str, Any]:
        """Fetch + store the latest weather now (Tempest cloud if configured, else
        Open-Meteo). Runs in the server process, which holds TEMPEST_TOKEN in env."""
        _require_auth(authorization, x_auth_token)
        from ..ai import weather
        # the GET off the loop, the store on it (the collector does the same, M4)
        fetched = await asyncio.to_thread(weather.fetch_weather, app.state.config)
        return weather.store_weather(app.state.dal, fetched)

    # ── inventory browser (from the cached DB, no BACnet needed) ──────────────
    @app.get("/api/systems")
    async def systems() -> dict[str, Any]:
        return {"systems": app.state.dal.list_systems()}

    _OCC_MODES = ("scheduled", "vacant", "always")
    _DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

    def _valid_schedule(sched: Any) -> Optional[str]:
        """Validate a per-unit occupancy day-map: {day: null | [start_min, end_min]}.
        Returns a JSON string to store, or raises 400."""
        if sched is None:
            return None
        if sched == "" or sched == {}:
            return ""                      # explicit clear -> inherit building default
        if not isinstance(sched, dict):
            raise HTTPException(status_code=400, detail="occ_schedule must be an object")
        out: dict[str, Any] = {}
        for k, v in sched.items():
            if k not in _DAY_KEYS:
                raise HTTPException(status_code=400, detail=f"bad day key {k!r}")
            if v is None:
                out[k] = None
                continue
            if not (isinstance(v, (list, tuple)) and len(v) == 2):
                raise HTTPException(status_code=400,
                                    detail=f"{k} must be null or [start_min, end_min]")
            try:
                a, b = int(v[0]), int(v[1])
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail=f"{k} minutes must be integers")
            if not (0 <= a < b <= 1440):
                raise HTTPException(status_code=400,
                                    detail=f"{k} needs 0 <= start < end <= 1440 (minutes)")
            out[k] = [a, b]
        return json.dumps(out)

    @app.put("/api/systems/{system_id}/context")
    async def set_system_context(system_id: int, payload: dict = Body(default={}),
                                 authorization: str = Header(default=""),
                                 x_auth_token: str = Header(default="")) -> dict[str, Any]:
        """Set operator context on a unit: free-text `notes`, `occupancy_mode`
        ('scheduled' | 'vacant' | 'always'), and/or `occ_schedule` (per-unit hours as a
        day-map of minutes-from-midnight, e.g. {"mon":[330,1080],"sat":null}; {} clears it
        back to the building default). Fed to the AI and to the occ/unocc analysis split."""
        _require_auth(authorization, x_auth_token)
        notes = payload.get("notes")
        mode = payload.get("occupancy_mode")
        if mode is not None and mode not in _OCC_MODES:
            raise HTTPException(status_code=400,
                                detail=f"occupancy_mode must be one of {_OCC_MODES}")
        if notes is not None and not isinstance(notes, str):
            raise HTTPException(status_code=400, detail="notes must be a string")
        if notes is not None and len(notes) > 2000:
            raise HTTPException(status_code=400, detail="notes too long (max 2000 chars)")
        sched = _valid_schedule(payload.get("occ_schedule")) if "occ_schedule" in payload else None
        ok = app.state.dal.set_system_context(system_id, notes=notes, occupancy_mode=mode,
                                              occ_schedule=sched)
        if not ok:
            raise HTTPException(status_code=404, detail="unknown system")
        return {"system_id": system_id, "notes": notes, "occupancy_mode": mode,
                "occ_schedule": sched}

    # Equipment specs the operator maintains. Free-form keys are allowed (equipment_meta
    # is generic) — this list is what the editor UI shows by default.
    _EQUIP_NUM = {"tons", "compressors", "cooling_stages", "heating_stages",
                  "supply_fans", "design_cfm"}
    _EQUIP_UNITS = {"tons": "tons", "design_cfm": "cfm"}

    @app.get("/api/systems/equipment")
    async def systems_equipment() -> dict[str, Any]:
        """All per-unit equipment specs + sequence of operations, for the editor."""
        dal = app.state.dal
        eq = dal.equipment_by_system()
        return {"equipment": {str(k): v for k, v in eq.items()},
                "systems": [{"id": s["id"], "code": s["code"], "name": s.get("name"),
                             "sequence_of_operations": s.get("sequence_of_operations")}
                            for s in dal.list_systems()]}

    @app.put("/api/systems/{system_id}/equipment")
    async def set_system_equipment(system_id: int, payload: dict = Body(default={}),
                                   authorization: str = Header(default=""),
                                   x_auth_token: str = Header(default="")) -> dict[str, Any]:
        """Create/UPDATE/CLEAR a unit's equipment specs and sequence of operations. Fully
        editable: `specs` is {key: value|null|""} — a null/empty value DELETES that spec,
        and re-sending a key overwrites it. `sequence_of_operations` is free prose ("" clears).
        Numeric keys are stored as numbers so they can be compared/plotted later."""
        _require_auth(authorization, x_auth_token)
        specs = payload.get("specs")
        soo = payload.get("sequence_of_operations")
        if specs is not None and not isinstance(specs, dict):
            raise HTTPException(status_code=400, detail="specs must be an object")
        if soo is not None and not isinstance(soo, str):
            raise HTTPException(status_code=400, detail="sequence_of_operations must be a string")
        if soo is not None and len(soo) > 20000:
            raise HTTPException(status_code=400, detail="sequence_of_operations too long")
        dal = app.state.dal
        if not any(s["id"] == system_id for s in dal.list_systems()):
            raise HTTPException(status_code=404, detail="unknown system")

        written = []
        for key, val in (specs or {}).items():
            k = str(key).strip()
            if not k or len(k) > 60:
                raise HTTPException(status_code=400, detail=f"bad spec key {key!r}")
            if val is None or (isinstance(val, str) and not val.strip()):
                dal.set_equipment_meta(system_id, k)            # cleared
                written.append(k)
                continue
            if k in _EQUIP_NUM:
                try:
                    dal.set_equipment_meta(system_id, k, value_num=float(val),
                                           unit=_EQUIP_UNITS.get(k))
                except (TypeError, ValueError):
                    raise HTTPException(status_code=400, detail=f"{k} must be numeric")
            else:
                dal.set_equipment_meta(system_id, k, value_text=str(val)[:500])
            written.append(k)
        if soo is not None:
            dal.set_system_context(system_id, sequence_of_operations=soo)
        return {"system_id": system_id, "specs_written": written,
                "sequence_of_operations": soo}

    @app.get("/api/systems/schedule-detect")
    async def schedule_detect(days: int = 14) -> dict[str, Any]:
        """Derive each unit's REAL occupancy hours from trended history (its own
        Occupancy Status point where available, else fan speed with cold-weather
        freeze-circulation excluded). Read-only — proposals for the operator to confirm,
        never auto-applied. Days without enough coverage report `insufficient`."""
        # A worker thread on its own read-only connection: 0.9 s for 14 days and 7.2 s for
        # 90, on the loop the collector polls from, held /health at a second (2026-10-27).
        return await asyncio.to_thread(_schedule_detect_ro, str(app.state.dal.db_path),
                                       app.state.config, max(1, min(int(days), 90)))

    # Key readings for the building-wide comparison matrix, in column order. Chosen for
    # cross-unit comparison of near-identical AHUs, not completeness — the per-unit
    # dashboard has everything.
    _FLEET_COLS = [("DAT", "Disch Air"), ("MAT", "Mixed Air"), ("RAT", "Return Air"),
                   ("FAN_SPEED", "Fan %"), ("OA_DAMPER", "OA Dmpr"),
                   ("DUCT_PRESS", "Duct SP")]

    def _overrides() -> list[dict]:
        """Points under an override right now, oldest first (bacnet/priority.py), each with
        its level's name, its value as an operator would say it, a one-line text, and
        `expected` (the reason) when site knowledge says it is held on purpose. [] until the
        first priority read."""
        from .. import coverage
        dal = app.state.dal
        if not dal.has_table("point_command"):
            return []
        try:
            facts = coverage.site_facts(app.state.config.get("site_knowledge"))
        except ValueError:            # malformed knowledge: flag everything, as without it
            facts = None
        return [dict(r, level_name=_priority.level_name(r["level"]),
                     value=_priority.shown_value(r), text=_priority.describe(r))
                for r in _priority.mark_expected(dal.overrides(), facts)]

    def _live_alerts() -> list[dict]:
        """Open alerts from a check that still runs: the watchdog's always, the pre-screen's
        only while alerts.enabled is on. Two pre-screen rows were left open when it was
        switched off on 2026-09-15. Nothing re-checks them, so on 10-27 they were still
        "2 active alerts" on the reports page and alert counts on the dashboard, six weeks
        after they were last seen. The check-in and the watchdog already count only the
        watchdog's; every page goes through here."""
        rows = app.state.dal.open_alerts()
        if bool(_cfg("alerts", "enabled", default=False)):
            return rows
        return [a for a in rows if a.get("source") == "watchdog"]

    def _after_hours(days: float, system_id: Optional[int] = None) -> list[dict]:
        dal = app.state.dal
        if not dal.has_table("point_command"):
            return []
        now = int(time.time())
        rows = dal.after_hours_sessions(now - int(days * 86400), now + 1)
        if system_id is not None:
            sysrow = next((s for s in dal.list_systems() if s["id"] == system_id), None)
            rows = [r for r in rows if sysrow and r["system_code"] == sysrow["code"]]
        return [dict(r, text=_priority.session_text(r)) for r in rows]

    @app.get("/api/after-hours")
    async def after_hours(days: float = 7) -> dict[str, Any]:
        """Units run outside their schedule by an occupancy override (a thermostat's button),
        oldest first (DAL.after_hours_sessions). Local only, like /api/overrides."""
        return {"days": days, "sessions": _after_hours(max(0.0, min(float(days), 92.0)))}

    @app.get("/api/overrides")
    async def overrides_now() -> dict[str, Any]:
        """Who has overridden what right now, and when the priority arrays were last read.
        In _VIEWER_GET: the trend viewer polls it to keep its OVR marks current (2026-10-27;
        before, a mark changed only when the page was reloaded)."""
        last = app.state.dal.get_meta("priority.last_read_utc")
        return {"overrides": _overrides(), "last_read_utc": int(last) if last else None}

    @app.get("/api/overview")
    async def fleet_overview(roles: Optional[str] = None) -> dict[str, Any]:
        """The building landing view: one row per unit, one column per key reading, plus
        each unit's open alert / finding / override counts. Read-only; no BACnet.

        `roles` (comma-separated) lets the operator choose which columns to show; it is
        intersected with the roles that actually have canonical points, so a stale saved
        selection can never produce empty columns. Absent/empty -> the default set."""
        dal = app.state.dal
        now = int(time.time())
        avail = dal.canonical_roles()
        avail_set = {r["role"] for r in avail}
        default_roles = [r for r, _ in _FLEET_COLS if r in avail_set] or [r for r, _ in _FLEET_COLS]
        if roles:
            want = [r.strip() for r in roles.split(",") if r.strip()]
            chosen = [r for r in want if r in avail_set]      # drop unknown / no-data roles
        else:
            chosen = []
        role_list = chosen or default_roles
        fo = dal.fleet_overview(role_list, now_utc=now)
        cells_by_sys = fo["cells_by_system"]

        alert_n: dict[str, int] = {}
        for a in _live_alerts():
            alert_n[a.get("system_code")] = alert_n.get(a.get("system_code"), 0) + 1
        find_n: dict[Any, int] = {}
        for f in dal.list_findings(status="open", limit=500):
            find_n[f.get("system_id")] = find_n.get(f.get("system_id"), 0) + 1
        over_n: dict[Any, int] = {}
        for o in _overrides():
            if not o["expected"]:          # held on purpose is not something to look at
                over_n[o.get("system_id")] = over_n.get(o.get("system_id"), 0) + 1

        units = []
        for s in dal.list_systems():
            units.append({
                "id": s["id"], "code": s["code"], "name": s.get("name"),
                "occupancy_mode": s.get("occupancy_mode"),
                "alerts": alert_n.get(s["code"], 0),
                "findings": find_n.get(s["id"], 0),
                "overrides": over_n.get(s["id"], 0),
                "cells": cells_by_sys.get(s["id"], {}),
            })
        labels = dict(_FLEET_COLS)
        return {"now_utc": now,
                "columns": [{"role": r, "label": labels.get(r, r)} for r in role_list],
                "available_roles": avail,      # [{role, units}] — the column-picker menu
                "default_roles": default_roles,
                "units": units}

    @app.get("/api/systems/{system_id}/overview")
    async def system_overview(system_id: int) -> dict[str, Any]:
        """One round-trip for the unit dashboard: this unit's live points (last value +
        quality + age + 24 h sparkline, grouped client-side by role) plus its open alerts,
        open findings, active operator-log entries, occupancy context and equipment specs.
        Read-only; no BACnet.

        No health grade since 2026-10-20 (the operator: "we dont need them anymore"). The grades
        came from the old AI detection layer and had been frozen at its last run, 09-14."""
        dal = app.state.dal
        sysrow = next((s for s in dal.list_systems() if s["id"] == system_id), None)
        if not sysrow:
            raise HTTPException(status_code=404, detail="unknown system")
        code = sysrow.get("code")
        now = int(time.time())
        ov = dal.system_overview(system_id, now_utc=now)

        # alerts + findings + active operator notes scoped to this unit
        alerts = [a for a in _live_alerts() if a.get("system_code") == code]
        findings = dal.list_findings(system_id=system_id, status="open", limit=50)
        notes = [a for a in dal.annotations_overlapping(now - 86400, now + 1)
                 if a.get("system_id") in (system_id, None)]
        equip = (dal.equipment_by_system() or {}).get(system_id, {})
        over = [o for o in _overrides() if o.get("system_id") == system_id]
        by_pt = {o["point_id"]: o for o in over}
        for p in ov["points"]:
            if p.get("id") in by_pt:
                p["override"] = by_pt[p["id"]]
        return {
            "system": {"id": system_id, "code": code, "name": sysrow.get("name"),
                       "kind": sysrow.get("kind"),
                       "occupancy_mode": sysrow.get("occupancy_mode"),
                       "occ_schedule": sysrow.get("occ_schedule"),
                       "notes": sysrow.get("notes"),
                       "sequence_of_operations": sysrow.get("sequence_of_operations")},
            "alerts": alerts, "findings": findings,
            "operator_notes": notes, "equipment": equip, "overrides": over,
            "after_hours": _after_hours(7, system_id),
            "points": ov["points"], "now_utc": now,
        }

    @app.get("/api/systems/{system_id}/points")
    async def system_points(system_id: int, q: Optional[str] = None,
                            loggable: bool = False) -> dict[str, Any]:
        pts = app.state.dal.points_for_system(system_id, q=q, loggable_only=loggable)
        return {"system_id": system_id, "count": len(pts), "points": pts}

    @app.post("/api/points/{point_id}/enabled")
    async def set_enabled(point_id: int, enabled: bool = Body(..., embed=True),
                          authorization: str = Header(default=""),
                          x_auth_token: str = Header(default="")) -> dict[str, Any]:
        """Select/deselect a point for logging. Local DB write — NOT a BACnet write."""
        _require_auth(authorization, x_auth_token)   # changes what the plant records
        app.state.dal.set_point_enabled(point_id, enabled)
        return {"point_id": point_id, "enabled": enabled}

    # ── live preview reads (the disambiguation tool, plan §9.1) ───────────────
    @app.post("/api/points/{point_id}/label")
    async def set_point_label(point_id: int, payload: dict = Body(default={}),
                              authorization: str = Header(default=""),
                              x_auth_token: str = Header(default="")) -> dict[str, Any]:
        """Operator's own name + note for a point.

        BACnet names are commissioning strings, not labels: on the tower panel the reading
        and its setpoint are `SITE.CHW.HDR.SUP.TEMP` and `SITE.CHW.HDR.SUP.TEMP.SP` — two
        characters apart and indistinguishable at a glance on a chart legend. Send
        `display_name` to rename and/or `operator_note` to annotate; omit a field to leave
        it alone, send "" to clear it back to the BACnet name."""
        _require_auth(authorization, x_auth_token)
        dn = payload.get("display_name")
        note = payload.get("operator_note")
        for label, val in (("display_name", dn), ("operator_note", note)):
            if val is not None and not isinstance(val, str):
                raise HTTPException(status_code=400, detail=f"{label} must be a string")
            if isinstance(val, str) and len(val) > 200:
                raise HTTPException(status_code=400, detail=f"{label} too long (max 200)")
        ok = app.state.dal.set_point_labels(point_id, display_name=dn, operator_note=note)
        if not ok:
            raise HTTPException(status_code=404, detail="unknown point")
        meta = app.state.dal.point_meta(point_id)
        return {"point_id": point_id, "display_name": meta.get("display_name"),
                "operator_note": meta.get("operator_note"), "name": meta.get("name")}

    # The three endpoints below EMIT BACNET TRAFFIC to the plant, so they are gated exactly
    # like /api/discover. They were not, which mattered for one specific reason: the plan of
    # record is to enable api.auth before ever opening the firewall, and at that moment these
    # would have been the only unauthenticated way to put load on the BAS network --
    # device_objects on a 2000-object panel is a ~4 minute operation. The browser already
    # sends the token on every request (index.html j()), so gating costs the UI nothing.
    # POST, not GET, since 2026-11-06: a GET that reads the plant can be fired by an <img>
    # on any web page opened on this computer (review M1).
    @app.post("/api/points/{point_id}/read")
    async def read_one(point_id: int, authorization: str = Header(default=""),
                       x_auth_token: str = Header(default="")) -> dict[str, Any]:
        _require_auth(authorization, x_auth_token)   # live read on the plant network
        targets = app.state.dal.points_read_targets([point_id])
        if not targets:
            raise HTTPException(status_code=404, detail="unknown point")
        results = await _read_points_live(_bacnet(), targets)
        return results.get(point_id, {"id": point_id, "error": "no-response"})

    @app.post("/api/points/read")
    async def read_many(ids: list[int] = Body(..., embed=True),
                        authorization: str = Header(default=""),
                        x_auth_token: str = Header(default="")) -> dict[str, Any]:
        _require_auth(authorization, x_auth_token)   # live read on the plant network
        if len(ids) > 2000:
            raise HTTPException(status_code=413, detail="at most 2000 points per read")
        targets = app.state.dal.points_read_targets(ids)
        results = await _read_points_live(_bacnet(), targets)
        return {"results": [results.get(i, {"id": i, "error": "no-response"}) for i in ids]}

    # ── point coverage review (coverage.py, ROADMAP Phase 0.5) ───────────────
    @app.get("/api/coverage")
    async def coverage_view(authorization: str = Header(default=""),
                            x_auth_token: str = Header(default="")) -> dict[str, Any]:
        """The review from the database only. The live check is POST: a GET that reads the
        plant can be fired by an <img> on any web page opened on this computer (M1)."""
        return await coverage_review(False, authorization, x_auth_token)

    @app.post("/api/coverage")
    async def coverage_review(live: bool = False, authorization: str = Header(default=""),
                              x_auth_token: str = Header(default="")) -> dict[str, Any]:
        """Which points each monitoring rule needs, unit by unit, and what is logged.

        The config's `site_knowledge` sets aside the points the operator ruled out. live=true also
        reads every point the review would offer from its controller first, and sets aside
        any the controller itself reports unreliable: a Trane profile point the unit's
        program does not drive reads 'unreliable-other' (AC-1's Outdoor Air Damper Position,
        2026-09-29). That is about a hundred reads, batched per device -- the same kind of
        read as "read visible points". Without a BACnet stack the review still comes back,
        with `live_error` saying why it was not checked."""
        _require_auth(authorization, x_auth_token)   # live=true reads the plant network
        from .. import coverage
        dal = app.state.dal
        pts = dal.coverage_points()
        knowledge = app.state.config.get("site_knowledge")
        try:
            report = coverage.build(pts, knowledge)
        except ValueError as exc:       # a malformed site_knowledge entry: name it
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        report["live_checked"] = None
        if live:
            ids = sorted({c["id"] for s in report["systems"] for c in s["suggest"]}
                         | {c["id"] for c in report["unassigned_suggest"]})
            skipped = max(0, len(ids) - _COVERAGE_LIVE_MAX)
            ids = ids[:_COVERAGE_LIVE_MAX]
            if app.state.bacnet is None:
                report["live_error"] = "BACnet stack not available (browse-only mode)"
            elif ids:
                reads = await _read_points_live(_bacnet(), dal.points_read_targets(ids))
                report = dict(coverage.build(pts, knowledge, reads), live_checked=len(ids),
                              live_skipped=skipped)
            else:
                report["live_checked"] = 0
        report["built_utc"] = int(time.time())
        return report

    # ── discovery (re-sweep / re-enumerate) — shared stack, plan §9.1 ─────────
    @app.post("/api/discover")
    async def discover(timeout: float = 3.0, directed: str | None = None,
                       cache: bool = True,
                       low: int | None = None, high: int | None = None,
                       objects: bool = True,
                       authorization: str = Header(default=""),
                       x_auth_token: str = Header(default="")) -> dict[str, Any]:
        """Who-Is sweep, then enumerate each device found.

        An UNBOUNDED call is a whole-building operation: the Who-Is is a single broadcast,
        but run_discovery then enumerates EVERY device that answered — on this site that is
        70+ devices including every VAV controller, and one 2000-object panel alone takes
        ~4 minutes. Use `low`/`high` to Who-Is a single instance (the normal case when
        commissioning ONE new panel: `?low=100300&high=100300`), and `objects=false` to
        locate a device without reading its object list at all."""
        _require_auth(authorization, x_auth_token)   # network sweep + inventory writes
        bacnet = _bacnet()
        async with _one_sweep("a discovery"):
            cfg = app.state.config
            site = cfg.get("site")
            site_id = app.state.dal.ensure_site(
                site.get("code", "SITE"), site.get("name", "Site"),
                site.get("timezone", "America/New_York"))
            results = await run_discovery(bacnet, app.state.dal if cache else None,
                                          site_id=site_id, address=directed,
                                          low_limit=low, high_limit=high,
                                          enumerate_objects=objects,
                                          timeout=timeout, cache=cache)
        return {"devices": [_device_json(r) for r in results]}

    # POST since 2026-11-06 (it was a GET an <img> could fire: M1).
    @app.post("/api/devices/{device_instance}/objects")
    async def device_objects(device_instance: int, authorization: str = Header(default=""),
                             x_auth_token: str = Header(default="")) -> dict[str, Any]:
        _require_auth(authorization, x_auth_token)   # full object enumeration: minutes of traffic
        row = app.state.dal.device_address(device_instance)
        if not row:
            raise HTTPException(status_code=404, detail="device not discovered yet")
        dd = DiscoveredDevice(device_instance=device_instance, address=row["address"],
                              network_number=row.get("network_number"))
        bacnet = _bacnet()
        async with _one_sweep("a device read"):
            objects = await enumerate_device(bacnet, dd)
        return {"device_instance": device_instance,
                "objects": [_object_json(o) for o in objects]}

    # One sweep of the plant network at a time -- a discovery, a full device read, a rescan.
    # Each is minutes of reads at worst; two at once only double the load on the controllers.
    _SWEEP = asyncio.Lock()

    @asynccontextmanager
    async def _one_sweep(what: str):
        st = getattr(app.state, "rescan", None) or {}
        if _SWEEP.locked() or st.get("state") == "running":
            raise HTTPException(status_code=409, detail=f"refused {what}: another sweep of "
                                                        "the controllers is running")
        async with _SWEEP:
            yield

    # ── rescan one controller (Point Selection's button; rescan.py) ───────────
    # The object list is read on the shared stack while the collector keeps polling, so
    # nothing is stopped. One at a time: a Tracer SC is ~5,500 objects, a minute or two of
    # reads, and two at once would only double the load on the controllers.
    def _rescan_state() -> dict:
        st = getattr(app.state, "rescan", None)
        if st is None:
            st = app.state.rescan = {"state": "idle"}
        return st

    @app.get("/api/rescan/controllers")
    async def rescan_controllers() -> dict[str, Any]:
        from .. import rescan as _rescan
        return {"controllers": await asyncio.to_thread(
            _rescan.controllers, str(app.state.dal.db_path), app.state.config),
                "bacnet": getattr(app.state, "bacnet", None) is not None}

    @app.get("/api/rescan")
    async def rescan_status() -> dict[str, Any]:
        st = _rescan_state()
        return {k: v for k, v in st.items() if k != "task"}

    @app.post("/api/rescan")
    async def rescan_start(device: int, authorization: str = Header(default=""),
                           x_auth_token: str = Header(default="")) -> dict[str, Any]:
        _require_auth(authorization, x_auth_token)   # minutes of reads + inventory writes
        if not 0 <= device <= 4194302:
            raise HTTPException(status_code=400, detail="device must be 0..4194302")
        bacnet = _bacnet()
        st = _rescan_state()
        if st.get("state") == "running":
            raise HTTPException(status_code=409,
                                detail=f"controller {st.get('device')} is being rescanned")
        if _SWEEP.locked():
            raise HTTPException(status_code=409, detail="a discovery or device read is running")
        from .. import rescan as _rescan

        def progress(done: int, total: int) -> None:
            st["done"], st["total"] = done, total

        async def run() -> None:
            try:
                st["result"] = await _rescan.rescan_device(
                    bacnet, str(app.state.dal.db_path),
                    _rescan.default_schema(app.state.config), app.state.config, device,
                    on_progress=progress)
                st["state"] = "done"
            except _rescan.RescanError as exc:
                st.update(state="error", error=str(exc))
            except Exception as exc:  # noqa: BLE001 -- shown on the page, and logged
                logger.exception("rescan of %s failed", device)
                st.update(state="error", error=f"rescan failed: {exc}")
            finally:
                st["finished_utc"] = int(time.time())
                st.pop("task", None)

        st.clear()
        st.update(state="running", device=device, done=0, total=None,
                  started_utc=int(time.time()))
        st["task"] = asyncio.create_task(run(), name=f"rescan-{device}")
        return {k: v for k, v in st.items() if k != "task"}

    # ── trend viewer (Phase 3) — reads stored samples, no BACnet needed ───────
    @app.get("/api/logged")
    async def logged_points() -> dict[str, Any]:
        """Point-picker source for the viewer: enabled points + per-point sample
        count / last-seen, plus the overall sample time-extent. A point under an override
        carries it (bacnet/priority.py), so the viewer can mark it."""
        pts = app.state.dal.logged_points()
        over = {o["point_id"]: o for o in _overrides()}
        for p in pts:
            if p["id"] in over:
                p["override"] = over[p["id"]]
        return {"points": pts, "extent": app.state.dal.samples_time_extent()}

    @app.get("/api/extent")
    async def samples_extent_only() -> dict[str, Any]:
        """Just the min/max sample timestamps — two lone MIN/MAX aggregates answered from
        idx_samples_ts in O(log n), no COUNT.

        The viewer anchors its range buttons ("6h" = the last 6 hours OF DATA, so a dead
        collector shows data rather than blank canvas) to this. It used to come only from
        /api/logged at page load and was never refreshed, so a tab left open for hours
        anchored to a stale "newest sample" and every range button returned a window ending
        hours in the past. This lets the viewer re-anchor on every click for ~1 ms."""
        return {"extent": app.state.dal.samples_time_extent()}

    @app.get("/api/weather")
    async def weather(start: int, end: int) -> dict[str, Any]:
        """Outdoor conditions for the range — the viewer overlays OAT + wet-bulb on
        their own axis (independent of the point picker)."""
        return {"weather": app.state.dal.weather_range(_ts(start), _ts(end))}

    @app.get("/api/points/{point_id}/peers")
    async def point_peers(point_id: int) -> dict[str, Any]:
        """The same reading (by role) across all AHUs — 'add for all units'."""
        return {"peers": app.state.dal.role_peers(point_id)}

    @app.get("/api/points/{point_id}/series")
    async def point_series(point_id: int, start: int, end: int,
                           rollup: str | None = None) -> dict[str, Any]:
        """One point's series. Bounded and off the loop like POST /api/series, which it now
        shares: it had no clamp and no row budget, raw by default (2026-10-27 review)."""
        if start > end:
            start, end = end, start
        out = await asyncio.to_thread(
            _series_payload, str(app.state.dal.db_path), [point_id], _ts(start), _ts(end),
            _valid_rollup(rollup), _cfg("bacnet", "poll", "interval_s", default=120))
        if not out["series"]:
            raise HTTPException(status_code=404, detail="unknown point")
        s = out["series"][0]
        return {"point_id": point_id, "meta": s["meta"], "rollup": s["rollup"],
                "samples": s["samples"]}

    @app.post("/api/series")
    async def series_batch(payload: dict = Body(...)) -> dict[str, Any]:
        """Batch series fetch for a multi-point overlay (one round-trip). Body:
        {ids:[int], start:int, end:int, rollup?:'raw'|'5m'|'1h'}.

        ⚠️ BOUNDED, deliberately. This route is in `_VIEWER_GET`, so any host on the HVAC
        VLAN can post to it, and it used to accept an unlimited id list (duplicates and
        all) over an unbounded window at `rollup:'raw'`. One request naming every enabled
        point across the whole record would pull tens of millions of rows into Python ON
        THE COLLECTOR'S EVENT LOOP -- the DAL connection is thread-bound, so there is
        nowhere to offload it, and polling stops until it finishes.
        That is not an access-control problem:
        a VLAN host can already speak BACnet to the controllers. It is a resource bound,
        and the thing it protects is the RECORD. 2026-09-22 audit, M-10.
        """
        try:
            raw_ids = [int(i) for i in payload.get("ids", [])]
            start, end = int(payload["start"]), int(payload["end"])
        except (KeyError, TypeError, ValueError, OverflowError):    # Overflow: JSON Infinity
            raise HTTPException(status_code=400,
                                detail="body needs ids:[int], start:int, end:int")
        # dict.fromkeys de-duplicates while KEEPING ORDER: the viewer's colour assignment
        # and axis grouping both follow the order the ids were sent in.
        unique = list(dict.fromkeys(raw_ids))
        ids = unique[:_SERIES_MAX_IDS]
        # Said out loud: the points past the cap used to vanish from the chart AND from its
        # CSV with nothing to show for it (2026-10-20, exporting 84 tower points).
        dropped = len(unique) - len(ids)
        if start > end:
            start, end = end, start
        out = await asyncio.to_thread(
            _series_payload, str(app.state.dal.db_path), ids, _ts(start), _ts(end),
            _valid_rollup(payload.get("rollup")),
            _cfg("bacnet", "poll", "interval_s", default=120))
        return dict(out, dropped=dropped)

    @app.post("/api/export")
    async def export_data(payload: dict = Body(...)) -> Any:
        """Any logged points over any run of local days, as one CSV or a zip of a CSV per
        day (export.py). Body: {ids:[int], start:int, end:int, resolution:'raw'|'5m'|'1h',
        layout:'day'|'one', weather?:bool}. The viewer's "Export data" dialog. `weather`
        adds the hourly weather file (the Tempest beside the building's outdoor sensors),
        and with no ids it is the whole download.

        Bounded, like /api/series and for the same reason, but by SIZE rather than by
        resolution: an export is asked for at a resolution and must not quietly arrive
        coarser. Over export.MAX_CELLS it is refused with the numbers. The file is written in
        a worker thread on its own read-only connection, one export at a time."""
        import os
        import tempfile

        from fastapi.responses import FileResponse
        from starlette.background import BackgroundTask

        from .. import export as _export
        try:
            ids = list(dict.fromkeys(int(i) for i in payload.get("ids", [])))
            start, end = int(payload["start"]), int(payload["end"])
        except (KeyError, TypeError, ValueError, OverflowError):
            raise HTTPException(status_code=400,
                                detail="body needs ids:[int], start:int, end:int")
        res = payload.get("resolution") or "raw"
        layout = payload.get("layout") or "day"
        weather = bool(payload.get("weather"))
        if res not in _export.STEP_S or layout not in ("day", "one"):
            raise HTTPException(status_code=400,
                                detail="resolution must be raw|5m|1h and layout day|one")
        if (not ids and not weather) or end <= start:
            raise HTTPException(status_code=400,
                                detail="pick at least one point (or the weather) and one day")
        cells = _export.estimate_cells(len(ids), start, end, res)
        if cells > _export.MAX_CELLS:
            raise HTTPException(status_code=413, detail=(
                f"That is about {cells / 1e6:.0f} million readings ({len(ids)} points); the "
                f"limit is {_export.MAX_CELLS / 1e6:.0f} million. Pick fewer days or points, "
                "or 5-minute or hourly averages."))
        # Then to what can exist. Days are walked one by one in LOCAL time, which Windows
        # cannot do before 1970 or after 3000: a weather-only export (no size check) over a
        # far range was a 500 or a long loop (2026-10-27 review).
        start, end = max(start, _EXPORT_FIRST), min(end, int(time.time()) + 86400)
        if end <= start:
            raise HTTPException(status_code=400, detail="nothing is recorded in that range")
        if _EXPORT_LOCK.locked():
            raise HTTPException(status_code=429,
                                detail="another export is still being written; try again shortly")
        async with _EXPORT_LOCK:
            fd, tmp = tempfile.mkstemp(prefix="hvactrend_export_")
            os.close(fd)
            try:
                info = await asyncio.to_thread(_export.write_export, str(app.state.dal.db_path),
                                               ids, start, end, res, layout, tmp,
                                               weather=weather)
            except Exception:
                os.unlink(tmp)
                raise
        return FileResponse(tmp, filename=info["filename"],
                            media_type="application/zip" if info["zip"] else "text/csv",
                            headers={"X-Export-Points": str(info["points"]),
                                     "X-Export-Rows": str(info["rows"]),
                                     "X-Export-Weather-Hours": str(info["weather_hours"])},
                            background=BackgroundTask(os.unlink, tmp))

    # ── AI reports — DB-only, no BACnet needed ────────────────────────────────
    # /api/anomalies (the old pre-screen, "what the AI report will be handed") and
    # /api/health (the old health grades) were removed 2026-10-20. The summary is handed
    # the alarm rules' findings instead (ai/summary.py), and both ran the old detectors on
    # the loop the collector polls from: a week's context took 60 s.
    @app.get("/api/alerts")
    async def alerts(status: str | None = None, limit: int = 100) -> dict[str, Any]:
        """Between-report alerts: currently-active high/critical conditions raised by the
        continuous check (deduped, auto-resolved). `status` filters open|resolved."""
        dal = app.state.dal
        return {"open": _live_alerts(),
                "recent": dal.list_alerts(status=status, limit=limit)}

    @app.post("/api/alerts/check")
    async def alerts_check(authorization: str = Header(default=""),
                           x_auth_token: str = Header(default="")) -> dict[str, Any]:
        """Run the alert evaluation NOW (instead of waiting for the collector's timer).
        Deterministic — no LLM/key. Useful for an operator 'check now' + testing."""
        _require_auth(authorization, x_auth_token)
        from ..ai.alerts import evaluate_alerts
        return evaluate_alerts(app.state.dal, app.state.config, int(time.time()))

    @app.post("/api/reports/generate")
    async def generate(payload: dict = Body(default={}),
                       authorization: str = Header(default=""),
                       x_auth_token: str = Header(default="")) -> dict[str, Any]:
        """Write the AI summary now (ai/summary.py): gathers what the alarm rules found
        (in a worker thread), calls Claude, stores it.
        Body: {kind:'daily'|'weekly'|'manual', start?:int, end?:int}."""
        _require_auth(authorization, x_auth_token)   # billed LLM call + DB writes
        from ..ai.reports import REPORT_LOCK, generate_report
        kind = payload.get("kind", "daily")
        if kind not in ("daily", "weekly", "manual"):
            raise HTTPException(status_code=400, detail="kind must be daily|weekly|manual")
        # Limits on a billed call (2026-11-06 review, M11): it ignored ai.enabled, took any
        # window (a one-sided {"start": 0} loaded every raw sample of every rule point), and
        # two presses were two calls.
        if not bool(_cfg("ai", "enabled", default=False)):
            raise HTTPException(status_code=409,
                                detail="the AI summary is switched off (ai.enabled)")
        start, end = payload.get("start"), payload.get("end")
        if (start is None) != (end is None):
            raise HTTPException(status_code=400, detail="give both start and end, or neither")
        if start is not None:
            if not all(isinstance(x, int) and not isinstance(x, bool) for x in (start, end)):
                raise HTTPException(status_code=400, detail="start and end are epoch seconds")
            if not 0 < end - start <= 7 * 86400:
                raise HTTPException(status_code=400, detail="the window is at most 7 days")
        if REPORT_LOCK.locked():
            raise HTTPException(status_code=409, detail="a report is being written now")
        async with REPORT_LOCK:
            try:
                return await generate_report(app.state.dal, app.state.config, kind,
                                             start=start, end=end)
            except ValueError as exc:          # the prompt-size refusal
                raise HTTPException(status_code=413, detail=str(exc)) from exc

    @app.get("/api/reports")
    async def reports_list(limit: int = 50) -> dict[str, Any]:
        return {"reports": app.state.dal.list_reports(limit=limit)}

    @app.get("/api/reports/{report_id}")
    async def report_get(report_id: int) -> dict[str, Any]:
        rep = app.state.dal.get_report(report_id)
        if not rep:
            raise HTTPException(status_code=404, detail="unknown report")
        return rep

    @app.get("/api/reports/{report_id}/verify")
    async def report_verify(report_id: int) -> dict[str, Any]:
        """Check the report's CLAIMS against the stored data. Read-only, cheap, and
        deliberately unauthenticated like the rest of the read surface — a report that
        says something untrue should be catchable by anyone looking at it."""
        from ..ai.verify import verify_report
        out = verify_report(app.state.dal, report_id)
        if out.get("error"):
            raise HTTPException(status_code=404, detail=out["error"])
        return out

    @app.get("/api/findings")
    async def findings_list(status: str | None = None, severity: str | None = None,
                            system_id: int | None = None, limit: int = 500) -> dict[str, Any]:
        return {"findings": app.state.dal.list_findings(
            status=status, severity=severity, system_id=system_id, limit=limit)}

    @app.post("/api/findings/{finding_id}/status")
    async def finding_set_status(finding_id: int, payload: dict = Body(default={}),
                                 authorization: str = Header(default=""),
                                 x_auth_token: str = Header(default="")) -> dict[str, Any]:
        """Set a finding's status, optionally with an operator note explaining WHY
        ("yes, I raised min OA to 35% to test IAQ"). An acknowledgement WITH a note is
        fed back to the model as ground truth — the action stops being reported as a
        fault, but its consequences are still analyzed."""
        _require_auth(authorization, x_auth_token)   # mutates the tracked findings ledger
        status = payload.get("status")
        if status not in ("open", "acknowledged", "closed"):
            raise HTTPException(status_code=400,
                                detail="status must be open|acknowledged|closed")
        note = payload.get("note")
        if note is not None and not isinstance(note, str):
            raise HTTPException(status_code=400, detail="note must be a string")
        if note is not None and len(note) > 2000:
            raise HTTPException(status_code=400, detail="note too long (max 2000 chars)")
        app.state.dal.set_finding_status(finding_id, status, operator_note=note)
        return {"finding_id": finding_id, "status": status, "note": note}

    # ── operator action log (ranged annotations) ─────────────────────────────
    @app.get("/api/annotations")
    async def annotations_list(limit: int = 100, start: Optional[int] = None,
                               end: Optional[int] = None) -> dict[str, Any]:
        """The operator's own record: what was changed, when, why. With start+end,
        returns only what OVERLAPS that window (what a report for it would see)."""
        dal = app.state.dal
        if start is not None and end is not None:
            return {"annotations": dal.annotations_overlapping(_ts(start), _ts(end))}
        return {"annotations": dal.list_annotations(limit=max(1, min(int(limit), 500)))}

    @app.post("/api/annotations")
    async def annotation_add(payload: dict = Body(default={}),
                             authorization: str = Header(default=""),
                             x_auth_token: str = Header(default="")) -> dict[str, Any]:
        """Log a TIME-BOUNDED operator action — "forced AC-9 occupied 14:00-16:00 to air
        it out", "raised the DAT setpoint to test". It reaches the AI only for reports
        whose window it overlaps, which is what makes it temporary rather than the
        always-on per-unit note. Omit `ts_end_utc` for a point-in-time event."""
        _require_auth(authorization, x_auth_token)
        title = (payload.get("title") or "").strip()
        if not title:
            raise HTTPException(status_code=400, detail="title is required")
        if len(title) > 300:
            raise HTTPException(status_code=400, detail="title too long (max 300 chars)")
        body = payload.get("body")
        if body is not None and (not isinstance(body, str) or len(body) > 4000):
            raise HTTPException(status_code=400, detail="body must be a string <= 4000 chars")
        now = int(time.time())
        try:
            ts = int(payload.get("ts_utc") or now)
            ts_end = payload.get("ts_end_utc")
            ts_end = int(ts_end) if ts_end not in (None, "") else None
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="ts_utc / ts_end_utc must be epoch seconds")
        if ts_end is not None and ts_end <= ts:
            raise HTTPException(status_code=400, detail="ts_end_utc must be after ts_utc")
        sid, pid = payload.get("system_id"), payload.get("point_id")
        if sid is not None and not any(s["id"] == sid for s in app.state.dal.list_systems()):
            raise HTTPException(status_code=404, detail="unknown system")
        aid = app.state.dal.add_annotation(
            ts_utc=ts, ts_end_utc=ts_end, system_id=sid, point_id=pid,
            title=title, body=body, author=payload.get("author") or "operator")
        return {"id": aid, "ts_utc": ts, "ts_end_utc": ts_end, "title": title,
                "system_id": sid}

    @app.delete("/api/annotations/{annotation_id}")
    async def annotation_delete(annotation_id: int,
                                authorization: str = Header(default=""),
                                x_auth_token: str = Header(default="")) -> dict[str, Any]:
        _require_auth(authorization, x_auth_token)
        if not app.state.dal.delete_annotation(annotation_id):
            raise HTTPException(status_code=404, detail="unknown annotation")
        return {"deleted": annotation_id}

    return app


# ── logging (so the 24/7 Task Scheduler run isn't blind) ─────────────────────
# Service names whose failures are somebody ELSE'S packet, not our bug.
_WIRE_DECODE_ERRORS = frozenset({"DecodingError", "InvalidTag", "DecodingObjectError"})
_wire_noise = {"n": 0}


def _install_bacnet_noise_filter() -> None:
    """Stop undecodable inbound BACnet packets from drowning the log.

    This program is a passive listener on a shared BACnet/IP VLAN. Other vendors' devices
    broadcast frames bacpypes3 cannot parse, and each one surfaced as an asyncio
    "Task exception was never retrieved" ERROR with a ~35-line traceback: 1,292 of them in
    a single day, against zero genuine errors. Two real costs -- they consumed the 30 MB
    rotating log budget (cutting real diagnostic history to about five days) and they made
    a genuine ERROR unfindable.

    They are NOT suppressed, only made proportionate: every one is still available at DEBUG,
    and a running total is reported at INFO on an exponential schedule (1st, 10th, 100th...)
    so a sudden change in wire health is still visible without 35 lines of noise per packet.
    Collection is provably unaffected by them -- the poll cadence is exact.
    """
    import asyncio as _asyncio

    try:
        loop = _asyncio.get_running_loop()
    except RuntimeError:                          # no loop yet (tests constructing the app)
        return
    default = loop.get_exception_handler()

    def handler(lp, context):
        exc = context.get("exception")
        if exc is not None and type(exc).__module__.startswith("bacpypes3") \
                and type(exc).__name__ in _WIRE_DECODE_ERRORS:
            _wire_noise["n"] += 1
            n = _wire_noise["n"]
            logger.debug("undecodable inbound BACnet frame (#%d): %r", n, exc)
            if n in (1, 10, 100) or n % 1000 == 0:
                logger.info("ignored %d undecodable inbound BACnet frame(s) so far — "
                            "other vendors' traffic on the shared VLAN, not our reads; "
                            "collection is unaffected (set logging.level=DEBUG for detail)", n)
            return
        (default or lp.default_exception_handler)(context)

    loop.set_exception_handler(handler)


def _setup_logging(config: Any) -> None:
    """Configure a rotating file log + console from config.logging. Idempotent."""
    import logging.handlers
    from pathlib import Path

    logcfg = config.get("logging") if hasattr(config, "get") else None
    level = ((logcfg.get("level") if logcfg else None) or "INFO").upper()
    path = (logcfg.get("file") if logcfg else None) or "logs/hvactrend.log"

    root = logging.getLogger()
    if any(getattr(h, "_occ", False) for h in root.handlers):
        return                                    # already configured this process
    root.setLevel(level)
    # Tests construct the real app, which used to append to the REAL log. That put fixture
    # noise ("weather refresh failed: boom") and -- far worse -- a fabricated
    # "Read-only override active (allow_writes=true)" WARNING into the operational record.
    # That line is the single most alarming thing this log can contain, and grepping for a
    # safety violation would have found one that never happened. Console only under pytest.
    under_test = bool(os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("HVACTREND_NO_FILE_LOG"))
    # httpx logs the FULL request URL at INFO — and the Tempest weather API takes its token
    # as a QUERY PARAMETER (?token=...), so every hourly fetch wrote the credential to disk
    # in plaintext. Found when a backup archive was scanned for secret material: 106
    # occurrences across four rotated log files. Anything that puts a credential in a URL
    # has the same problem, so silence the request line rather than special-casing Tempest.
    # WARNING still surfaces real HTTP failures.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        if under_test:
            raise OSError("under pytest: console logging only")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            path, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
        fh.setFormatter(fmt)
        fh._occ = True                            # mark as ours (idempotence)
        root.addHandler(fh)
    except OSError:                               # unwritable log dir — keep console only
        if not under_test:
            logger.warning("could not open log file %s; logging to console only", path)
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)


# ── trend-series helpers ─────────────────────────────────────────────────────
# ⚠️ Bounds for POST /api/series (2026-09-22 audit, M-10). Not security -- the VLAN is
# the boundary and that is a deliberate decision -- but a bound on what ONE request can
# make the collector's event loop do. The chart legend stops being readable well before
# 64 series, and 400k rows is roughly 20 points x 30 days of 5-minute buckets.
_SERIES_MAX_IDS = 64
_EXPORT_LOCK = asyncio.Lock()      # one bulk export at a time (export.py)
_SERIES_ROW_BUDGET = 400_000
_ROLLUP_STEP_S = {None: 120, "5m": 300, "1h": 3600}


# Epoch seconds outside this are not a time anyone means, and past 2**63 SQLite overflows:
# /api/weather, /api/annotations and /api/series answered such a value with a 500.
_TS_MAX = 2 ** 34            # the year 2514: Windows' localtime() ends at 3000


_EXPORT_FIRST = 1_500_000_000    # 2017-07-14: before this program or its building's BAS


def _ts(v: int) -> int:
    return max(0, min(int(v), _TS_MAX))


def _series_payload(db_path: str, ids: list, start: int, end: int,
                    roll: Optional[str], poll_s: Any) -> dict:
    """The work of POST /api/series, in a worker thread on its OWN read-only connection.

    It ran on the event loop the BACnet stack shares: 64 points over 14 days at 5 m is 1.2 s
    and 18 MB, and polling stood still for every one (2026-10-27 review). The loop's DAL
    connection is bound to its thread, so the worker opens its own, as export and
    /api/poll-health do: WAL admits a second reader, and sqlite3 releases the GIL as it
    steps."""
    from ..storage.sqlite_dal import SQLiteDAL
    dal = SQLiteDAL(db_path, read_only=True)
    try:
        ext = dal.samples_time_extent()
        # Clamp to what actually exists. A window of 1970->2100 costs the same as the real
        # extent once clamped, and a viewer bug that sends one stops being a DoS.
        if ext.get("min_ts") is not None:
            start = max(start, int(ext["min_ts"]) - 86400)
            end = min(end, int(ext["max_ts"]) + 86400)
        # ROW BUDGET. Raw is 2-minute data: a 90-day window on 20 points is ~1.3 M rows.
        # Rather than refuse, serve the same window at a resolution that fits -- which is
        # what the viewer would have asked for anyway.
        roll = _bounded_rollup(roll, len(ids), end - start)
        series = []
        for pid in ids:
            meta = dal.point_meta(pid)
            if not meta:
                continue
            eff, rows = _series_with_fallback(dal, pid, start, end, roll)
            samples = _series_rows(rows, eff)
            series.append({"point_id": pid, "meta": meta, "rollup": eff or "raw",
                           "samples": samples,
                           "outages": _outages(dal, pid, samples, eff, poll_s)})
        # `ext` is read once, above, to clamp the window: a second read was a 34 ms query on
        # every chart load (2026-09-29 audit, A7).
        return {"series": series, "rollup": roll or "raw", "extent": ext}
    finally:
        dal.close()


def _schedule_detect_ro(db_path: str, config: Any, days: int) -> dict:
    from ..ai.schedule_detect import detect_schedules
    from ..storage.sqlite_dal import SQLiteDAL
    dal = SQLiteDAL(db_path, read_only=True)
    try:
        return detect_schedules(dal, config, lookback_days=days)
    finally:
        dal.close()


def _bounded_rollup(rollup: Optional[str], n_ids: int, span_s: int) -> Optional[str]:
    """Coarsen the resolution until the request fits the row budget.

    Serving a coarser window beats refusing: the viewer already falls back to rollups for
    wide windows, so this only bites a request the UI would not have made -- and it bites
    it by answering, not by erroring. Returns the rollup to actually use.
    """
    n_ids = max(1, n_ids)
    span_s = max(0, span_s)
    for candidate in (rollup, "5m", "1h"):
        step = _ROLLUP_STEP_S.get(candidate if candidate != "raw" else None)
        if step is None:
            continue
        if n_ids * (span_s / step) <= _SERIES_ROW_BUDGET:
            return None if candidate == "raw" else candidate
    return "1h"


def _norm_rollup(rollup: Optional[str]) -> Optional[str]:
    """Normalize the rollup arg; None/''/'raw' -> None (raw samples). 5m/1h rollup
    tables are Phase 7 (not yet built) — query_series raises on other values."""
    return None if rollup in (None, "", "raw") else rollup


def _series_with_fallback(dal: Any, pid: int, start: int, end: int,
                          rollup: Optional[str]) -> tuple[Optional[str], list]:
    """Query at the requested rollup; if the downsample table has nothing for this
    window (not built yet, or older than its retention), transparently fall back to raw
    so the chart is never SILENTLY BLANK. Returns (effective_rollup, rows) so the caller
    formats + labels the response with the rollup actually used.

    ⚠️ AND stitch on the TAIL. The rollups are materialised hourly, so a non-empty
    rollup still ended at the last materialised bucket -- measured 84 minutes short at the
    live edge. The fallback above only fired when the table had NOTHING, so every wide
    window quietly stopped an hour ago and read as "collection has stopped". The tail is
    bucketed from raw on the fly, one bounded per-point read. 2026-09-22 audit, M-9.
    """
    if not rollup:
        return None, dal.query_series(pid, start, end, rollup=None)
    if hasattr(dal, "rollup_series"):
        # stored buckets, with the head (5 m retention) and the tail (not built yet) bucketed
        # from raw: the window stays at its resolution and inside the row budget. It used to
        # drop to RAW whenever the table had nothing, outside the budget (2026-10-27 review).
        rows = dal.rollup_series(pid, start, end, rollup)
        if rows:
            return rollup, rows
    rows = dal.query_series(pid, start, end, rollup=rollup)
    return (rollup, rows) if rows else (None, dal.query_series(pid, start, end, rollup=None))


def _series_rows(rows: list, rollup: Optional[str]) -> list[dict]:
    """Compact stored rows into the viewer's wire shape.

    raw:    {t, v, vt, q}          value_num, value_text label, quality
    rollup: {t, v, lo, hi, n, nt}  bucket avg + min/max envelope + counts

    `nt` (n_total) is the ALL-QUALITY population of the bucket; `n` counts only the
    chartable ones. `nt > 0 and n == 0` therefore means "we were recording and the sensor
    was faulted the whole hour", which the viewer must not draw as the same empty gap it
    draws when the collector was down. Buckets written before the n_total migration carry
    NULL, which is "unknown", not zero -- the viewer treats it as a plain gap.
    """
    if rollup:
        return [{"t": r["bucket_utc"], "v": r["v_avg"], "lo": r["v_min"],
                 "hi": r["v_max"], "last": r["v_last"], "n": r["n"],
                 "nt": (r["n_total"] if "n_total" in r.keys() else None)} for r in rows]
    return [{"t": r["ts_utc"], "v": r["value_num"], "vt": r["value_text"],
             "q": r["quality"]} for r in rows]


_OUTAGE_MAX_RUNS = 200


def _outages(dal: Any, pid: int, samples: list, rollup: Optional[str],
             poll_s: Any = 120) -> list[dict]:
    """The runs of the window in which this point gave no good reading: [{a, b, q}], b
    exclusive. 2026-10-14: when the tower panel loaded an old database, 45 of its
    points answered nothing for 27 hours, and the viewer showed it only as a 3-px ribbon.

    A raw sample is bad when its quality is not good; a rollup bucket when it was recorded
    (nt > 0) and held nothing good (n == 0). Bad samples less than three steps apart are one
    run. `q` is the run's commonest bad quality: comm-loss (the point did not answer), fault
    (its controller flagged it), stale. In a rollup window it is read from the raw samples,
    one bounded query per run; None when they have been pruned."""
    step = {"5m": 300, "1h": 3600}.get(rollup or "", 0) or int(float(poll_s or 120))
    runs: list[dict] = []
    cur: Optional[dict] = None
    for s in samples:
        bad = ((s.get("nt") or 0) > 0 and not s.get("n")) if rollup \
            else s.get("q") not in (None, "good")
        if not bad:
            if cur is not None and s.get("v") is not None:     # a good reading ends it
                runs.append(cur)
                cur = None
            continue
        if cur is not None and s["t"] - cur["last"] <= 3 * step:
            cur["last"] = s["t"]
        else:
            if cur is not None:
                runs.append(cur)
            cur = {"a": s["t"], "last": s["t"], "mix": {}}
        if not rollup:
            cur["mix"][s["q"]] = cur["mix"].get(s["q"], 0) + 1
    if cur is not None:
        runs.append(cur)
    out = []
    for r in runs[-_OUTAGE_MAX_RUNS:]:
        b = r["last"] + step
        mix = r["mix"] if not rollup else dal.bad_quality_mix(pid, r["a"], b)
        q = max(mix.items(), key=lambda kv: kv[1])[0] if mix else None
        out.append({"a": r["a"], "b": b, "q": q})
    return out


# ── live-read helpers ────────────────────────────────────────────────────────
_LIVE_PROPS = ["present-value", "status-flags", "reliability", "out-of-service"]
# The coverage review's live check reads at most this many points per request, however many
# are on offer (2026-09-22 audit L-13: "read visible" is unbounded on the plant network, and
# this is a second reader of the same kind). 58 were on offer when it was written.
_COVERAGE_LIVE_MAX = 150


async def _read_points_live(bacnet: Any, targets: list[dict], *,
                            chunk: int = 10) -> dict[int, dict]:
    """One-shot live read of present-value + status for each point target.

    Batched RPM per device (chunked to stay under APDU limits), with per-object
    fallback. Read-only — this is the browser's 'what is this point actually
    reading right now' check.
    """
    from ..bacnet.discovery import _is_error, _present_value

    by_addr: dict[str, list[dict]] = defaultdict(list)
    for t in targets:
        by_addr[t["address"]].append(t)

    results: dict[int, dict] = {}
    for address, group in by_addr.items():
        for i in range(0, len(group), chunk):
            batch = group[i:i + chunk]
            params: list[Any] = []
            oid_target: dict[str, dict] = {}
            for t in batch:
                oid = f"{t['object_type']},{t['object_instance']}"
                oid_target[oid] = t
                params.append(oid)
                params.append(list(_LIVE_PROPS))
            # A reply that will not decode raises, and one that is not an RPM-ACK comes back
            # as None: both used to end the whole read (None as a TypeError on the loop below).
            # One at a time instead, as the collector's poll does (2026-11-06 review, M8).
            try:
                rows = await bacnet.read_property_multiple(address, params)
            except Exception:  # noqa: BLE001
                rows = None

            if rows is None or _is_error(rows):
                for oid, t in oid_target.items():
                    try:
                        pv = await bacnet.read_property(address, oid, "present-value")
                    except Exception:  # noqa: BLE001 -- this point unread, the rest go on
                        pv = None
                    results[t["id"]] = _live_result(t, {"present-value": pv},
                                                    _is_error, _present_value)
            else:
                grouped: dict[str, dict[str, Any]] = {}
                for objid, propid, _idx, value in rows:
                    grouped.setdefault(str(objid), {})
                    if not _is_error(value):
                        grouped[str(objid)][str(propid)] = value
                for oid, t in oid_target.items():
                    results[t["id"]] = _live_result(t, grouped.get(oid, {}),
                                                    _is_error, _present_value)
    return results


def _live_result(t: dict, props: dict, is_error, present_value) -> dict:
    pv = props.get("present-value")
    err = None
    if is_error(pv):
        err, pv = str(pv), None
    num, text = present_value(pv)
    rel = props.get("reliability")
    rel_s = None if rel is None or is_error(rel) else str(rel)
    oos_raw = props.get("out-of-service")
    oos = bool(oos_raw) and not is_error(oos_raw)
    try:
        oos = bool(int(oos_raw)) if oos_raw is not None and not is_error(oos_raw) else False
    except (TypeError, ValueError):
        pass
    fault = oos or (rel_s is not None and rel_s not in ("no-fault-detected",))
    if err is None and pv is None and not props:
        err = "no-response"
    return {
        "id": t["id"], "value_num": num, "value_text": text,
        "units_text": t.get("units_text"), "reliability": rel_s,
        "out_of_service": oos, "quality": "fault" if fault else "good",
        "error": err,
    }


def _device_json(dd: Any) -> dict[str, Any]:
    d = dd.device
    return {
        "device_instance": d.device_instance, "address": d.address,
        "vendor_id": d.vendor_id, "vendor_name": d.vendor_name,
        "model_name": d.model_name, "firmware": d.firmware,
        "network_number": d.network_number, "rpm_supported": d.rpm_supported,
        "device_id": dd.device_id,
        "objects": [_object_json(o) for o in dd.objects],
    }


def _object_json(o: Any) -> dict[str, Any]:
    return {
        "object_type": o.object_type, "object_instance": o.object_instance,
        "name": o.name, "description": o.description,
        "units": o.units, "units_text": o.units_text,
        "value_num": o.value_num, "value_text": o.value_text,
        "status_flags": o.status_flags, "reliability": o.reliability,
        "out_of_service": o.out_of_service, "loggable": o.loggable,
        "error": o.error,
    }


def run() -> None:  # pragma: no cover - manual entry point
    """Dev entry point: uvicorn serves create_app() on 0.0.0.0:8000."""
    import uvicorn
    uvicorn.run("hvactrend.api.app:create_app", factory=True, host="127.0.0.1", port=8000)
