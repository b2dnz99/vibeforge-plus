import uuid
from datetime import datetime, timezone
from sqlalchemy import String, DateTime, Enum as SAEnum, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class TokenProjectPermission(Base):
    __tablename__ = "token_project_permissions"
    __table_args__ = (UniqueConstraint("token_id", "project_id", name="uq_token_project"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    token_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("api_tokens.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    permission_level: Mapped[str] = mapped_column(
        SAEnum("read", "write", "admin", name="permission_level_enum"),
        nullable=False,
        default="read",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
