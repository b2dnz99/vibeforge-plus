import uuid
from datetime import date, datetime, timezone
from sqlalchemy import String, Date, DateTime, Integer, Float, Text, Boolean, Enum as SAEnum, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    external_number: Mapped[int | None] = mapped_column(Integer, nullable=True)  # v1 integer id
    task_number: Mapped[int | None] = mapped_column(Integer, nullable=True)  # sequential per project
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    short_description: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(
        SAEnum(
            "backlog", "ready", "in_progress", "needs_review", "blocked", "done", "cancelled",
            name="task_status_enum",
        ),
        nullable=False,
        default="backlog",
    )
    priority: Mapped[str] = mapped_column(
        SAEnum("low", "medium", "high", "critical", name="task_priority_enum"),
        nullable=False,
        default="medium",
    )
    owner_label: Mapped[str] = mapped_column(String(50), nullable=False, default="agent")
    parent_task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    milestone_label: Mapped[str | None] = mapped_column(String(50), nullable=True)  # legacy — use milestone_id when available
    milestone_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("milestones.id", ondelete="SET NULL"), nullable=True
    )
    phase_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("phases.id", ondelete="SET NULL"), nullable=True
    )
    task_type: Mapped[str | None] = mapped_column(String(20), nullable=True)  # feature, bug, chore, spike, verification
    assignee_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    estimated_hours: Mapped[float | None] = mapped_column(Float, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    blocked_by_task_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    abandoned_note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # NOTE: v4.1 refactor — the drift flag state (was has_active_drift_flag +
    # drift_escalated_agent_id here) now lives in the drift_escalations table.
    # "Is task flagged?" = EXISTS(drift_escalations WHERE task_id=self.id AND ended_at IS NULL).
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
