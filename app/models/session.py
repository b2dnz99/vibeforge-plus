import uuid
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import String, DateTime, Text, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class UserSession(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False, index=True
    )
    session_type: Mapped[str] = mapped_column(String(10), nullable=False, default="user")
    # session_type: 'user' (normal) or 'sa' (super admin elevation)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_activity_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    ip_address: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    elevated_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # VF-264: when set and in the future, this vf_session carries SU admin-context
    # elevation (sudo-style). See identity-roles.md / su-elevation-tier.md.

    # VF-341: soft-delete revoke trio. See SESSION-LIFECYCLE-PROPOSAL §4.7 +
    # §6.2. Auth check stays single-WHERE (expires_at > now AND revoked_at
    # IS NULL); state is pure read-time derivation per §4.1.
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    revoke_reason: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
