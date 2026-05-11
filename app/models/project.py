import uuid
from datetime import datetime, timezone
from sqlalchemy import String, DateTime, Boolean, Integer, Text, Enum as SAEnum, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(
        SAEnum("active", "completed", "archived", name="project_status_enum"),
        nullable=False,
        default="active",
    )
    archived_reason: Mapped[str | None] = mapped_column(
        SAEnum("completed", "abandoned", name="project_archived_reason_enum"),
        nullable=True,
    )
    root_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    docs_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    project_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    resume_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    project_number: Mapped[int | None] = mapped_column(nullable=True)
    prefix: Mapped[str | None] = mapped_column(String(4), nullable=True)
    agentic_dev: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_by_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lifecycle_log: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    pin_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    card_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # VF-353 customer-onboard mechanism (CUSTOMER-ONBOARD-PROPOSAL §3.3): flexible
    # map of per-step hashes for the first-onboard workflow. Schema lives at the
    # application layer; column stays JSONB so per-step additions don't need
    # migrations. Default {} on backfill + on new rows.
    onboard_state: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
