"""Drift gate v4.1 — storage models.

See 0-MD/proposed/SYNC-ARCH-EXPERIMENT.md §2 for design.

- DriftEscalation: active + historical escalations. ended_at IS NULL = agent currently frozen.
- DriftEvalAttempt: one row per drift-eval prompt/response. Powers analytics + drilldown timeline.
"""
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import String, DateTime, Text, Integer, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DriftEscalation(Base):
    __tablename__ = "drift_escalations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    task_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    # NULL = active (agent still frozen). Non-null = cleared (human clicked Clear, or system-wide reset).
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    cleared_by: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    cleared_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class DriftEvalAttempt(Base):
    __tablename__ = "drift_eval_attempts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # NULL when an accepted attempt (didn't lead to escalation). Non-null for the attempts
    # that preceded/caused an escalation — lets us assemble the timeline drilldown.
    escalation_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("drift_escalations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    attempted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    question_idx: Mapped[int] = mapped_column(Integer, nullable=False)
    # SHA-256 hex of the response, or NULL if this is a 'prompt' attempt (no response yet).
    response_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # prompt / accepted / dedup / too_short / too_long / escalated
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
