# Archived nginx config — 2026-04-22

The VibeForge+ stack used nginx as its reverse-proxy + TLS-terminator until
2026-04-22. VF-246 spiked Caddy alongside nginx, confirmed feature parity on
a ~40-line Caddyfile (vs the 118 lines here), and PK decided to migrate.

This file is preserved as-is for:
- History / diff when debugging regressions
- Reference for any nginx-specific behaviour we later rediscover we relied on
- Copy source for a rollback if the Caddy migration ever needs to be reversed

The live proxy config is at `ops/caddy/Caddyfile`. The old mount path
`/etc/nginx/conf.d` + `/etc/nginx/certs` is gone from `docker-compose.yml`.
