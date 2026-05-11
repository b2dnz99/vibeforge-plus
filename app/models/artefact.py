import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Integer, Text, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class ProjectArtefact(Base):
    """Per-project versioned markdown artefact (doc contract, plan, handover, etc).

    Per PROJECT-SCAFFOLD-PROPOSAL.md §4. Each PUT creates a new version row.
    Last 50 retained per (project_id, name), oldest auto-pruned.
    64KB body cap enforced at the API layer. Agent read via GET,
    human write via PUT (v1: PUT not yet exposed, read-only for agents).
    """
    __tablename__ = "project_artefacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(50), nullable=False)  # 'contract', 'plan', 'handover'
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    byte_count: Mapped[int] = mapped_column(Integer, nullable=False)
    actor_type: Mapped[str] = mapped_column(String(10), nullable=False)  # 'human' or 'agent'
    actor_name: Mapped[str] = mapped_column(String(100), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)  # sha256 of body
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
