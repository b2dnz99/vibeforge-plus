"""Legacy generic API token storage.

DEPRECATED layer (VF-363): `ApiToken` is the historical "generic API token"
machinery. It is NOT the active editor-agent auth path.

Active path (every authenticated mutation today):
  Bearer token -> Agent.api_token_hash lookup OR session cookie -> User
  Resolved by `_resolve_actor` in app/api/v2/projects.py.

ApiToken / get_current_token / require_permission (in app/core/auth.py) are
defined but never called by current endpoints. The /api/v2/tokens router
issues + lists + revokes ApiToken rows, but those tokens validate against
nothing because get_current_token is unwired. Tables on DEV: 0 rows.

Do NOT build new agent behaviour on ApiToken unless intentionally reviving
this layer with explicit design + cross-reference. Sediment is fine pre-RC;
an accidental new caller is the failure mode this notice prevents.
"""
import uuid
from datetime import datetime, timezone
from sqlalchemy import String, DateTime, Enum as SAEnum, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


# DEPRECATED (VF-363): legacy generic API token model. Not the active editor-agent
# auth path. The active path is Agent.api_token_hash lookup via _resolve_actor in
# app/api/v2/projects.py. Do not build new agent behaviour on this model.
class ApiToken(Base):
    __tablename__ = "api_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    token_prefix: Mapped[str] = mapped_column(String(8), nullable=False)  # first 8 chars for display
    kind: Mapped[str] = mapped_column(
        SAEnum("personal", "agent", name="token_kind_enum"),
        nullable=False,
        default="personal",
    )
    status: Mapped[str] = mapped_column(
        SAEnum("active", "revoked", name="token_status_enum"),
        nullable=False,
        default="active",
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
