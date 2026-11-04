## Summary

What does this change do, and why?

## Checklist

- [ ] Tests added or updated for the change
- [ ] `python -m pytest -q` passes locally (it runs the viewer's JavaScript tests too, with Node)
- [ ] For a page change: the page loaded in a browser with no console errors
      (`python scripts/demo.py` gives you a building to try it on)
- [ ] CHANGELOG.md updated
- [ ] Docs updated if behaviour or a setting changed
- [ ] BACnet stays read-only: no write, no new service outside the allowlist
      (CONTRIBUTING.md, "Hard rules")
- [ ] No secrets, and no real building's addresses, device instances or names

## How to check it

Steps a reviewer can follow to see it working.
