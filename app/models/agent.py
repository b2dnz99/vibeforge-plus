import uuid
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import String, DateTime, Text, ForeignKey
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    slug: Mapped[str] = mapped_column(String(60), unique=True, nullable=False, index=True)
    # WHY: slug format is {project_slug}-{name_lower}, globally unique
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    # status: active, suspended, revoked

    # GATE: project scope — agent can only access this project
    project_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    # WHY: tracks who provisioned this agent — cascade revoke on creator delete
    created_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)

    # Model info — model_type set by human, model_name self-reported by agent
    model_type: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    model_name: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    # Token — hash only, never store plaintext
    api_token_hash: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, default=None)
    token_prefix: Mapped[Optional[str]] = mapped_column(String(8), nullable=True, default=None)

    # Revocation
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    # Future: token expiry
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Heartbeat: updated on any authenticated agent API call (VF-200)
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # VF-306: cumulative API-call counter (bare-minimum cut). Incremented in
    # the agent-auth helper next to last_seen_at; reset to 0 on token cycle.
    # See 0-MD/proposed/VF-306-AGENT-TELEMETRY-AND-DRIFT-PROPOSAL.md.
    api_call_count: Mapped[int] = mapped_column(nullable=False, default=0)
    api_call_count_since: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    # Context Drift Refresh (VF-266): set when agent reads /agentnotes, checked on every mutation
    last_contract_read_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Nonce: random 4-byte hex, rotates on each /agentnotes read, agent must echo on next mutation
    refresh_nonce: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    # v4: repurposed as pivot counter within a single drift-eval firing.
    # 0 = first attempt, 1-3 = pivots, 4 = escalation trigger.
    # Reset to 0 on contract refresh OR on accepted eval.
    drift_eval_count: Mapped[Optional[int]] = mapped_column(nullable=True)
    # v4 per-cycle pass marker. Non-null = cycle has had a clean eval pass;
    # gate stops firing for rest of cycle. Null = gate still active this cycle.
    # Reset to NULL on contract refresh.
    drift_eval_passed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Drift v4 dedup history: every accepted X-Drift-Response hash, capped at 200.
    # Persists across contract refreshes. Reset only on clear-drift.
    drift_eval_hashes: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    # NOTE: v4.1 refactor — escalation freeze state (was drift_escalated_at here)
    # now lives in the drift_escalations table. "Is agent frozen?" =
    # EXISTS(drift_escalations WHERE agent_id=self.id AND ended_at IS NULL).

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
