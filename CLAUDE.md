# CLAUDE.md

Guidance for Claude Code (and other AI assistants) working in this repository.

**Read [CONTRIBUTING.md](CONTRIBUTING.md) first.** Its hard rules override convenience, and
its lessons section lists the mistakes this codebase has already paid for. In short:

1. **BACnet is strictly read-only.** Never add a write path, never weaken
   `tests/test_safety.py`.
2. **One BACnet application, one event loop.** Never block the loop with database work;
   use a worker thread on a read-only connection.
3. **Alerts come from the watchdog.** It must not import the collector, and it imports the
   alarm rules only inside a fence.
4. **Secrets are the user's.** Never read, print, or write `config/secrets.env`, and never
   put a key or the ntfy topic in a tracked file.
5. **Load the page** after editing `src/hvactrend/api/static/*.html`, and read the console.
6. **Prove a new test can fail.** Break the fix in a scratch copy first.

Also:
- **Python:** 3.11+.
- **Tests:** `python -m pytest -q` and `node --test tests/js/viewer.test.mjs`.
- **Demo:** `python scripts/demo.py` serves a made-up building on port 8001, with no BACnet.
- **Line endings** are per file; preserve each one's.
- **Patch scripts:** write them as files rather than shell heredocs. Heredocs collapse
  doubled backslashes, which has turned a regex's `\b` into a backspace byte.
