import json

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from app.api.v2 import health, auth, tokens, ui, events, projects, members, contract, admin, bootstrap, admin_experimental, proxy, admin_portal, onboard
from app.db.session import engine
from app.db import base  # noqa: F401 - ensures models are registered

# WHY: redirect_slashes=False prevents 307 redirects that flip HTTPS to HTTP behind the reverse proxy
app = FastAPI(title="VibeForge+", version="2.0.0", redirect_slashes=False)


# ─── VF-357 (R2.7) ─── extra_forbidden translation to standard 422 shape ──
# Was: PATCH bodies silently dropped undocumented fields (200 + no-op).
# Claude Code review §6.2 — "worst kind of API gap." Now: PATCH body
# Pydantic models carry model_config = ConfigDict(extra='forbid'), which
# makes Pydantic raise a validation error (extra_forbidden type) when the
# client sends a field that's not on the allow-list. FastAPI surfaces that
# as RequestValidationError → default 422 with raw Pydantic detail array.
# This handler intercepts those specifically and reshapes to the standard
# 422 shape (code + detail + agent_remedy + human_visible) per the pinned
# 422-recoverable principle (memory feedback_422_recoverable_from_response_alone).
# Other validation errors (missing required fields, type mismatches) pass
# through with the default Pydantic shape — those already carry enough
# context to recover.
@app.exception_handler(RequestValidationError)
async def _vf357_validation_handler(request: Request, exc: RequestValidationError):
    errors = exc.errors() or []
    extra_forbidden_errors = [e for e in errors if e.get("type") == "extra_forbidden"]
    if extra_forbidden_errors and request.method.upper() in ("PATCH", "PUT"):
        rejected_fields = [e["loc"][-1] if e.get("loc") else "?" for e in extra_forbidden_errors]
        if len(rejected_fields) == 1:
            field_clause = f"Field '{rejected_fields[0]}' is not allowed"
        else:
            field_clause = f"Fields {rejected_fields} are not allowed"
        return JSONResponse(status_code=422, content={
            "detail": json.dumps({
                "code": "FIELD_NOT_ALLOWED_ON_PATCH",
                "detail": (
                    f"{field_clause} on this endpoint. PATCH bodies are strict allow-lists; "
                    "only documented fields are accepted to prevent silent drops. "
                    "(Was: silent 200 no-op pre-VF-357. See CUSTOMER-ONBOARD-FINDINGS IC-029.)"
                ),
                "agent_remedy": (
                    "Re-fetch /agentnotes for the project to see the current "
                    "endpoints.<resource> body allow-list. If the field you want "
                    "to mutate is creation-only, supersede the resource and recreate. "
                    "If the field belongs to a sibling resource (e.g. milestone "
                    "association on a phase), use that resource's PATCH endpoint "
                    "instead — e.g. PATCH /api/v2/phases/{id} for milestone_id, "
                    "added in VF-356."
                ),
                "rejected_fields": rejected_fields,
                "human_visible": True,
            }),
        })
    # Fall through to FastAPI's default validation-error shape for non-PATCH
    # or non-extra_forbidden validation errors.
    return JSONResponse(status_code=422, content={"detail": errors})


app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(health.router, prefix="/api/v2", tags=["health"])
app.include_router(auth.router, prefix="/api/v2", tags=["auth"])
app.include_router(tokens.router, prefix="/api/v2", tags=["tokens"])
app.include_router(ui.router, tags=["ui"])
app.include_router(events.router, tags=["events"])
app.include_router(projects.router, tags=["projects"])
app.include_router(members.router, tags=["members"])
app.include_router(contract.router, tags=["contract"])
app.include_router(admin.router, tags=["admin"])
app.include_router(bootstrap.router, tags=["bootstrap"])
app.include_router(admin_experimental.router, tags=["admin-experimental"])
app.include_router(proxy.router, tags=["proxy"])
app.include_router(admin_portal.router, tags=["admin-portal"])
app.include_router(onboard.router, tags=["onboard"])  # VF-353 customer-onboard mechanism


# Wave 2.0 (R2.7): mirror FastAPI's openapi.json at /api/v2/openapi.json so
# agents that probe the API-prefix path for schema discovery (Codex's pass-1
# self-recovery reflex on a 404 cluster) get the spec where they look.
# FastAPI's default openapi path stays at /openapi.json (root); this is a
# discoverability-belt-and-braces mirror, not a replacement. Also surfaces
# in the contract endpoints map.
@app.get("/api/v2/openapi.json", include_in_schema=False)
def _wave_2_0_openapi_mirror():
    return JSONResponse(content=app.openapi())


