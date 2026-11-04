"""Run the JavaScript viewer tests as part of the normal suite.

The viewer is 4,017 lines of canvas JavaScript -- 28% of the codebase and the primary
deliverable -- and until 2026-09-15 none of it was tested, because there was no runner. An
external audit found three real bugs in it, one of which had already been found and fixed in
two sibling renderers in the same file. `node --test` needs no dependencies and no build
step, which is the only reason it is acceptable here.

Skipped, not failed, when node is unavailable: a Python-only checkout must still be able to
run the suite. The skip is loud enough to notice in -v output.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JS_DIR = ROOT / "tests" / "js"


@pytest.mark.skipif(shutil.which("node") is None,
                    reason="node not installed — viewer JS tests skipped")
def test_viewer_javascript():
    env = dict(os.environ)
    # Several tests are about local-time handling across DST, with expected values written
    # for America/New_York -- pin it rather than inheriting the machine's own zone.
    env["TZ"] = "America/New_York"
    env.pop("HVACTREND_VIEWER_HTML", None)       # always test the SHIPPED file
    proc = subprocess.run(
        ["node", "--test", str(JS_DIR / "viewer.test.mjs")],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        pytest.fail("viewer JS tests failed:\n" + proc.stdout[-6000:] + "\n" + proc.stderr[-2000:])


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_the_js_harness_would_notice_a_regression():
    """A test that passes against both the broken and the fixed code is not a test.

    F-03's fix is verified by mutation: the harness is pointed at a copy of trends.html with
    the pre-fix single-path renderer restored, and the gap tests must fail against it."""
    src = (ROOT / "src" / "hvactrend" / "api" / "static" / "trends.html").read_text(
        encoding="utf-8")
    marker = "const ovGap = ovGapThreshold(ovData.pts);"
    assert marker in src, "the overview gap split is gone from trends.html"

    import tempfile
    with tempfile.TemporaryDirectory() as td:
        broken = Path(td) / "trends.html"
        # Remove the run-splitting so the strip draws one unbroken path again.
        broken.write_text(src.replace(marker, "const ovGap = Infinity;", 1), encoding="utf-8")
        env = dict(os.environ)
        env["TZ"] = "America/New_York"
        env["HVACTREND_VIEWER_HTML"] = str(broken)
        proc = subprocess.run(
            ["node", "--test", str(JS_DIR / "viewer.test.mjs")],
            cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120,
        )
        assert proc.returncode != 0, (
            "the gap tests PASSED against a viewer with the fix removed — they are not "
            "actually testing anything")
        assert "F-03" in proc.stdout, "expected the F-03 tests to be the ones that failed"
