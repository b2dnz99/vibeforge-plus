import uuid
from sqlalchemy import String, Text, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class UserPreferences(Base):
    """Per-user appearance + UI prefs as a single JSON blob.

    One row per user. JSON keeps schema flexible — client owns the shape.
    """
    __tablename__ = "user_preferences"

    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    prefs_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
