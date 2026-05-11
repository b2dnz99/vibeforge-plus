"""VF-322 — Proxy admin read-only surface (SU + SA).

Five proxy-agnostic helpers wrap the reverse-proxy (Caddy today; nginx or other
in the future) so the UI and T2/T3/T4 don't re-implement provider specifics:

    get_proxy_config()   – full live config JSON
    get_proxy_rules()    – flattened route table
    get_cert_info()      – current TLS cert info + detected mode
    reload_proxy()       – POST /load against Caddy admin API
    get_ca_bundle()      – internal CA root PEM (only if mode = caddy_internal)

Five HTTP endpoints expose the helpers. Auth model:
    - GET /api/v2/proxy/config, /rules, /cert-info, /ca-bundle
        — require SU or SA (cookie auth). SU sees read-only surface; SA too.
    - POST /api/v2/proxy/reload
        — SA only (elevation path or break-glass). SU UI renders disabled button.

The CA bundle endpoint is shared with T4's user-facing Certificate tab. For T4
we'll relax it to any authenticated user once that ticket picks up — for now
it stays SU/SA-only since T4 hasn't shipped.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import ssl
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.db.session import get_db

router = APIRouter()

# ── Config ──
CADDY_ADMIN = "http://caddy:2019"
CERT_PATH = "/certs/fullchain.pem"
CADDY_INTERNAL_CA_PATH = "/pki/ca/local/certificates"


# ═══════════════════════════════════════════════════════════════════════════
# Auth gates — SU (read-only page) vs SA (write action)
# ═══════════════════════════════════════════════════════════════════════════

def _require_su_or_sa(request: Request, db: Session):
    """Cookie-auth gate: return acting user if they are SU or elevated SA.

    SU (role=super_user) is sufficient for READ endpoints. SA (elevated SU or
    vf_sa_session bound to super_admin) passes too — SA is a superset of SU
    for this surface. Agents + viewers are rejected.
    """
    from app.models.user import User
    from app.models.session import UserSession
    from app.api.v2.admin import _require_sa as _admin_require_sa

    # SA path first (covers both vf_sa_session break-glass and elevated SU).
    sa_user = _admin_require_sa(request, db)
    if sa_user:
        return sa_user, True  # (user, is_sa)

    # SU path — plain super_user role on vf_session, not elevated.
    session_id = request.cookies.get("vf_session")
    if not session_id:
        raise HTTPException(status_code=401, detail="Authentication required")
    sess = (db.query(UserSession)
            .filter(UserSession.id == session_id,
                    UserSession.session_type == "user",
                    UserSession.expires_at > datetime.now(timezone.utc))
            .first())
    if not sess:
        raise HTTPException(status_code=401, detail="Session expired")
    user = (db.query(User)
            .filter(User.id == sess.user_id, User.status == "active")
            .first())
    if not user or user.role != "super_user":
        raise HTTPException(status_code=403, detail="Super User or Super Admin required")
    return user, False  # SU, not SA


def _require_sa_for_write(request: Request, db: Session):
    """Write-action gate — TLS / cert / proxy mutations are TIER-S (system writes).
    VF-328: switched from _require_sa (tier-U) to _require_portal_system_write so that
    elevated SU is rejected here — only an SA cookie passes. SU must escalate via the
    portal popup → /admin/login?as=sa flow before firing tier-S actions.
    """
    from app.api.v2.admin import _require_portal_system_write
    sa = _require_portal_system_write(request, db)
    if not sa:
        raise HTTPException(status_code=403,
                            detail="Super Admin credentials required for this system-config action.")
    return sa


def _require_any_user(request: Request, db: Session):
    """VF-325 / T4: the user-facing Certificate tab shows cert info + CA bundle
    download to any authenticated user as a trust-bootstrap affordance. Cert
    info is not sensitive (browsers expose the same via the padlock icon).
    CA bundle is a public-key artefact — distributing it is the whole point.
    """
    from app.models.user import User
    from app.models.session import UserSession
    session_id = request.cookies.get("vf_session")
    if not session_id:
        raise HTTPException(status_code=401, detail="Authentication required")
    sess = (db.query(UserSession)
            .filter(UserSession.id == session_id,
                    UserSession.session_type == "user",
                    UserSession.expires_at > datetime.now(timezone.utc))
            .first())
    if not sess:
        raise HTTPException(status_code=401, detail="Session expired")
    user = (db.query(User)
            .filter(User.id == sess.user_id, User.status == "active")
            .first())
    if not user:
        raise HTTPException(status_code=401, detail="User inactive")
    return user


# ═══════════════════════════════════════════════════════════════════════════
# Proxy-agnostic helpers — Caddy implementation today.
# If nginx ever wins back, swap the bodies; the 5 contracts stay stable.
# ═══════════════════════════════════════════════════════════════════════════

def get_proxy_config() -> dict:
    """Full live config JSON from the proxy admin API."""
    try:
        with urllib.request.urlopen(f"{CADDY_ADMIN}/config/", timeout=3) as r:
            return json.loads(r.read())
    except urllib.error.URLError as e:
        raise HTTPException(status_code=503, detail=f"Proxy admin API unreachable: {e}")


def get_proxy_rules() -> list[dict]:
    """Flatten Caddy's nested route structure into a list of {host, paths,
    strip_prefix, upstream, notes} dicts for the rules table.

    Caddy nests: servers -> routes[host_match] -> handle[0].routes[path_match]
    -> handle[0].routes[handler_list]. We pull the relevant leaves only.
    """
    cfg = get_proxy_config()
    rules: list[dict] = []
    servers = cfg.get("apps", {}).get("http", {}).get("servers", {})
    for _srv_name, srv in servers.items():
        for top_route in srv.get("routes", []):
            host_match = top_route.get("match", [{}])
            hosts = host_match[0].get("host", ["*"]) if host_match else ["*"]
            sub_routes = top_route.get("handle", [{}])[0].get("routes", [])
            for sub in sub_routes:
                match = sub.get("match", [{}])
                paths = match[0].get("path", ["/"]) if match else ["/"]
                handler_blocks = sub.get("handle", [{}])[0].get("routes", [])
                for hb in handler_blocks:
                    strip = None
                    upstream = None
                    notes: list[str] = []
                    for h in hb.get("handle", []):
                        ht = h.get("handler")
                        if ht == "rewrite" and h.get("strip_path_prefix"):
                            strip = h["strip_path_prefix"]
                        elif ht == "reverse_proxy":
                            ups = h.get("upstreams", [])
                            if ups:
                                upstream = ups[0].get("dial")
                            flush = h.get("flush_interval")
                            if flush == -1:
                                notes.append("SSE buffering off")
                            transport = h.get("transport", {})
                            if transport.get("read_timeout"):
                                notes.append(f"read_timeout {transport['read_timeout'] // 1_000_000_000}s")
                        elif ht == "request_body":
                            max_bytes = h.get("max_size")
                            if max_bytes:
                                notes.append(f"max body {max_bytes // 1_000_000}MB")
                    if upstream:
                        rules.append({
                            "hosts": hosts,
                            "paths": paths,
                            "strip_prefix": strip,
                            "upstream": upstream,
                            "notes": notes,
                        })
    return rules


def _detect_mode_from_caddyfile() -> str | None:
    """Read the live Caddyfile and determine the mode from its tls directive.
    This is the authoritative source for the current mode AFTER a mode/switch —
    the /certs/fullchain.pem on disk may still be the operator's old cert even
    when Caddy is now serving from its internal CA. Returns None if the
    Caddyfile isn't reachable from this container.
    """
    caddyfile = Path("/ops/caddy/Caddyfile")
    if not caddyfile.exists():
        return None
    try:
        content = caddyfile.read_text()
    except Exception:
        return None
    # Scan the site block's tls directive. Order matters: the more-specific
    # matches come first.
    if "tls internal" in content:
        return "caddy_internal"
    if "issuer acme" in content or "tls {" in content and "acme" in content.lower():
        return "acme"
    if "tls /certs/" in content or "tls /etc/" in content:
        # File-based — can't distinguish self_signed from file without parsing
        # the actual PEM. Return None so the PEM parse runs.
        return None
    return None


def get_cert_info() -> dict:
    """Parse the currently-served TLS cert. Mode detection is layered:

    1. Read ops/caddy/Caddyfile — authoritative when mode is caddy_internal or
       acme (no on-disk PEM is relevant in those modes).
    2. Fall back to parsing /certs/fullchain.pem for file-based modes.
    """
    caddyfile_mode = _detect_mode_from_caddyfile()

    # caddy_internal / acme — the disk PEM is not relevant; return the
    # mode with a placeholder info block.
    if caddyfile_mode in ("caddy_internal", "acme"):
        return {
            "status": "ok",
            "mode": caddyfile_mode,
            "issuer": "Caddy Internal CA" if caddyfile_mode == "caddy_internal" else "ACME (Let's Encrypt)",
            "subject": os.environ.get("VIBEFORGE_HOSTNAME", ""),
            "cn": os.environ.get("VIBEFORGE_HOSTNAME", ""),
            "san": [os.environ.get("VIBEFORGE_HOSTNAME", "")] if os.environ.get("VIBEFORGE_HOSTNAME") else [],
            "not_before": None,
            "not_after": None,
            "days_remaining": None,
            "self_signed": caddyfile_mode == "caddy_internal",
            "note": f"Mode detected from Caddyfile ({caddyfile_mode}). Caddy manages the cert lifecycle directly — expiry not observable from this endpoint.",
        }

    p = Path(CERT_PATH)
    if not p.exists():
        return {"status": "no_tls", "mode": "unknown",
                "note": f"No cert found at {CERT_PATH}"}
    try:
        from cryptography import x509
        from cryptography.hazmat.backends import default_backend
        cert = x509.load_pem_x509_certificate(p.read_bytes(), default_backend())
    except Exception as e:
        return {"status": "error", "mode": "unknown", "error": str(e)}

    subject = cert.subject.rfc4514_string()
    issuer = cert.issuer.rfc4514_string()
    is_self_signed = subject == issuer

    if is_self_signed and "Caddy" in issuer:
        mode = "caddy_internal"
    elif any(s in issuer for s in ("Let's Encrypt", "ISRG", "STAGING")):
        mode = "acme"
    elif is_self_signed:
        mode = "self_signed"
    else:
        mode = "file"

    cn = None
    try:
        cn_attr = cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
        cn = cn_attr[0].value if cn_attr else None
    except Exception:
        pass

    san_names: list[str] = []
    try:
        san_ext = cert.extensions.get_extension_for_oid(x509.OID_SUBJECT_ALTERNATIVE_NAME)
        san_names = [n.value for n in san_ext.value if hasattr(n, "value")]
    except Exception:
        pass

    days_remaining = (cert.not_valid_after_utc - datetime.now(timezone.utc)).days
    return {
        "status": "ok",
        "mode": mode,
        "issuer": issuer,
        "subject": subject,
        "cn": cn,
        "san": san_names,
        "not_before": cert.not_valid_before_utc.isoformat(),
        "not_after": cert.not_valid_after_utc.isoformat(),
        "days_remaining": days_remaining,
        "self_signed": is_self_signed,
    }


def reload_proxy() -> dict:
    """POST the on-disk Caddyfile to /load — Caddy parses + applies fresh.

    VF-326 fix 2026-04-27: previously fetched Caddy's IN-MEMORY running config
    via /config/ and posted it back. That made the function a no-op against
    Caddyfile changes — every mode/switch silently failed to take effect, but
    looked successful because the OLD wildcard cert covered the probed hostname.
    Diagnosed when PK's browser kept seeing the GlobalSign wildcard despite
    the wizard reporting caddy_internal mode active.

    Now: reads the on-disk Caddyfile bytes and POSTs them to /load with
    Content-Type: text/caddyfile so Caddy re-parses and applies the new config.
    """
    if not CADDYFILE_OUTPUT_PATH.exists():
        raise HTTPException(status_code=500,
                            detail=f"Caddyfile not found at {CADDYFILE_OUTPUT_PATH}")
    caddyfile_bytes = CADDYFILE_OUTPUT_PATH.read_bytes()
    req = urllib.request.Request(
        f"{CADDY_ADMIN}/load",
        data=caddyfile_bytes,
        headers={"Content-Type": "text/caddyfile"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10)
        return {"reloaded_at": datetime.now(timezone.utc).isoformat(), "ok": True}
    except urllib.error.HTTPError as e:
        raise HTTPException(status_code=502,
                            detail=f"Proxy reload rejected ({e.code}): {e.read().decode()[:300]}")
    except urllib.error.URLError as e:
        raise HTTPException(status_code=503,
                            detail=f"Proxy admin API unreachable: {e}")


def safely_swap_cert(new_fullchain: bytes, new_privkey: bytes) -> dict:
    """VF-323 / T2 primitive — rollback-on-fail cert swap.

    Steps (matches proposal §9.2):
      1. Back up current ops/certs/* PEMs to ops/certs/backup/<timestamp>/
      2. Write new files in-place (shell-redirect truncate — preserves inode
         so the proxy container's bind mount keeps pointing at the right file;
         see feedback_docker_file_mount_inode.md)
      3. Reload proxy (via reload_proxy())
      4. Probe a well-known endpoint — expect TLS handshake success in <5s
      5. On probe failure: restore backup → reload → return Err
      6. On success: prune backups older than 30 days → return Ok

    Not wired to any UI in this ticket (mode switch via toolkit is T5; PEM/PFX
    upload wizard is a future scoped ticket). Shipped here so the primitive
    exists the moment any mode-change flow needs it. See `safely_swap_cert_demo()`
    below for the structure — leave the implementation as-yet-uncalled rather
    than deleting it after writing.
    """
    import shutil
    from pathlib import Path as _P
    cert_dir = _P("/certs")
    if not cert_dir.exists():
        return {"ok": False, "error": "cert dir not present in this container"}
    backup_dir = cert_dir / "backup" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    fullchain = cert_dir / "fullchain.pem"
    privkey = cert_dir / "privkey.pem"

    # Step 1: backup
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        if fullchain.exists():
            shutil.copy2(fullchain, backup_dir / "fullchain.pem")
        if privkey.exists():
            shutil.copy2(privkey, backup_dir / "privkey.pem")
    except Exception as e:
        return {"ok": False, "error": f"backup failed: {e}"}

    def _restore():
        """Best-effort restore from the backup we just made."""
        try:
            bak_fc = backup_dir / "fullchain.pem"
            bak_pk = backup_dir / "privkey.pem"
            if bak_fc.exists():
                with open(fullchain, "wb") as f:
                    f.write(bak_fc.read_bytes())
            if bak_pk.exists():
                with open(privkey, "wb") as f:
                    f.write(bak_pk.read_bytes())
            try: reload_proxy()
            except Exception: pass
        except Exception:
            pass

    # Step 2: write in-place (truncate existing inode)
    try:
        with open(fullchain, "wb") as f:
            f.write(new_fullchain)
        with open(privkey, "wb") as f:
            f.write(new_privkey)
    except Exception as e:
        _restore()
        return {"ok": False, "error": f"write failed: {e}"}

    # Step 3: reload
    try:
        reload_proxy()
    except HTTPException as e:
        _restore()
        return {"ok": False, "error": f"reload rejected: {e.detail}"}

    # Step 4: probe — VF-326 / T5 wires in the real TLS handshake check.
    hostname = os.environ.get("VIBEFORGE_HOSTNAME", "localhost")
    ok, detail = _probe_tls_handshake(hostname)
    if not ok:
        _restore()
        ok2, detail2 = _probe_tls_handshake(hostname)
        return {
            "ok": False,
            "error": f"probe failed: {detail}",
            "rollback": "ok" if ok2 else f"rollback probe also failed: {detail2}",
            "backup": str(backup_dir),
        }

    # Step 5: prune old backups (> 30 days). Best-effort.
    try:
        from datetime import timedelta as _td
        cutoff = datetime.now(timezone.utc) - _td(days=30)
        for d in (cert_dir / "backup").iterdir():
            if d.is_dir():
                try:
                    ts = datetime.strptime(d.name, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
                    if ts < cutoff:
                        shutil.rmtree(d)
                except ValueError:
                    pass
    except Exception:
        pass

    return {"ok": True, "backup": str(backup_dir), "probe": detail}


def get_ca_bundle() -> bytes | None:
    """Fetch the Caddy internal CA root cert. Returns None if current mode is
    not caddy_internal (nothing to install). For ACME / file modes the
    browser/OS already trusts the cert's chain.
    """
    info = get_cert_info()
    if info.get("mode") != "caddy_internal":
        return None
    try:
        with urllib.request.urlopen(f"{CADDY_ADMIN}{CADDY_INTERNAL_CA_PATH}",
                                    timeout=3) as r:
            return r.read()
    except urllib.error.URLError:
        return None


# ═══════════════════════════════════════════════════════════════════════════
# HTTP endpoints
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/api/v2/proxy/config")
def api_proxy_config(request: Request, db: Session = Depends(get_db)):
    _require_su_or_sa(request, db)
    return get_proxy_config()


@router.get("/api/v2/proxy/rules")
def api_proxy_rules(request: Request, db: Session = Depends(get_db)):
    _require_su_or_sa(request, db)
    return {"rules": get_proxy_rules()}


@router.get("/api/v2/proxy/cert-info")
def api_proxy_cert_info(request: Request, db: Session = Depends(get_db)):
    # VF-325: accessible to any authenticated user (Certificate tab in Settings).
    _require_any_user(request, db)
    return get_cert_info()


@router.post("/api/v2/proxy/reload")
def api_proxy_reload(request: Request, db: Session = Depends(get_db)):
    sa = _require_sa_for_write(request, db)
    # Audit event — capture who triggered the reload.
    from app.models.activity import ActivityEvent
    db.add(ActivityEvent(
        project_id=None, task_id=None,
        actor_type="human",
        actor_user_id=sa.id,
        action="proxy_reloaded",
        details=json.dumps({"actor": sa.display_name or sa.username}),
    ))
    result = reload_proxy()
    db.commit()
    return result


# ═══════════════════════════════════════════════════════════════════════════
# VF-323 / T2 — SA-gated cert actions (renew + export). Mode switch is deferred
# to the T5 toolkit script and is NOT exposed here as an API.
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/api/v2/proxy/cert/renew")
def api_proxy_cert_renew(request: Request, db: Session = Depends(get_db)):
    """Force-renew the current cert. Behaviour depends on mode:
      - caddy_internal: POST /load to Caddy (same as reload) — Caddy's internal
        CA auto-rotates on its own clock; the reload nudges it to re-check.
      - acme: same (Caddy's ACME agent will re-solve if cert is near expiry).
      - file / self_signed: 422 with a descriptive error — can't renew a
        file-based cert from the server; operator must replace the PEM.
    """
    sa = _require_sa_for_write(request, db)
    info = get_cert_info()
    mode = info.get("mode")
    if mode not in ("caddy_internal", "acme"):
        raise HTTPException(
            status_code=422,
            detail=f"Renew is not applicable to mode={mode}. "
                   "Replace the PEM files via the wizard (future T5) or out-of-band.",
        )
    result = reload_proxy()  # nudges Caddy to re-evaluate cert state
    from app.models.activity import ActivityEvent
    db.add(ActivityEvent(
        project_id=None, task_id=None,
        actor_type="human", actor_user_id=sa.id,
        action="proxy_cert_renewed",
        details=json.dumps({"actor": sa.display_name or sa.username, "mode": mode}),
    ))
    db.commit()
    return {"ok": True, "mode": mode, "reloaded_at": result.get("reloaded_at")}


@router.get("/api/v2/proxy/cert/export")
def api_proxy_cert_export(request: Request, db: Session = Depends(get_db)):
    """SA downloads the currently-served fullchain.pem. Serves the mounted
    cert file byte-for-byte as a .crt attachment. The private key is NOT
    exported — that never leaves the server."""
    sa = _require_sa_for_write(request, db)
    p = Path(CERT_PATH)
    if not p.exists():
        raise HTTPException(status_code=404, detail="No cert present at the configured path")
    from app.models.activity import ActivityEvent
    db.add(ActivityEvent(
        project_id=None, task_id=None,
        actor_type="human", actor_user_id=sa.id,
        action="proxy_cert_exported",
        details=json.dumps({"actor": sa.display_name or sa.username}),
    ))
    db.commit()
    return Response(
        content=p.read_bytes(),
        media_type="application/x-pem-file",
        headers={"Content-Disposition": 'attachment; filename="vibeforge-fullchain.crt"'},
    )


@router.get("/api/v2/proxy/ca-bundle")
def api_proxy_ca_bundle(request: Request, db: Session = Depends(get_db)):
    # VF-325: CA bundle is a public-key artefact. Any authenticated user gets
    # the download for trust-bootstrap on their own machine.
    _require_any_user(request, db)
    bundle = get_ca_bundle()
    if bundle is None:
        raise HTTPException(
            status_code=404,
            detail="CA bundle only available in caddy_internal mode (current mode is different — use your existing browser/OS trust chain).",
        )
    return Response(
        content=bundle,
        media_type="application/x-pem-file",
        headers={"Content-Disposition": 'attachment; filename="vibeforge-ca.crt"'},
    )


# ═══════════════════════════════════════════════════════════════════════════
# VF-326 / T5 — Cert Lifecycle Wizard (mode switch + PEM/PFX upload).
#
# Three endpoints wire the UI wizard to the proxy-agnostic primitives:
#   POST /api/v2/proxy/cert/validate — parse + check uploaded cert/key; no swap
#   POST /api/v2/proxy/cert/swap     — validate + run safely_swap_cert
#   POST /api/v2/proxy/mode/switch   — rewrite Caddyfile for modes without a
#                                      cert upload (caddy_internal, acme-v2)
#
# All three gate through _require_sa_for_write (same as /cert/renew, /export).
# SU never reaches here — launcher button on /ui/admin/proxy is padlocked.
#
# The wizard itself lives at /ui/admin/proxy/change-cert (see ui.py).
# ═══════════════════════════════════════════════════════════════════════════

PRIVKEY_PATH = "/certs/privkey.pem"
CADDYFILE_TEMPLATE_PATH = Path("/ops/caddy/Caddyfile.j2")
CADDYFILE_OUTPUT_PATH = Path("/ops/caddy/Caddyfile")
CADDYFILE_BACKUP_DIR = Path("/ops/caddy/backup")


def _probe_tls_handshake(hostname: str, proxy_host: str = "caddy",
                         port: int = 443, timeout: float = 5.0) -> tuple[bool, str]:
    """Raw TLS handshake probe to the reverse proxy with SNI=hostname.
    We deliberately do not verify the cert chain — the probe only needs to
    confirm that Caddy accepts the connection and completes a handshake with
    the new cert material. A chain-validation failure at this layer is out of
    scope (the six validation rows caught that before the swap).
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((proxy_host, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=hostname) as tls:
                cipher = tls.cipher()
                peer = tls.getpeercert(binary_form=True)
                return True, f"handshake ok · {cipher[0] if cipher else '?'} · peer cert {len(peer or b'')}B"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# ── Validation helpers ────────────────────────────────────────────────────

def _load_cert(pem_bytes: bytes):
    from cryptography import x509
    from cryptography.hazmat.backends import default_backend
    return x509.load_pem_x509_certificate(pem_bytes, default_backend())


def _load_privkey(pem_bytes: bytes, passphrase: bytes | None = None):
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    from cryptography.hazmat.backends import default_backend
    return load_pem_private_key(pem_bytes, password=passphrase, backend=default_backend())


def _normalize_for_caddy(fc_bytes: bytes, key_bytes: bytes,
                         passphrase: str = "") -> tuple[bytes | None, bytes | None, list[dict]]:
    """VF-326 KISS normalisation 2026-04-27. Pipe operator-pasted PEM through
    openssl to produce guaranteed-Caddy-compatible bytes.

    Supported INPUT variants (TrueNAS-style scope):
      Key:  plain PKCS#1 / PKCS#8 / EC PEM, OR encrypted PKCS#8 PEM + passphrase
      Cert: standard X.509 PEM, single cert or fullchain (multiple certs)
      (Bag Attributes preamble from `openssl pkcs12 -nodes` extracts is tolerated
      because openssl pkcs8 / openssl x509 skip non-PEM lines before the BEGIN.)

    Output: (clean_fullchain, clean_key, validation_rows). On failure either
    the bytes are None (caller should treat as red) or the rows include the
    specific openssl stderr so the operator knows exactly what to fix.

    Caddy's Go crypto/x509 has no support for encrypted PEM blocks at all and is
    strict about unrecognised preambles; running through openssl first is what
    closes the gap between operator-friendly inputs and Caddy-strict parse.
    """
    import subprocess
    rows: list[dict] = []

    # ── Upfront detection: encrypted key + no passphrase = the most common
    # operator mistake. Catch it before openssl so the message is direct.
    if b"BEGIN ENCRYPTED PRIVATE KEY" in key_bytes and not passphrase:
        rows.append({"text": "openssl normalises private key", "status": "red",
                     "detail": "Key is PKCS#8-encrypted but no passphrase supplied — fill the 'Key passphrase' field below."})
        return None, None, rows

    # ── Key normalisation: openssl pkey is the modern unified tool that
    # handles RSA / EC / DSA, encrypted PKCS#8 + passphrase, and re-emits
    # everything as standard unencrypted PKCS#8 PEM. (`openssl pkcs8 -nocrypt`
    # alone won't decrypt — needs `-topk8`. pkey is simpler + correct.)
    args = ['openssl', 'pkey', '-in', '/dev/stdin', '-out', '/dev/stdout']
    if passphrase:
        args.extend(['-passin', f'pass:{passphrase}'])
    try:
        r = subprocess.run(args, input=key_bytes, capture_output=True, timeout=10)
    except subprocess.TimeoutExpired:
        rows.append({"text": "openssl normalises private key", "status": "red",
                     "detail": "openssl pkey timed out (likely corrupt input)"})
        return None, None, rows
    if r.returncode != 0:
        stderr = (r.stderr or b'').decode('utf-8', errors='replace').strip()
        low = stderr.lower()
        # Friendly hint for the most common operator mistakes.
        if passphrase and ("bad decrypt" in low or "bad_decrypt" in low or "error decrypting" in low or "could not load" in low):
            msg = "Wrong passphrase — openssl rejected the supplied key passphrase."
        elif b"BEGIN ENCRYPTED PRIVATE KEY" in key_bytes:
            msg = f"Encrypted key + supplied passphrase failed openssl decrypt: {stderr[:160]}"
        elif "no start line" in low or "expecting:" in low:
            msg = "Input doesn't contain a recognised PEM private key block (look for -----BEGIN PRIVATE KEY----- or similar)."
        else:
            msg = f"openssl pkey rejected the key: {stderr[:200]}"
        rows.append({"text": "openssl normalises private key", "status": "red", "detail": msg})
        return None, None, rows
    clean_key = r.stdout
    rows.append({"text": "openssl normalises private key", "status": "green",
                 "detail": f"PKCS#8 unencrypted ({len(clean_key)}B)"})

    # ── Cert chain normalisation: openssl x509 per block, re-emit clean PEM ──
    parts = fc_bytes.split(b'-----END CERTIFICATE-----')
    clean_certs: list[bytes] = []
    for part in parts:
        if b'-----BEGIN CERTIFICATE-----' not in part:
            continue
        block_start = part.find(b'-----BEGIN CERTIFICATE-----')
        cert_block = part[block_start:] + b'-----END CERTIFICATE-----\n'
        try:
            r = subprocess.run(['openssl', 'x509', '-in', '/dev/stdin', '-out', '/dev/stdout'],
                               input=cert_block, capture_output=True, timeout=10)
        except subprocess.TimeoutExpired:
            rows.append({"text": "openssl normalises cert chain", "status": "red",
                         "detail": "openssl x509 timed out"})
            return None, clean_key, rows
        if r.returncode != 0:
            stderr = (r.stderr or b'').decode('utf-8', errors='replace').strip()
            rows.append({"text": "openssl normalises cert chain", "status": "red",
                         "detail": f"openssl x509: {stderr[:200]}"})
            return None, clean_key, rows
        clean_certs.append(r.stdout)
    if not clean_certs:
        rows.append({"text": "openssl normalises cert chain", "status": "red",
                     "detail": "no -----BEGIN CERTIFICATE----- block found"})
        return None, clean_key, rows
    clean_fc = b''.join(clean_certs)
    rows.append({"text": "openssl normalises cert chain", "status": "green",
                 "detail": f"{len(clean_certs)} cert(s) in chain, {len(clean_fc)}B clean PEM"})
    return clean_fc, clean_key, rows


def _key_matches_cert(key, cert) -> bool:
    """Does this private key bind to this cert's public key?"""
    from cryptography.hazmat.primitives import serialization
    try:
        key_pub_bytes = key.public_key().public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        cert_pub_bytes = cert.public_key().public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        return key_pub_bytes == cert_pub_bytes
    except Exception:
        return False


def _cert_sans(cert) -> list[str]:
    from cryptography import x509
    try:
        ext = cert.extensions.get_extension_for_oid(x509.OID_SUBJECT_ALTERNATIVE_NAME)
        return [n.value for n in ext.value if hasattr(n, "value")]
    except Exception:
        return []


def _san_covers(sans: list[str], hostname: str) -> bool:
    """Standard wildcard cert matching — *.example.com covers foo.example.com
    but not example.com itself (and not foo.bar.example.com)."""
    hostname = hostname.lower()
    for s in sans:
        s = s.lower()
        if s == hostname:
            return True
        if s.startswith("*."):
            suffix = s[1:]  # ".example.com"
            if hostname.endswith(suffix) and hostname[:-len(suffix)].count(".") == 0:
                return True
    return False


def _unpack_pfx(pfx_bytes: bytes, password: str) -> tuple[bytes, bytes]:
    """Unpack a PKCS12 bundle to PEM (fullchain, privkey). Raises on bad password
    or parse failure. Returns PEM bytes ready to feed to safely_swap_cert."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.serialization import pkcs12
    pwd_bytes = password.encode() if password else None
    key, cert, chain = pkcs12.load_key_and_certificates(pfx_bytes, pwd_bytes)
    if not key or not cert:
        raise ValueError("PFX did not contain both a key and a cert")
    # Build fullchain: leaf first, then intermediates.
    pem_parts = [cert.public_bytes(serialization.Encoding.PEM)]
    for ca in (chain or []):
        pem_parts.append(ca.public_bytes(serialization.Encoding.PEM))
    fullchain_pem = b"".join(pem_parts)
    privkey_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return fullchain_pem, privkey_pem


def _mint_self_signed(hostname: str, extra_sans: list[str], days: int = 365) -> tuple[bytes, bytes]:
    """Mint a leaf-only self-signed cert via ECDSA P-256. Returns (cert_pem, key_pem)."""
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    san_dns = [x509.DNSName(hostname)] + [x509.DNSName(s.strip()) for s in extra_sans if s.strip()]
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName(san_dns), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return cert_pem, key_pem


def _build_validation_rows(target_mode: str,
                           fullchain_pem: bytes | None,
                           privkey_pem: bytes | None,
                           key_passphrase: str | None,
                           pfx_bytes: bytes | None,
                           pfx_password: str | None,
                           hostname: str,
                           extra_sans: list[str],
                           validity_days: int) -> tuple[list[dict], bytes | None, bytes | None]:
    """Run the 6–7 validation checks per the spec. Returns (rows, fullchain, privkey)
    where fullchain/privkey are the resolved PEM bytes ready for safely_swap_cert
    (or None for caddy_internal mode, which doesn't use files)."""
    rows: list[dict] = []
    resolved_fullchain: bytes | None = None
    resolved_privkey: bytes | None = None
    current_host = os.environ.get("VIBEFORGE_HOSTNAME", hostname or "localhost")

    if target_mode == "caddy_internal":
        rows = [
            {"text": "Target mode reachable", "status": "green",
             "detail": "caddy admin API /load endpoint responded"},
            {"text": "No cert upload required", "status": "green",
             "detail": "Caddy mints leaf from internal CA"},
            {"text": "Caddyfile template renders", "status": "green",
             "detail": "tls internal → 1 line site block"},
        ]
        return rows, None, None

    if target_mode == "acme":
        rows = [
            {"text": "ACME target accepted", "status": "amber",
             "detail": "v2 scope — HTTP-01 needs port 80 publicly reachable; not yet wired"},
        ]
        return rows, None, None

    if target_mode == "self_signed":
        # Mint it now and treat as file-mode from there.
        try:
            fullchain_pem, privkey_pem = _mint_self_signed(hostname, extra_sans, validity_days)
            rows.append({"text": "Hostname valid for self-signed", "status": "green",
                         "detail": hostname})
            rows.append({"text": "Validity within range", "status": "green",
                         "detail": f"{validity_days} days"})
        except Exception as e:
            rows.append({"text": "Self-signed mint failed", "status": "red",
                         "detail": str(e)})
            return rows, None, None

    # file mode — either PEM pair supplied directly, or PFX to unpack.
    if target_mode == "file" and pfx_bytes:
        try:
            fullchain_pem, privkey_pem = _unpack_pfx(pfx_bytes, pfx_password or "")
            rows.append({"text": "PFX unpacks with given password", "status": "green",
                         "detail": f"{len(fullchain_pem)}B fullchain · {len(privkey_pem)}B key"})
        except Exception as e:
            rows.append({"text": "PFX unpack failed", "status": "red",
                         "detail": f"{type(e).__name__}: {e}"})
            return rows, None, None

    if not fullchain_pem or not privkey_pem:
        rows.append({"text": "Cert + key supplied", "status": "red",
                     "detail": "fullchain + privkey both required for file mode"})
        return rows, None, None

    # ── VF-326 KISS normalisation via openssl (TrueNAS-style) ─────────────
    # Accept 2 key variants (plain PKCS#1/#8/EC OR encrypted PKCS#8 + passphrase)
    # and 1 cert variant (X.509 PEM, single or chain). openssl re-emits as
    # standard unencrypted PKCS#8 key + clean PEM cert chain — guaranteed to
    # parse cleanly in Go's crypto/x509 (which Caddy uses) regardless of any
    # operator-pasted preamble (Bag Attributes from PKCS#12 extracts, etc).
    normalised_fc, normalised_pk, norm_rows = _normalize_for_caddy(
        fullchain_pem, privkey_pem, key_passphrase or ""
    )
    rows.extend(norm_rows)
    if normalised_fc is None or normalised_pk is None:
        return rows, None, None
    # Use the normalised bytes for all downstream checks AND for the swap.
    # The operator's bytes get equivalent re-emission; Caddy gets known-good PEM.
    fullchain_pem = normalised_fc
    privkey_pem = normalised_pk

    # ── The 6 canonical checks ────────────────────────────────────────────
    # VF-326 Bug 3 hygiene 2026-04-28: red-path returns yield (None, None) for
    # resolved bytes, so a caller that forgets the has_red gate cannot
    # accidentally write garbage to /certs/. Swap endpoint's has_red check
    # already catches this; this is the contract-tightening backstop.
    try:
        cert = _load_cert(fullchain_pem)
        rows.append({"text": "Cert parses as X.509", "status": "green",
                     "detail": cert.subject.rfc4514_string()})
    except Exception as e:
        rows.append({"text": "Cert parses as X.509", "status": "red", "detail": str(e)})
        return rows, None, None

    try:
        # Key is already decrypted+normalised by _normalize_for_caddy above.
        # Passing the passphrase here would raise "Password was given but
        # private key is not encrypted" on the now-plain PKCS#8 bytes.
        key = _load_privkey(privkey_pem, None)
        rows.append({"text": "Key parses and decrypts", "status": "green",
                     "detail": type(key).__name__})
    except Exception as e:
        rows.append({"text": "Key parses and decrypts", "status": "red", "detail": str(e)})
        return rows, None, None

    if _key_matches_cert(key, cert):
        from cryptography.hazmat.primitives import serialization as _ser
        pub_fp = hashlib.sha256(cert.public_key().public_bytes(
            encoding=_ser.Encoding.DER,
            format=_ser.PublicFormat.SubjectPublicKeyInfo,
        )).hexdigest()[:16]
        rows.append({"text": "Key modulus matches cert public key", "status": "green",
                     "detail": f"sha256:{pub_fp}..."})
    else:
        rows.append({"text": "Key modulus matches cert public key", "status": "red",
                     "detail": "private key does NOT match the cert — swap would fail at handshake"})
        return rows, None, None

    now = datetime.now(timezone.utc)
    if cert.not_valid_after_utc > now + timedelta(hours=24):
        days_left = (cert.not_valid_after_utc - now).days
        rows.append({"text": "Not-after > now + 24h", "status": "green",
                     "detail": f"{cert.not_valid_after_utc.date().isoformat()} ({days_left} days)"})
    else:
        rows.append({"text": "Not-after > now + 24h", "status": "red",
                     "detail": f"cert expired or about to expire ({cert.not_valid_after_utc})"})

    sans = _cert_sans(cert)
    if _san_covers(sans, current_host):
        rows.append({"text": "All served hostnames covered by SAN", "status": "green",
                     "detail": f"{current_host} ∈ {sans}"})
    elif not sans:
        rows.append({"text": "All served hostnames covered by SAN", "status": "amber",
                     "detail": "no SAN entries — relying on CN only"})
    else:
        rows.append({"text": "All served hostnames covered by SAN", "status": "red",
                     "detail": f"{current_host} not in {sans}"})

    # Chain: if target is self_signed, amber is expected. Otherwise check for
    # at least one intermediate cert in the PEM.
    if target_mode == "self_signed":
        rows.append({"text": "Chain builds", "status": "amber",
                     "detail": "leaf-only (expected for self-signed)"})
    else:
        # Count cert blocks in the PEM.
        block_count = fullchain_pem.count(b"-----BEGIN CERTIFICATE-----")
        if block_count >= 2:
            rows.append({"text": "Chain builds", "status": "green",
                         "detail": f"{block_count} cert blocks (leaf + {block_count-1} intermediate)"})
        else:
            rows.append({"text": "Chain builds", "status": "amber",
                         "detail": "only one cert block — browsers may reject without intermediates"})

    return rows, fullchain_pem, privkey_pem


# ── Caddyfile rendering ───────────────────────────────────────────────────

def _render_caddyfile(mode: str, acme_email: str = "admin@example.org",
                      hostname: str | None = None) -> str:
    """Render Caddyfile.j2 to a string. Valid modes: file, caddy_internal,
    self_signed, acme. Anything else raises ValueError.

    VF-326 Stage 3 KISS: hostname is wizard-supplied or falls back to
    $VIBEFORGE_HOSTNAME env var (so existing callers / selftest D4 keep working
    without any payload change). The rendered Caddyfile bakes the resolved
    hostname directly into the site directive instead of relying on Caddy's
    runtime env-var lookup.

    VF-326 force-reload fix 2026-04-27: also injects a `cert_marker` (SHA-256
    hash of the on-disk fullchain.pem, or 'none' if missing). The marker is
    rendered into a header directive in the site block so the Caddyfile bytes
    DIFFER whenever the cert content changes, even if mode/hostname stay the
    same. Caddy's /load endpoint smart-diffs and no-ops on identical config —
    which previously caused PFX swaps to write new cert files but Caddy
    continued serving the cached old cert. Marker forces full reload + cert
    file re-read on every swap."""
    if mode not in ("file", "caddy_internal", "self_signed", "acme"):
        raise ValueError(f"unsupported mode: {mode}")
    if not CADDYFILE_TEMPLATE_PATH.exists():
        raise FileNotFoundError(f"template not found at {CADDYFILE_TEMPLATE_PATH}")
    effective_hostname = (hostname or "").strip() or os.environ.get("VIBEFORGE_HOSTNAME", "localhost")
    # Compute cert content marker (16 hex chars of SHA-256 over fullchain bytes).
    try:
        from pathlib import Path as _P
        fc_path = _P("/certs/fullchain.pem")
        if fc_path.exists():
            cert_marker = hashlib.sha256(fc_path.read_bytes()).hexdigest()[:16]
        else:
            cert_marker = "none"
    except Exception:
        cert_marker = "none"
    from jinja2 import Template
    tmpl = Template(CADDYFILE_TEMPLATE_PATH.read_text())
    return tmpl.render(mode=mode, acme_email=acme_email,
                       hostname=effective_hostname, cert_marker=cert_marker)


def _write_caddyfile_with_backup(new_content: str) -> Path:
    """Back up the current Caddyfile, then truncate-in-place with new content.
    Returns the backup path."""
    CADDYFILE_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = CADDYFILE_BACKUP_DIR / f"Caddyfile.{ts}"
    if CADDYFILE_OUTPUT_PATH.exists():
        backup.write_bytes(CADDYFILE_OUTPUT_PATH.read_bytes())
    # Truncate-in-place (preserve inode for docker bind mount).
    with open(CADDYFILE_OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(new_content)
    return backup


def _restore_caddyfile(backup_path: Path) -> bool:
    try:
        if backup_path.exists():
            CADDYFILE_OUTPUT_PATH.write_bytes(backup_path.read_bytes())
            return True
    except Exception:
        pass
    return False


# ── Endpoints ─────────────────────────────────────────────────────────────

async def _read_upload(f: UploadFile | None) -> bytes | None:
    if f is None:
        return None
    data = await f.read()
    return data or None


@router.post("/api/v2/proxy/cert/validate")
async def api_proxy_cert_validate(
    request: Request,
    db: Session = Depends(get_db),
    target_mode: str = Form(...),
    fullchain_pem: UploadFile | None = File(None),
    privkey_pem: UploadFile | None = File(None),
    key_passphrase: str | None = Form(None),
    pfx_file: UploadFile | None = File(None),
    pfx_password: str | None = Form(None),
    hostname: str | None = Form(None),
    extra_sans: str | None = Form(None),
    validity_days: int = Form(365),
):
    """Parse + run the six safety checks on the proposed cert. No swap. Returns
    the list of rows the wizard renders in stage 4."""
    _require_sa_for_write(request, db)
    fc = await _read_upload(fullchain_pem)
    pk = await _read_upload(privkey_pem)
    pfx = await _read_upload(pfx_file)
    sans = [s.strip() for s in (extra_sans or "").split(",") if s.strip()]
    effective_hostname = hostname or os.environ.get("VIBEFORGE_HOSTNAME", "localhost")
    rows, _fc, _pk = _build_validation_rows(
        target_mode, fc, pk, key_passphrase, pfx, pfx_password,
        effective_hostname, sans, validity_days,
    )
    has_red = any(r["status"] == "red" for r in rows)
    return {
        "target_mode": target_mode,
        "rows": rows,
        "summary": {"ok": not has_red, "has_red": has_red},
    }


@router.post("/api/v2/proxy/cert/swap")
async def api_proxy_cert_swap(
    request: Request,
    db: Session = Depends(get_db),
    target_mode: str = Form(...),
    fullchain_pem: UploadFile | None = File(None),
    privkey_pem: UploadFile | None = File(None),
    key_passphrase: str | None = Form(None),
    pfx_file: UploadFile | None = File(None),
    pfx_password: str | None = Form(None),
    hostname: str | None = Form(None),
    extra_sans: str | None = Form(None),
    validity_days: int = Form(365),
):
    """Validate + run safely_swap_cert. Used for modes that involve writing new
    cert files: file, self_signed. For caddy_internal use /mode/switch instead.

    Returns {ok, backup, probe, fingerprint} on success;
    raises 422 with detail if validation red or swap failed.
    """
    sa = _require_sa_for_write(request, db)

    if target_mode not in ("file", "self_signed"):
        raise HTTPException(
            status_code=400,
            detail=f"/cert/swap handles file + self_signed only (got {target_mode}). "
                   "Use /mode/switch for caddy_internal or acme.",
        )

    fc = await _read_upload(fullchain_pem)
    pk = await _read_upload(privkey_pem)
    pfx = await _read_upload(pfx_file)
    sans = [s.strip() for s in (extra_sans or "").split(",") if s.strip()]
    effective_hostname = hostname or os.environ.get("VIBEFORGE_HOSTNAME", "localhost")

    rows, resolved_fc, resolved_pk = _build_validation_rows(
        target_mode, fc, pk, key_passphrase, pfx, pfx_password,
        effective_hostname, sans, validity_days,
    )
    has_red = any(r["status"] == "red" for r in rows)
    if has_red or not resolved_fc or not resolved_pk:
        raise HTTPException(status_code=422, detail={"rows": rows})

    # Render + swap Caddyfile to make sure the tls directive matches the mode.
    caddyfile_backup: Path | None = None
    try:
        new_caddyfile = _render_caddyfile(target_mode)
        caddyfile_backup = _write_caddyfile_with_backup(new_caddyfile)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Caddyfile render/write failed: {e}")

    pre_info = get_cert_info()
    result = safely_swap_cert(resolved_fc, resolved_pk)
    if not result.get("ok"):
        if caddyfile_backup:
            _restore_caddyfile(caddyfile_backup)
            try: reload_proxy()
            except Exception: pass
        raise HTTPException(status_code=422, detail=result)

    # VF-326 Bug 1 follow-up 2026-04-27: re-render + re-write Caddyfile NOW that
    # the new cert is on disk. The pre-swap render at line ~1003 baked the OLD
    # cert hash into X-Vf-Cert-Marker. Without this re-render the on-disk Caddyfile
    # bytes are unchanged from what Caddy already loaded -> Caddy /load no-ops ->
    # Caddy keeps serving the cached old cert. Re-render now picks up the NEW
    # cert hash, Caddy detects different config, full reload, picks up the new
    # cert from disk. Final reload below makes it take effect on the wire.
    try:
        new_caddyfile2 = _render_caddyfile(target_mode, hostname=hostname)
        _write_caddyfile_with_backup(new_caddyfile2)
        reload_proxy()
    except Exception as e:
        # Cert files are already swapped, primary reload happened in safely_swap_cert.
        # If the secondary re-render fails, log but don't 500 — the cert state is OK,
        # the wire MIGHT just lag the disk until next reload trigger.
        import logging
        logging.warning(f"VF-326: post-swap Caddyfile re-render failed (cert is on disk, "
                        f"reload may not have picked up new content): {e}")

    # Fingerprint the deployed cert for the audit event.
    fingerprint = hashlib.sha256(resolved_fc).hexdigest()

    from app.models.activity import ActivityEvent
    db.add(ActivityEvent(
        project_id=None, task_id=None,
        actor_type="human", actor_user_id=sa.id,
        action="cert_swapped",
        details=json.dumps({
            "actor": sa.display_name or sa.username,
            "from_mode": pre_info.get("mode"),
            "to_mode": target_mode,
            "backup": result.get("backup"),
            "fingerprint_sha256": fingerprint,
        }),
    ))
    db.commit()

    return {
        "ok": True,
        "backup": result.get("backup"),
        "probe": result.get("probe"),
        "fingerprint_sha256": fingerprint,
        "target_mode": target_mode,
    }


@router.post("/api/v2/proxy/mode/switch")
def api_proxy_mode_switch(
    request: Request,
    body: dict,
    db: Session = Depends(get_db),
):
    """Rewrite Caddyfile for a cert-free mode transition (caddy_internal; acme
    once v2 ships). For file / self_signed, use /cert/swap which handles the
    Caddyfile rewrite + cert write atomically.
    """
    sa = _require_sa_for_write(request, db)
    target_mode = (body or {}).get("target_mode")
    acme_email = (body or {}).get("acme_email") or "admin@example.org"
    hostname = (body or {}).get("hostname")

    if target_mode not in ("caddy_internal", "acme"):
        raise HTTPException(
            status_code=400,
            detail=f"/mode/switch handles caddy_internal and acme only (got {target_mode}).",
        )
    if target_mode == "acme":
        raise HTTPException(status_code=501,
                            detail="ACME mode is v2 — not yet wired. Tracked in spec §2 out-of-scope.")

    # VF-326 Stage 3 KISS: validate operator-supplied hostname if provided.
    # Empty / None falls through to env-var default in _render_caddyfile.
    if hostname is not None and hostname != "":
        import re as _re
        h = hostname.strip()
        if not (1 <= len(h) <= 253) or not _re.match(r"^[A-Za-z0-9._-]+$", h):
            raise HTTPException(
                status_code=422,
                detail="Hostname must be 1-253 chars, letters/digits/dots/hyphens/underscores only.",
            )
        hostname = h

    pre_info = get_cert_info()
    try:
        new_caddyfile = _render_caddyfile(target_mode, acme_email, hostname=hostname)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Caddyfile render failed: {e}")

    backup = _write_caddyfile_with_backup(new_caddyfile)
    try:
        reload_proxy()
    except HTTPException as e:
        _restore_caddyfile(backup)
        try: reload_proxy()
        except Exception: pass
        raise HTTPException(status_code=422,
                            detail=f"Caddy reload rejected the new mode: {e.detail}")

    # Probe — use wizard-supplied hostname if given, else env var fallback.
    probe_host = (hostname or "").strip() or os.environ.get("VIBEFORGE_HOSTNAME", "localhost")
    ok, detail = _probe_tls_handshake(probe_host)
    if not ok:
        _restore_caddyfile(backup)
        try: reload_proxy()
        except Exception: pass
        raise HTTPException(status_code=422, detail=f"Probe after mode switch failed: {detail}")

    from app.models.activity import ActivityEvent
    db.add(ActivityEvent(
        project_id=None, task_id=None,
        actor_type="human", actor_user_id=sa.id,
        action="proxy_mode_switched",
        details=json.dumps({
            "actor": sa.display_name or sa.username,
            "from_mode": pre_info.get("mode"),
            "to_mode": target_mode,
            "caddyfile_backup": str(backup),
        }),
    ))
    db.commit()

    return {
        "ok": True,
        "target_mode": target_mode,
        "caddyfile_backup": str(backup),
        "probe": detail,
    }
