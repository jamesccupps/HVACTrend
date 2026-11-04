# Security policy

HVACTrend runs on building controls networks, so security reports are taken seriously.

## Reporting a vulnerability

Please **do not open a public issue**. Use GitHub's private vulnerability reporting instead:
**Security → Report a vulnerability** on this repository
(<https://github.com/jamesccupps/HVACTrend/security/advisories/new>).

Include:
- what an attacker could do;
- the steps to reproduce;
- the version or commit.

You should hear back within a week.

Of particular interest:
- **anything that could send a BACnet write**, or any service other than reads and
  discovery;
- **a way past `api.remote_viewer_only`** that reaches settings, secrets or backups from
  another computer;
- **a way to read `config/secrets.env`** through the program.

## Supported versions

The latest release on the `main` branch.

## Design

[docs/security.md](docs/security.md) describes:
- the read-only guarantee;
- network exposure;
- how secrets are handled;
- what, if anything, leaves the computer.
