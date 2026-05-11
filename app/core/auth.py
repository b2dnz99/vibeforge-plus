"""Auth helpers — mixed active + DEPRECATED (VF-363).

ACTIVE PATH (every authenticated mutation today):
  Bearer token -> Agent.api_token_hash lookup OR session cookie -> User.
  Resolved by `_resolve_actor` in app/api/v2/projects.py.

DEPRECATED (VF-363): `get_current_token` + `require_permission` below are the
legacy generic-API-token auth machinery. Defined, never called by current
endpoints. The /api/v2/tokens router issues + lists + revokes ApiToken rows,
but those tokens validate against nothing because get_current_token is
unwired. Tables on DEV: 0 rows.

Do NOT wire new endpoints to get_current_token / require_permission unless
intentionally reviving this layer with explicit design. The risk this notice
guards against: future engineer (human or agent) reads the codebase, sees
the helpers, wires a new endpoint, silently gates against zero rows.
"""
import hashlib
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.models.token import ApiToken
from app.models.user import User
from app.models.permission import TokenProjectPermission
from app.models.project import Project

JWT_ALGORITHM = "HS256"

bearer_scheme = HTTPBearer(auto_error=False)


# DEPRECATED (VF-363): legacy generic API token validator. NOT the active
# editor-agent auth path. The active path is _resolve_actor in
# app/api/v2/projects.py (Bearer token -> Agent.api_token_hash). Do not wire
# new endpoints to this function; it gates against zero rows on DEV/PROD.
def get_current_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> ApiToken:
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    raw_token = credentials.credentials
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

    token = db.query(ApiToken).filter(ApiToken.token_hash == token_hash).first()

    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    if token.status != "active":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token revoked")

    token.last_used_at = datetime.now(timezone.utc)
    db.commit()

    return token


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    """Validates a JWT and returns the User. Used for human-facing endpoints."""
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        payload = jwt.decode(credentials.credentials, settings.SECRET_KEY, algorithms=[JWT_ALGORITHM])
        user_id: str = payload.get("sub")
        if not user_id:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    if user.status != "active":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account suspended")
    return user


# Permission levels ranked for >= comparisons
_LEVEL_RANK = {"read": 1, "write": 2, "admin": 3}


# DEPRECATED (VF-363): legacy permission gate built on TokenProjectPermission
# (also legacy, 0 rows on DEV). NOT the active permission path. Active path:
# Agent.project_id is the per-agent project scope; cross-project tokens fail
# in _resolve_actor with 403. For human session-based permissions, see the
# admin/portal cookie checks in app/api/v2/admin_portal.py. Do not add new
# endpoints behind this dependency unless intentionally reviving the layer.
def require_permission(required: str):
    """
    DEPRECATED — see module docstring + comment above. Returns a FastAPI
    dependency that checks the calling token has at least `required`
    permission on the project identified by `project_slug` path param.

    Usage (legacy, do not adopt for new endpoints):
        @router.get("/{project_slug}/tasks",
                    dependencies=[Depends(require_permission("read"))])
    """
    def _check(
        project_slug: str,
        token: ApiToken = Depends(get_current_token),
        db: Session = Depends(get_db),
    ):
        project = db.query(Project).filter(Project.slug == project_slug).first()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")

        perm = (
            db.query(TokenProjectPermission)
            .filter(
                TokenProjectPermission.token_id == token.id,
                TokenProjectPermission.project_id == project.id,
            )
            .first()
        )

        if not perm:
            raise HTTPException(status_code=403, detail="No access to this project")

        if _LEVEL_RANK.get(perm.permission_level, 0) < _LEVEL_RANK.get(required, 99):
            raise HTTPException(
                status_code=403,
                detail=f"Requires {required} permission, token has {perm.permission_level}",
            )

        return perm

    return _check
