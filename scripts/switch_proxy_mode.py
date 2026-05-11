#!/usr/bin/env python3
"""VF-326 / T5 — Disaster-recovery toolkit: switch the proxy cert mode via CLI.

This is the escape hatch for when the UI wizard can't run (app container down,
network isolated, etc.). Called via:

    docker compose exec app python scripts/switch_proxy_mode.py <mode>

where <mode> is one of: caddy_internal | self_signed | file

For `caddy_internal` — re-renders the Caddyfile with `tls internal` and reloads.
For `self_signed`   — mints a new leaf and swaps PEMs in place, then reloads.
For `file`          — re-renders the Caddyfile to point at /certs/*.pem
                      (does NOT upload new files — operator must `docker cp`
                      the new fullchain.pem + privkey.pem into the container
                      first, then run this with `file` to repoint the config).

Always backs up the previous Caddyfile to ops/caddy/backup/<timestamp>/ before
writing. Rolls back on reload failure or TLS probe failure.

Everything here reuses the helpers in app/api/v2/proxy.py so the toolkit + the
wizard share a single source of truth.
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.api.v2.proxy import (
    _mint_self_signed,
    _probe_tls_handshake,
    _render_caddyfile,
    _restore_caddyfile,
    _write_caddyfile_with_backup,
    reload_proxy,
    safely_swap_cert,
)


def _fail(msg: str, code: int = 1):
    print(f"  FAIL  {msg}")
    sys.exit(code)


def _ok(msg: str):
    print(f"  OK    {msg}")


def switch(mode: str) -> int:
    if mode not in ("caddy_internal", "self_signed", "file"):
        _fail(f"unsupported mode: {mode} (use one of caddy_internal, self_signed, file)", code=2)

    print("=" * 66)
    print(f"  SWITCH_PROXY_MODE -> {mode}")
    print("=" * 66)

    hostname = os.environ.get("VIBEFORGE_HOSTNAME", "localhost")
    print(f"\n[step 1] render Caddyfile template for mode={mode}")
    try:
        new_caddyfile = _render_caddyfile(mode)
        _ok(f"template rendered ({len(new_caddyfile)} bytes)")
    except Exception as e:
        _fail(f"render failed: {e}")

    print("\n[step 2] back up current Caddyfile + write new one in place")
    try:
        backup = _write_caddyfile_with_backup(new_caddyfile)
        _ok(f"backup at {backup}")
    except Exception as e:
        _fail(f"backup/write failed: {e}")

    # self_signed also needs a fresh leaf cert.
    if mode == "self_signed":
        print("\n[step 2a] mint self-signed leaf + swap PEMs")
        try:
            cert_pem, key_pem = _mint_self_signed(hostname, [], days=365)
            result = safely_swap_cert(cert_pem, key_pem)
            if not result.get("ok"):
                _restore_caddyfile(backup)
                _fail(f"safely_swap_cert rejected: {result}")
            _ok(f"minted + swapped, backup at {result.get('backup')}")
        except Exception as e:
            _restore_caddyfile(backup)
            _fail(f"self_signed mint failed: {e}")

    print("\n[step 3] reload Caddy")
    try:
        reload_proxy()
        _ok("Caddy /load accepted")
    except Exception as e:
        _restore_caddyfile(backup)
        try: reload_proxy()
        except Exception: pass
        _fail(f"Caddy rejected reload — rolled back: {e}")

    print("\n[step 4] probe TLS handshake")
    time.sleep(1)  # give caddy a moment to rebind
    ok, detail = _probe_tls_handshake(hostname)
    if not ok:
        _restore_caddyfile(backup)
        try: reload_proxy()
        except Exception: pass
        _fail(f"probe failed, rolled back — {detail}")
    _ok(f"probe ok — {detail}")

    print("\n" + "=" * 66)
    print("  ALL STEPS GREEN")
    print("=" * 66)
    return 0


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(switch(sys.argv[1]))


if __name__ == "__main__":
    main()
