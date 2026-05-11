import uuid
from datetime import date, datetime, timezone
from sqlalchemy import String, Date, DateTime, Integer, Text, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class Milestone(Base):
    __tablename__ = "milestones"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    label: Mapped[str] = mapped_column(String(50), nullable=False)  # e.g. "Milestone B"
    name: Mapped[str] = mapped_column(String(200), nullable=False)  # e.g. "Auth & Security Foundation"
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    target_date: Mapped[date | None] = mapped_column(Date, nullable=True)  # Gantt diamond marker
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")  # active, complete, deferred
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
