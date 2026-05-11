import hashlib
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.db.session import get_db
from app.models.token import ApiToken
from app.models.user import User

router = APIRouter()

TOKEN_BYTES = 32  # 256-bit raw token


# --- Schemas ---

class CreateTokenRequest(BaseModel):
    name: str
    kind: str = "personal"  # personal | agent


class TokenCreatedResponse(BaseModel):
    id: str
    name: str
    kind: str
    prefix: str
    raw_token: str  # shown once only
    created_at: datetime


class TokenListItem(BaseModel):
    id: str
    name: str
    kind: str
    status: str
    prefix: str
    last_used_at: datetime | None
    created_at: datetime


# --- Helpers ---

def _generate_token() -> tuple[str, str, str]:
    """Returns (raw_token, prefix, sha256_hash)."""
    raw = secrets.token_hex(TOKEN_BYTES)
    prefix = raw[:8]
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    return raw, prefix, token_hash


# --- Endpoints ---

@router.post("/tokens", response_model=TokenCreatedResponse, status_code=status.HTTP_201_CREATED)
def create_token(
    body: CreateTokenRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if body.kind not in ("personal", "agent"):
        raise HTTPException(status_code=422, detail="kind must be personal or agent")

    user_id = current_user.id
    raw, prefix, token_hash = _generate_token()

    token = ApiToken(
        user_id=user_id,
        name=body.name,
        token_hash=token_hash,
        token_prefix=prefix,
        kind=body.kind,
    )
    db.add(token)
    db.commit()
    db.refresh(token)

    return TokenCreatedResponse(
        id=token.id,
        name=token.name,
        kind=token.kind,
        prefix=token.token_prefix,
        raw_token=raw,
        created_at=token.created_at,
    )


@router.get("/tokens", response_model=list[TokenListItem])
def list_tokens(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    user_id = current_user.id
    tokens = (
        db.query(ApiToken)
        .filter(ApiToken.user_id == user_id, ApiToken.status == "active")
        .order_by(ApiToken.created_at.desc())
        .all()
    )
    return [
        TokenListItem(
            id=t.id,
            name=t.name,
            kind=t.kind,
            status=t.status,
            prefix=t.token_prefix,
            last_used_at=t.last_used_at,
            created_at=t.created_at,
        )
        for t in tokens
    ]


@router.delete("/tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_token(
    token_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    user_id = current_user.id
    token = db.query(ApiToken).filter(ApiToken.id == token_id, ApiToken.user_id == user_id).first()
    if not token:
        raise HTTPException(status_code=404, detail="Token not found")
    token.status = "revoked"
    db.commit()
