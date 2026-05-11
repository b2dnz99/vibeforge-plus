import uuid
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import String, DateTime, Text, ForeignKey, Boolean, JSON
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class TaskNote(Base):
    __tablename__ = "task_notes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    author_type: Mapped[str] = mapped_column(String(20), nullable=False, default="human")  # human, agent, system
    author_name: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    body: Mapped[str] = mapped_column(Text, nullable=False)
    is_completion_note: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    # Human-only visibility: when true, the note is stripped from agent-facing GETs.
    # Used by drift v4 audit notes AND general internal-only discussion (security plane,
    # customer-hidden content). Agents cannot set this (enforced at the endpoint layer).
    is_internal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    superseded_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    superseded_by: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, default=None)
    superseded_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True, default=None)
    supersede_history: Mapped[Optional[list]] = mapped_column(JSON, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
