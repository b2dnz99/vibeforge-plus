#!/usr/bin/env python3
"""VF-326 / T5 self-test — Cert Lifecycle Wizard end-to-end.

Exercises:
  - /api/v2/proxy/cert/validate — SA, SU, unauth gates + all 5 modes
  - /api/v2/proxy/cert/swap     — validation blocking, success path, fingerprint
  - /api/v2/proxy/mode/switch   — caddy_internal round-trip, ACME 501, gating
  - /ui/admin/proxy/change-cert — SA 200, SU 302-bounce, unauth 302-login
  - audit trail (cert_swapped + proxy_mode_switched events land)

Run via:
    docker compose exec app python scripts/vf_t5_selftest.py

The test is DESTRUCTIVE — it performs real mode switches on the proxy. It
captures the starting mode at boot and restores at the end. If you see the
env left in a non-original mode, the restore step failed — check the Caddyfile
backup dir (/ops/caddy/backup/<ts>/) for the previous content.
"""
from __future__ import annotations

import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.activity import ActivityEvent
from app.models.session import UserSession
from app.models.user import User

DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg2://vibeforge:vibeforge@db:5432/vibeforge",
)
BASE = "http://localhost:8000"

CHECKS = 0
FAILS = 0


def _ok(cond, msg):
    global CHECKS, FAILS
    CHECKS += 1
    mark = "OK  " if cond else "FAIL"
    if not cond:
        FAILS += 1
    print(f"  {mark} {msg}")


def _section(title):
    print(f"\n[{title}]")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def http_error_302(self, req, fp, code, msg, headers):
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)
    http_error_303 = http_error_302
    http_error_307 = http_error_302


_opener = urllib.request.build_opener(_NoRedirect())


def _call_json(path, method="GET", cookie=None, sa_cookie=None, json_body=None):
    headers = {"Accept": "application/json"}
    cookies = []
    if cookie:
        cookies.append(f"vf_session={cookie}")
    if sa_cookie:
        cookies.append(f"vf_sa_session={sa_cookie}")
    if cookies:
        headers["Cookie"] = "; ".join(cookies)
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, method=method, headers=headers, data=data)
    try:
        r = _opener.open(req, timeout=15)
        return r.status, r.read().decode(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers) if e.headers else {}


def _call_multipart(path, fields: dict, files: dict, cookie=None, sa_cookie=None):
    """Minimal multipart encoder — avoids bringing requests into the test."""
    boundary = "----vf_t5_boundary_" + uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        if value is None:
            continue
        parts.append(f"--{boundary}\r\n")
        parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n')
        parts.append(f"{value}\r\n")
    for name, (fname, content) in files.items():
        if content is None:
            continue
        parts.append(f"--{boundary}\r\n")
        parts.append(f'Content-Disposition: form-data; name="{name}"; filename="{fname}"\r\n')
        parts.append("Content-Type: application/octet-stream\r\n\r\n")
        body_bytes = b"".join([p.encode() if isinstance(p, str) else p for p in parts])
        parts = [body_bytes, content, b"\r\n"]
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join([p.encode() if isinstance(p, str) else p for p in parts])

    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    cookies = []
    if cookie:
        cookies.append(f"vf_session={cookie}")
    if sa_cookie:
        cookies.append(f"vf_sa_session={sa_cookie}")
    if cookies:
        headers["Cookie"] = "; ".join(cookies)
    req = urllib.request.Request(BASE + path, method="POST", headers=headers, data=body)
    try:
        r = _opener.open(req, timeout=15)
        return r.status, r.read().decode(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers) if e.headers else {}


def _mint_test_pair(hostname, days=365):
    """Mint a self-signed cert/key pair for testing via the same helper the
    prod code uses. Returns (fullchain_pem, privkey_pem) as bytes."""
    from app.api.v2.proxy import _mint_self_signed
    return _mint_self_signed(hostname, [], days=days)


def _mint_mismatched_pair(hostname):
    """Mint cert A + key B — used to test the key/cert binding check."""
    from app.api.v2.proxy import _mint_self_signed
    fc, _pk = _mint_self_signed(hostname, [], days=365)
    _fc2, pk2 = _mint_self_signed(hostname, [], days=365)
    return fc, pk2


