"""Every page's script must at least PARSE.

A SyntaxError kills a whole no-build-step <script> while the server answers 200 and every API
test passes. An edit to Point Selection once lost one backslash (`unit\\'s` became
`unit's` inside a quoted string) and the page came up blank, with the full suite green; only
loading the page caught it. This catches that class, not the rest: still load the page.
Skipped, not failed, without node (as test_viewer_js.py).
"""

import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "src" / "hvactrend" / "api" / "static"

_PARSE = r"""
const fs = require('fs');
const html = fs.readFileSync(process.argv[1], 'utf8');
const blocks = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
blocks.forEach((src, i) => {
  try { new Function(src); }
  catch (e) { console.error(`script block ${i}: ${e.name}: ${e.message}`); process.exit(1); }
});
console.log(`${blocks.length} block(s) parse`);
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize("page", sorted(p.name for p in STATIC.glob("*.html")))
def test_every_page_script_parses(page):
    r = subprocess.run(["node", "-e", _PARSE, str(STATIC / page)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"{page}: {r.stderr.strip()}"
    assert "block(s) parse" in r.stdout and not r.stdout.startswith("0 "), r.stdout
