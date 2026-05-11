# Security

## Status

**This is a pre-RC personal project (0.7.0-PRE-RC).** A formal threat-model review has not been completed. The codebase has known posture limitations, including:

- Self-signed cert by default (operators must trust it manually or supply their own)
- Super-Admin password set once; no rotation policy enforced
- No CSRF tokens on API mutations (cookie-only same-origin protection)
- No content-security-policy headers tuned for production exposure
- Audit log is local-only (no off-host shipping)
- No formal security audit performed

Suitable for self-hosted small-team or single-operator use behind your own network controls. **Not for public-internet exposure without further hardening.**

## Reporting a vulnerability

If you find a security issue, please report it **privately** rather than filing a public issue:

- Use GitHub's **Private Vulnerability Reporting** feature on this repo (Security tab → Report a vulnerability), or
- Open a GitHub issue marked `[security]` in the title with only enough detail to coordinate — the maintainer will follow up on a private channel before any technical specifics get discussed publicly

There is **no SLA**. The project is maintained by one person on a part-time basis. Expect:

- Acknowledgment within a few days, not hours
- Discussion + reproduction in private
- Disclosure timeline negotiated case-by-case
- A fix in a future release if confirmed; no commitment to backport to in-the-wild installs

## Scope

In scope:

- Code in this repository (the `app/`, `scripts/`, `migrations/`, `ops/` trees)
- The bundled `Caddyfile` and `docker-compose.yml`
- The install script (`scripts/vibeforge-install.sh`)
- The agent contract content (the API and the rules it advertises)

Out of scope:

- Caddy itself (report to upstream)
- Postgres itself (report to upstream)
- The bundled `forgejo` and `vaultwarden` containers — they're not currently wired into the board flow in this release; report directly to those projects
- Any operator's own deployment choices (cert mode beyond self-signed, custom Caddyfile edits, additional services they've added, etc.)

## What this means in practice

Don't expose this on the open internet without doing your own security work. The bundled defaults are fine for behind-VPN / behind-firewall use. Beyond that, you're on your own to add CSP headers, real certs from a public CA, WAF rules, etc.
