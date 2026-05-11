import uuid
from sqlalchemy import String, Integer, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class UserProjectPin(Base):
    """Per-user project pin. Independent of Project.pinned (legacy global pins).

    pin_order asc = top of pinned list. Absent row = not pinned for this user.
    """
    __tablename__ = "user_project_pins"
    __table_args__ = (UniqueConstraint("user_id", "project_id", name="uq_user_project_pin"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    pin_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