def _mint_expired_cert(hostname):
    """Mint a cert whose not-after is already in the past."""
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=2))
            .not_valid_after(now - timedelta(hours=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
            .sign(key, hashes.SHA256()))
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return cert_pem, key_pem


def _mint_pfx(hostname, password):
    """Mint a self-signed cert, wrap into a PFX with the given password."""
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import pkcs12, BestAvailableEncryption
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=365))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
            .sign(key, hashes.SHA256()))
    enc = BestAvailableEncryption(password.encode()) if password else serialization.NoEncryption()
    return pkcs12.serialize_key_and_certificates(
        name=b"test-pfx", key=key, cert=cert, cas=None, encryption_algorithm=enc,
    )


def main():
    engine = create_engine(DB_URL)
    Session = sessionmaker(bind=engine)
    db = Session()

    print("=" * 66)
    print("  VF-326 / T5 SELF-TEST — Cert Lifecycle Wizard")
    print("=" * 66)

    # ── Grab an SU + an SA. Stacked-SA pattern post-VF-328: tier-S endpoints
    # require an actual SA cookie (vf_sa_session, session_type="sa") on top of
    # the SU vf_session. Plain SU (no SA stack) is rejected by tier-S.
    su = db.query(User).filter(User.role == "super_user", User.status == "active").first()
    _ok(su is not None, "active SU exists")
    sa_user = db.query(User).filter(User.role == "super_admin", User.status == "active").first()
    _ok(sa_user is not None, "active SA exists")

    hostname = os.environ.get("VIBEFORGE_HOSTNAME", "localhost")
    now = datetime.now(timezone.utc)
    cookie_su = str(uuid.uuid4())
    db.add(UserSession(id=cookie_su, user_id=su.id, session_type="user",
                       created_at=now, expires_at=now + timedelta(hours=1)))
    # Real SA cookie — vf_sa_session, session_type="sa". Used in tandem with
    # cookie_su to simulate the operator having escalated SU → SA via the
    # portal's tier-S popup (stacked SA). Pure break-glass would omit cookie_su.
    cookie_sa = str(uuid.uuid4())
    db.add(UserSession(id=cookie_sa, user_id=sa_user.id, session_type="sa",
                       created_at=now, expires_at=now + timedelta(minutes=30)))
    db.commit()

    # ── Capture starting state for later restore ──
    # We read the actual cert bytes + Caddyfile content so the D4 round-trip
    # can put the environment back to exactly what it was. Without this the
    # test would leave DEV with a freshly-minted self-signed cert instead of
    # the operator's original.
    s, body, _ = _call_json("/api/v2/proxy/cert-info", cookie=cookie_su, sa_cookie=cookie_sa)
    original_info = json.loads(body) if s == 200 else {}
    original_mode = original_info.get("mode", "unknown")
    from pathlib import Path as _P
    cert_dir = _P("/certs")
    caddyfile = _P("/ops/caddy/Caddyfile")
    original_fullchain = (cert_dir / "fullchain.pem").read_bytes() if (cert_dir / "fullchain.pem").exists() else None
    original_privkey = (cert_dir / "privkey.pem").read_bytes() if (cert_dir / "privkey.pem").exists() else None
    original_caddyfile = caddyfile.read_bytes() if caddyfile.exists() else None
    print(f"  (original mode = {original_mode}; fullchain {len(original_fullchain or b'')}B; Caddyfile {len(original_caddyfile or b'')}B)")

    # ──────────────────────────────────────────────────────────────────
    # A. Validate endpoint — auth gates
    # ──────────────────────────────────────────────────────────────────
    _section("A. validate — auth gates")
    s, _, _ = _call_multipart("/api/v2/proxy/cert/validate",
                              {"target_mode": "caddy_internal"}, {})
    _ok(s in (401, 403), f"unauth /cert/validate -> 401/403 (got {s})")

    s, _, _ = _call_multipart("/api/v2/proxy/cert/validate",
                              {"target_mode": "caddy_internal"}, {}, cookie=cookie_su)
    _ok(s == 403, f"plain SU /cert/validate -> 403 (got {s})")

    s, body, _ = _call_multipart("/api/v2/proxy/cert/validate",
                                 {"target_mode": "caddy_internal"}, {}, cookie=cookie_su, sa_cookie=cookie_sa)
    _ok(s == 200, f"elevated SA /cert/validate (caddy_internal) -> 200 (got {s})")
    d = json.loads(body)
    _ok(d.get("target_mode") == "caddy_internal", "response echoes target_mode")
    _ok(isinstance(d.get("rows"), list) and len(d["rows"]) >= 2, "rows returned for caddy_internal")

    # ──────────────────────────────────────────────────────────────────
    # B. Validate endpoint — file mode success + failure paths
    # ──────────────────────────────────────────────────────────────────
    _section("B. validate — file mode PEM+KEY")
    fc, pk = _mint_test_pair(hostname)
    s, body, _ = _call_multipart("/api/v2/proxy/cert/validate",
                                 {"target_mode": "file"},
                                 {"fullchain_pem": ("fullchain.pem", fc),
                                  "privkey_pem":  ("privkey.pem",  pk)},
                                 cookie=cookie_su, sa_cookie=cookie_sa)
    _ok(s == 200, f"valid PEM+KEY -> 200 (got {s})")
    d = json.loads(body)
    reds = [r for r in d.get("rows", []) if r["status"] == "red"]
    _ok(len(reds) == 0, f"valid pair has no red rows (got {reds})")

    _section("B2. validate — mismatched key")
    fc_bad, pk_bad = _mint_mismatched_pair(hostname)
    s, body, _ = _call_multipart("/api/v2/proxy/cert/validate",
                                 {"target_mode": "file"},
                                 {"fullchain_pem": ("fullchain.pem", fc_bad),
                                  "privkey_pem":  ("privkey.pem",  pk_bad)},
                                 cookie=cookie_su, sa_cookie=cookie_sa)
    _ok(s == 200, f"mismatched pair -> 200 (got {s})")
    d = json.loads(body)
    match_row = next((r for r in d.get("rows", []) if "modulus" in r["text"].lower()), None)
    _ok(match_row and match_row["status"] == "red", "mismatched pair flagged red on modulus row")

    _section("B3. validate — expired cert")
    fc_exp, pk_exp = _mint_expired_cert(hostname)
    s, body, _ = _call_multipart("/api/v2/proxy/cert/validate",
                                 {"target_mode": "file"},
                                 {"fullchain_pem": ("fullchain.pem", fc_exp),
                                  "privkey_pem":  ("privkey.pem",  pk_exp)},
                                 cookie=cookie_su, sa_cookie=cookie_sa)
    d = json.loads(body)
    exp_row = next((r for r in d.get("rows", []) if "not-after" in r["text"].lower()), None)
    _ok(exp_row and exp_row["status"] == "red", "expired cert flagged red on not-after row")

    _section("B4. validate — PFX happy path")
    pfx_bytes = _mint_pfx(hostname, "changeit")
    s, body, _ = _call_multipart("/api/v2/proxy/cert/validate",
                                 {"target_mode": "file", "pfx_password": "changeit"},
                                 {"pfx_file": ("cert.pfx", pfx_bytes)},
                                 cookie=cookie_su, sa_cookie=cookie_sa)
    d = json.loads(body)
    unpack_row = next((r for r in d.get("rows", []) if "pfx unpacks" in r["text"].lower()), None)
    _ok(unpack_row and unpack_row["status"] == "green", "PFX unpacks with correct password")

    _section("B5. validate — PFX wrong password")
    s, body, _ = _call_multipart("/api/v2/proxy/cert/validate",
                                 {"target_mode": "file", "pfx_password": "wrong-password"},
                                 {"pfx_file": ("cert.pfx", pfx_bytes)},
                                 cookie=cookie_su, sa_cookie=cookie_sa)
    d = json.loads(body)
    reds = [r for r in d.get("rows", []) if r["status"] == "red"]
    _ok(len(reds) >= 1, "wrong PFX password flagged red")

    _section("B6. validate — self_signed mode")
    s, body, _ = _call_multipart("/api/v2/proxy/cert/validate",
                                 {"target_mode": "self_signed",
                                  "hostname": hostname,
                                  "validity_days": "365"}, {},
                                 cookie=cookie_su, sa_cookie=cookie_sa)
    d = json.loads(body)
    # VF-326 2026-04-28: be specific — _normalize_for_caddy now emits an
    # "openssl normalises cert chain" row that ALSO matches "chain", and runs
    # BEFORE the canonical checks. Match the canonical "Chain builds" row only.
    chain_row = next((r for r in d.get("rows", []) if r["text"].lower() == "chain builds"), None)
    _ok(chain_row and chain_row["status"] == "amber",
        "self_signed chain row is amber (leaf-only expected)")

    # ──────────────────────────────────────────────────────────────────
    # C. Swap endpoint gates
    # ──────────────────────────────────────────────────────────────────
    _section("C. swap — auth gates")
    s, _, _ = _call_multipart("/api/v2/proxy/cert/swap",
                              {"target_mode": "self_signed", "hostname": hostname}, {})
    _ok(s in (401, 403), f"unauth /cert/swap -> 401/403 (got {s})")

    s, _, _ = _call_multipart("/api/v2/proxy/cert/swap",
                              {"target_mode": "self_signed", "hostname": hostname},
                              {}, cookie=cookie_su)
    _ok(s == 403, f"plain SU /cert/swap -> 403 (got {s})")

    _section("C2. swap — invalid input blocked")
    s, body, _ = _call_multipart("/api/v2/proxy/cert/swap",
                                 {"target_mode": "file"},
                                 {"fullchain_pem": ("fullchain.pem", fc_bad),
                                  "privkey_pem":  ("privkey.pem",  pk_bad)},
                                 cookie=cookie_su, sa_cookie=cookie_sa)
    _ok(s == 422, f"mismatched pair /cert/swap -> 422 (got {s})")

    _section("C3. swap — caddy_internal target rejected here")
    s, _, _ = _call_multipart("/api/v2/proxy/cert/swap",
                              {"target_mode": "caddy_internal"}, {}, cookie=cookie_su, sa_cookie=cookie_sa)
    _ok(s == 400, f"caddy_internal to /cert/swap -> 400 (got {s})")

    # ──────────────────────────────────────────────────────────────────
    # C4. swap — FULL E2E happy path with file-mode cert
    # Mints a fresh PEM+KEY pair, swaps it in via /cert/swap, verifies the
    # response carries backup + fingerprint, verifies cert-info reflects
    # the new cert, verifies cert_swapped audit event landed.
    # F's cleanup restores the original cert files captured at boot.
    # ──────────────────────────────────────────────────────────────────
    _section("C4. swap — file-mode E2E happy path")
    if original_mode in ("file", "self_signed"):
        pre_swap_events = (db.query(ActivityEvent)
                           .filter(ActivityEvent.action == "cert_swapped")
                           .count())
        fc_new, pk_new = _mint_test_pair(hostname)
        s, body, _ = _call_multipart("/api/v2/proxy/cert/swap",
                                     {"target_mode": "file"},
                                     {"fullchain_pem": ("fullchain.pem", fc_new),
                                      "privkey_pem":  ("privkey.pem",  pk_new)},
                                     cookie=cookie_su, sa_cookie=cookie_sa)
        _ok(s == 200, f"valid PEM+KEY /cert/swap -> 200 (got {s})")
        if s == 200:
            try:
                d = json.loads(body)
            except Exception:
                d = {}
            _ok(d.get("ok") is True, f"swap response ok=True (got {d.get('ok')})")
            _ok(bool(d.get("backup")), f"swap response includes backup path (got {str(d.get('backup'))[:80]!r})")
            fp = d.get("fingerprint_sha256") or ""
            _ok(len(fp) >= 32, f"swap response includes non-empty fingerprint_sha256 (len={len(fp)})")

            # Probe cert-info — should reflect the new cert.
            time.sleep(1)
            s2, body2, _ = _call_json("/api/v2/proxy/cert-info", cookie=cookie_su, sa_cookie=cookie_sa)
            _ok(s2 == 200, f"cert-info after swap -> 200 (got {s2})")
            try:
                info = json.loads(body2)
            except Exception:
                info = {}
            # Mode should still be 'file' (or self_signed if chain only) — definitely not caddy_internal.
            _ok(info.get("mode") in ("file", "self_signed"),
                f"cert-info mode is file/self_signed after swap (got {info.get('mode')})")

            # Verify cert_swapped audit event.
            db.expire_all()
            post_swap_events = (db.query(ActivityEvent)
                                .filter(ActivityEvent.action == "cert_swapped")
                                .count())
            _ok(post_swap_events > pre_swap_events,
                f"cert_swapped audit event landed ({pre_swap_events}->{post_swap_events})")

            # VF-326 Bug 1: verify the WIRE-served cert actually matches the
            # on-disk leaf. Catches the reload-no-op class of bug where Caddy
            # reports config loaded but keeps serving the stale cached cert.
            try:
                import ssl as _ssl
                import socket as _sock
                import hashlib as _h
                from pathlib import Path as _PP
                from cryptography import x509 as _x509
                from cryptography.hazmat.primitives.serialization import Encoding as _Enc
                # Parse on-disk leaf -> DER bytes -> SHA-256.
                disk_pem_all = _PP("/certs/fullchain.pem").read_bytes()
                first_block = disk_pem_all.split(b"-----END CERTIFICATE-----")[0] + b"-----END CERTIFICATE-----\n"
                disk_leaf = _x509.load_pem_x509_certificate(first_block)
                disk_leaf_sha = _h.sha256(disk_leaf.public_bytes(_Enc.DER)).hexdigest()
                # Fetch served cert from caddy on the docker network -> DER bytes -> SHA-256.
                ctx = _ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = _ssl.CERT_NONE
                with _sock.create_connection(("caddy", 443), timeout=5) as raw:
                    with ctx.wrap_socket(raw, server_hostname=hostname) as tls:
                        wire_der = tls.getpeercert(binary_form=True)
                wire_sha = _h.sha256(wire_der).hexdigest()
                _ok(wire_sha == disk_leaf_sha,
                    f"wire-served cert matches on-disk leaf (wire={wire_sha[:12]} disk_leaf={disk_leaf_sha[:12]})")
            except Exception as e:
                _ok(False, f"wire-cert verify failed: {e}")
    else:
        _ok(True, f"original mode={original_mode} — skipping cert/swap E2E (unsafe to overwrite)")

    # ──────────────────────────────────────────────────────────────────
    # D. mode/switch endpoint gates + round-trip
    # ──────────────────────────────────────────────────────────────────
    _section("D. mode/switch — auth gates")
    s, _, _ = _call_json("/api/v2/proxy/mode/switch", method="POST",
                         json_body={"target_mode": "caddy_internal"})
    _ok(s in (401, 403), f"unauth /mode/switch -> 401/403 (got {s})")

    s, _, _ = _call_json("/api/v2/proxy/mode/switch", method="POST",
                         cookie=cookie_su,
                         json_body={"target_mode": "caddy_internal"})
    _ok(s == 403, f"plain SU /mode/switch -> 403 (got {s})")

    _section("D2. mode/switch — ACME not yet wired")
    s, _, _ = _call_json("/api/v2/proxy/mode/switch", method="POST",
                         cookie=cookie_su, sa_cookie=cookie_sa,
                         json_body={"target_mode": "acme", "acme_email": "test@example.com"})
    _ok(s == 501, f"ACME /mode/switch -> 501 (got {s})")

    _section("D3. mode/switch — bad mode rejected")
    s, _, _ = _call_json("/api/v2/proxy/mode/switch", method="POST",
                         cookie=cookie_su, sa_cookie=cookie_sa,
                         json_body={"target_mode": "banana"})
    _ok(s == 400, f"unknown mode /mode/switch -> 400 (got {s})")

    # Round-trip mode switch IF we can sensibly restore.
    _section("D4. mode/switch — round-trip to caddy_internal and back")
    # CA bundle is gated on caddy_internal mode — should 404 in any other mode.
    if original_mode != "caddy_internal":
        s_ca0, _, _ = _call_json("/api/v2/proxy/ca-bundle", cookie=cookie_su)
        _ok(s_ca0 == 404, f"/ca-bundle in {original_mode} mode -> 404 expected (got {s_ca0})")

    if original_mode in ("file", "self_signed", "caddy_internal"):
        pre_events = (db.query(ActivityEvent)
                      .filter(ActivityEvent.action == "proxy_mode_switched")
                      .count())
        s, body, _ = _call_json("/api/v2/proxy/mode/switch", method="POST",
                                cookie=cookie_su, sa_cookie=cookie_sa,
                                json_body={"target_mode": "caddy_internal"})
        _ok(s == 200, f"/mode/switch caddy_internal -> 200 (got {s})")
        if s == 200:
            time.sleep(1)
            s2, body2, _ = _call_json("/api/v2/proxy/cert-info", cookie=cookie_su, sa_cookie=cookie_sa)
            info_after = json.loads(body2) if s2 == 200 else {}
            _ok(info_after.get("mode") == "caddy_internal",
                f"cert-info reflects caddy_internal (got {info_after.get('mode')})")

            # Full E2E with CA download: verify Caddy minted a CA we can fetch + parse.
            s_ca, body_ca, hdrs_ca = _call_json("/api/v2/proxy/ca-bundle", cookie=cookie_su)
            _ok(s_ca == 200, f"GET /ca-bundle in caddy_internal -> 200 (got {s_ca})")
            disp = hdrs_ca.get("Content-Disposition") or hdrs_ca.get("content-disposition", "")
            _ok("vibeforge-ca.crt" in disp, f"CA bundle download filename present (got {disp[:80]!r})")
            ctype = hdrs_ca.get("Content-Type") or hdrs_ca.get("content-type", "")
            _ok("pem" in ctype.lower(), f"CA bundle Content-Type is PEM (got {ctype!r})")
            # Parse + validate it's a real CA cert.
            try:
                from cryptography import x509
                ca_cert = x509.load_pem_x509_certificate(body_ca.encode())
                subj = ca_cert.subject.rfc4514_string()
                _ok(True, f"CA bundle parses as X.509 (subject={subj[:60]})")
                bc = ca_cert.extensions.get_extension_for_class(x509.BasicConstraints).value
                _ok(bc.ca is True, f"CA cert has basicConstraints CA:TRUE (got CA={bc.ca})")
                ku = ca_cert.extensions.get_extension_for_class(x509.KeyUsage).value
                _ok(ku.key_cert_sign is True, f"CA cert KeyUsage has keyCertSign (got {ku.key_cert_sign})")
            except Exception as e:
                _ok(False, f"CA bundle parsing/validation failed: {e}")

        db.expire_all()
        post_events = (db.query(ActivityEvent)
                       .filter(ActivityEvent.action == "proxy_mode_switched")
                       .count())
        _ok(post_events > pre_events,
            f"proxy_mode_switched audit event landed ({pre_events}->{post_events})")
    else:
        _ok(True, f"original mode={original_mode} — skipping round-trip (unsafe to switch)")

    # After cleanup (section F) restores original mode, /ca-bundle should 404 again
    # in non-caddy_internal modes — captured as G after the restore.

    # ──────────────────────────────────────────────────────────────────
    # D5. mode/switch — hostname plumbing (VF-326 Stage 3)
    # Tests the operator-supplied-hostname path without actually swapping
    # to a hostname that would fail the TLS probe.
    #   - Renderer honours custom hostname (Python-level, no API)
    #   - API rejects malformed hostname with 422
    #   - API accepts empty hostname (env-var fallback, doesn't 422 the validation)
    # ──────────────────────────────────────────────────────────────────
    _section("D5. mode/switch — hostname plumbing")
    try:
        from app.api.v2.proxy import _render_caddyfile as _rc
        rendered_custom = _rc("caddy_internal", hostname="example.local-test")
        _ok("example.local-test:443" in rendered_custom,
            "renderer bakes operator-supplied hostname into site directive")
        rendered_default = _rc("caddy_internal", hostname=None)
        _ok(":443 {" in rendered_default,
            "renderer with hostname=None falls back to env var (site directive present)")
        # Verify the SITE DIRECTIVE specifically uses Jinja-baked hostname (not Caddy env-var lookup).
        # Look at lines ending with :443 { — that's where the host appears.
        site_lines = [ln for ln in rendered_default.splitlines()
                      if ln.strip().endswith(":443 {") and not ln.lstrip().startswith("#")]
        _ok(site_lines and "{$VIBEFORGE_HOSTNAME}" not in site_lines[0],
            f"site directive is Jinja-baked, no Caddy env-var lookup (got: {site_lines[0] if site_lines else 'NONE'!r})")
    except Exception as e:
        _ok(False, f"renderer test failed: {e}")

    # API-level: malformed hostname rejected with 422.
    s, body, _ = _call_json("/api/v2/proxy/mode/switch", method="POST",
                            cookie=cookie_su, sa_cookie=cookie_sa,
                            json_body={"target_mode": "caddy_internal",
                                       "hostname": "bad host;rm -rf /"})
    _ok(s == 422, f"malformed hostname /mode/switch -> 422 (got {s})")
    if s == 422:
        try:
            d = json.loads(body)
            detail = d.get("detail", "")
            _ok("Hostname must be" in str(detail) or "hostname" in str(detail).lower(),
                f"validation error names hostname (got {str(detail)[:80]!r})")
        except Exception:
            _ok(False, "validation response not JSON")

    # API-level: empty hostname is accepted (treated as fallback to env var).
    # This proves the optional-param contract holds for legacy callers.
    s, _, _ = _call_json("/api/v2/proxy/mode/switch", method="POST",
                         cookie=cookie_su, sa_cookie=cookie_sa,
                         json_body={"target_mode": "caddy_internal", "hostname": ""})
    # Note: this actually performs a swap (then F restores). We only check that
    # validation didn't reject it (200 OR a downstream-specific error, NOT a
    # 422 hostname-validation failure).
    _ok(s != 422 or (s == 422 and "Hostname must be" not in body),
        f"empty hostname does NOT trip hostname-validation 422 (got {s})")

    # ──────────────────────────────────────────────────────────────────
    # E. UI page access
    # ──────────────────────────────────────────────────────────────────
    _section("E. /ui/admin/proxy/change-cert access")
    s, _, _ = _call_json("/ui/admin/proxy/change-cert")
    _ok(s == 302, f"unauthenticated -> 302 (got {s})")

    s, _, hdrs = _call_json("/ui/admin/proxy/change-cert", cookie=cookie_su)
    _ok(s == 302, f"plain SU -> 302 bounce (got {s})")
    loc = hdrs.get("location") or hdrs.get("Location", "")
    _ok("admin/login" in loc, f"SU redirected to /admin/login (got {loc})")

    s, body, _ = _call_json("/ui/admin/proxy/change-cert", cookie=cookie_su, sa_cookie=cookie_sa)
    _ok(s == 200, f"elevated SA -> 200 (got {s})")
    _ok("Change cert" in body or "change-cert" in body, "wizard HTML rendered")
    _ok("ap-pill-sa" in body, "SA role pill present in topbar (ap-pill-sa class)")
    _ok("Pick target mode" in body or "// STAGE 2" in body, "mode picker stage rendered")

    # ──────────────────────────────────────────────────────────────────
    # F. Cleanup — restore the environment to its original cert + Caddyfile
    # so DEV keeps serving the operator's real cert after the test. Without
    # this the test run would leave a freshly-minted self-signed cert and
    # require manual recovery.
    # ──────────────────────────────────────────────────────────────────
    _section("F. cleanup")
    try:
        if original_caddyfile:
            caddyfile.write_bytes(original_caddyfile)
        if original_fullchain and original_privkey:
            (cert_dir / "fullchain.pem").write_bytes(original_fullchain)
            (cert_dir / "privkey.pem").write_bytes(original_privkey)
        # Reload caddy to pick up the restored files + Caddyfile.
        from app.api.v2.proxy import reload_proxy as _reload
        try: _reload()
        except Exception: pass
        time.sleep(1)
        s_r, body_r, _ = _call_json("/api/v2/proxy/cert-info", cookie=cookie_su, sa_cookie=cookie_sa)
        info_after = json.loads(body_r) if s_r == 200 else {}
        _ok(info_after.get("mode") == original_mode,
            f"env restored to original mode={original_mode} (got {info_after.get('mode')})")
    except Exception as e:
        _ok(False, f"restore failed: {e}")

    # ──────────────────────────────────────────────────────────────────
    # G. Post-restore: /ca-bundle should 404 again in non-caddy_internal
    # modes. Proves the restore actually unwound the mode change.
    # ──────────────────────────────────────────────────────────────────
    if original_mode != "caddy_internal":
        _section("G. ca-bundle gating after restore")
        s_g, _, _ = _call_json("/api/v2/proxy/ca-bundle", cookie=cookie_su)
        _ok(s_g == 404, f"/ca-bundle in restored {original_mode} mode -> 404 (got {s_g})")

    db.query(UserSession).filter(UserSession.id.in_([cookie_su, cookie_sa])).delete(synchronize_session=False)
    # Remove the audit events we generated (best-effort — self_test actions
    # should not pollute the real audit trail).
    db.query(ActivityEvent).filter(
        ActivityEvent.action.in_(["proxy_mode_switched", "cert_swapped"]),
        ActivityEvent.actor_user_id == su.id,
        ActivityEvent.created_at >= now,
    ).delete(synchronize_session=False)
    db.commit()
    print("  test sessions + audit events cleaned")

    print("\n" + "=" * 66)
    if FAILS == 0:
        print("  ALL CHECKS GREEN")
    else:
        print(f"  {FAILS} / {CHECKS} FAILED")
    print("=" * 66)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
