import uuid
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import String, DateTime, Enum as SAEnum, Text, Boolean
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    username: Mapped[Optional[str]] = mapped_column(String(10), unique=True, nullable=True, index=True)
    # username: login credential, max 10 chars, lowercase, no spaces
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="user", server_default="user")
    # role values: 'super_admin' (SA — admin portal only), 'super_user' (SU — day-to-day admin + board),
    # 'user' (standard board participant), 'viewer' (read-only). See identity-roles.md §2.
    display_role: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, default=None)
    # vanity label: 'CTO', 'Tech Lead', etc. Display only.
    title: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, default=None)
    # formal title: 'Chief Technology Officer'. Optional.
    nickname: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, default=None)
    # friendly short name: 'Pasha'. User can set themselves.
    status: Mapped[str] = mapped_column(
        SAEnum("active", "suspended", "deleted", name="user_status_enum", create_constraint=False),
        nullable=False,
        default="active",
    )
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    # WHY: Set true on admin password reset. Login redirects to change-password until cleared.
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    deleted_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    ui_prefs: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