# ─── VF-368 (R2.7 wave 2.0.8 / CONTRACT_VERSION 2.14.0) ────────────────────
# OpenAPI public-route filter — default-deny + explicit-allow include list.
# Codex's blind cross-vendor non-security audit (2026-05-03/04) flagged that
# the public /openapi.json + /api/v2/openapi.json exposed admin/bootstrap/
# proxy/token/agent-lifecycle/drift-telemetry surfaces — broad route-map
# leak, not full data leak, but pre-RC concern. KISS fix: filter the schema
# at serve time, keeping ONLY the agent-discovery + project-data surfaces
# agents legitimately need to introspect.
#
# Default-deny rationale: include-list (explicit-allow) is safer than
# exclude-list. New routes added later don't accidentally land in the
# public schema; whoever adds them has to consciously add their prefix to
# this allow-list. Reduces accidental-exposure failure mode.
#
# Operator note: the FULL schema is still available to anyone with shell
# access to the running container (FastAPI generates it from registered
# routes; this filter only affects what's served at /openapi.json + the
# /api/v2/openapi.json mirror). Self-hosted operator can still introspect
# their own deployment via container exec or by reading source.
_OPENAPI_PUBLIC_PATH_PREFIXES = (
    # Agent contract + identity
    "/agentnotes",
    "/api/v2/agentnotes",
    "/api/v2/me",
    # Onboard ceremony surface
    "/api/v2/onboard/",
    "/api/v2/projects/{slug}/onboard-state",
    # Project-scoped data surfaces
    "/api/v2/projects/{slug}/tasks",
    "/api/v2/projects/{slug}/milestones",
    "/api/v2/projects/{slug}/phases",
    "/api/v2/projects/{slug}/members",
    "/api/v2/projects/{slug}/mentionables",
    "/api/v2/projects/{slug}/dashboard",
    "/api/v2/projects/{slug}/resume",
    "/api/v2/projects/{slug}/artefacts",
    "/api/v2/projects/{slug}/archive-summary",
    # Task-scoped data surfaces
    "/api/v2/tasks/{task_id}",  # get + update
    "/api/v2/tasks/{task_id}/notes",
    "/api/v2/tasks/{task_id}/audit",
    "/api/v2/tasks/{task_id}/relationships",
    "/api/v2/tasks/{task_id}/related",
    "/api/v2/tasks/{task_id}/blocks",
    # Milestone close/reopen
    "/api/v2/milestones/{milestone_id}",
    # Phase update/delete (project-scoped phases live at /projects/{slug}/phases
    # for list/create; per-phase mutations live at /phases/{id} per contract.py
    # phases.update + phases.delete entries — agents need both).
    "/api/v2/phases/",
    # Triggers (mention placeholder)
    "/api/v2/triggers/",
)


def _filter_openapi_schema(schema: dict) -> dict:
    """Strip non-agent-relevant routes from the public OpenAPI schema.
    Only paths starting with one of _OPENAPI_PUBLIC_PATH_PREFIXES survive.
    Mutates the schema dict's 'paths' key in place + returns it."""
    paths = schema.get("paths", {})
    filtered = {}
    for path, methods in paths.items():
        if any(path.startswith(prefix) for prefix in _OPENAPI_PUBLIC_PATH_PREFIXES):
            filtered[path] = methods
    schema["paths"] = filtered
    return schema


_original_openapi_fn = app.openapi


def _filtered_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    schema = _original_openapi_fn()
    return _filter_openapi_schema(schema)


app.openapi = _filtered_openapi


@app.on_event("startup")
def _register_build_label_jinja_global() -> None:
    """Compute the build label once at startup, register as a Jinja2 global on
    every templates instance in the app. Templates use it as `{{ build_label }}`
    to render under the top-left logo. See app/api/v2/contract.py BUILD_TAG."""
    from sqlalchemy import text
    from app.db.session import SessionLocal
    from app.api.v2.contract import BUILD_TAG

    # is_release_build = True iff BUILD_TAG was explicitly set in contract.py
    # (i.e. this is a downstream release branch like 0.7.0-RC, not master).
    # Templates use it to apply the orange-pulse style on the build label —
    # a quiet visual reminder that you're on a pre-release, not a frozen RC.
    is_release_build = bool(BUILD_TAG)
    if is_release_build:
        label = BUILD_TAG
    else:
        db = SessionLocal()
        try:
            head = db.execute(text("SELECT version_num FROM alembic_version LIMIT 1")).scalar()
            label = f"Pre-RC · alembic {head[:12]}" if head else "Pre-RC"
        except Exception:
            label = "Pre-RC"
        finally:
            db.close()

    # Register on every Jinja2Templates instance the app uses. Each module
    # creates its own; we reach in and set the global on each.
    from app.api.v2 import ui as _ui_mod, admin as _admin_mod
    from app.api.v2 import admin_portal as _ap_mod, admin_experimental as _ae_mod
    for mod in (_ui_mod, _admin_mod, _ap_mod, _ae_mod):
        if hasattr(mod, "templates"):
            mod.templates.env.globals["build_label"] = label
            mod.templates.env.globals["is_release_build"] = is_release_build


@app.on_event("startup")
def _vf310_cleanup_legacy_sa_board_sessions() -> None:
    """VF-310: revoke any SA-held board sessions left over from before the
    login block. One-shot, idempotent. See identity-roles.md §5.4."""
    from sqlalchemy import text
    from app.db.session import SessionLocal
    db = SessionLocal()
    try:
        result = db.execute(text(
            "DELETE FROM sessions "
            "WHERE session_type = 'user' "
            "AND user_id IN (SELECT id FROM users WHERE role = 'super_admin')"
        ))
        deleted = result.rowcount or 0
        db.commit()
        if deleted:
            print(f"[VF-310] Revoked {deleted} legacy SA board session(s) at startup.")
    except Exception as e:
        db.rollback()
        print(f"[VF-310] Startup cleanup failed (non-fatal): {e}")
    finally:
        db.close()


@app.get("/")
async def root():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/ui/", status_code=302)
