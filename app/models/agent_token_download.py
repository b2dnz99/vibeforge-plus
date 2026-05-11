"""VF-303: short-lived plaintext store for one-time agent-token file downloads.

The token stays in plaintext for at most 5 minutes, keyed by a single-use
nonce, so the download can come from a known same-origin URL and sidestep
browser SmartScreen / Safe-Browsing reputation scans on blob: URLs.

Rows consume themselves on first GET via `consumed_at`. Expiry is enforced
in code (cheap) and can be pruned by a periodic sweep.
"""
import uuid
from datetime import datetime
from typing import Optional
from sqlalchemy import String, DateTime, Text, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class AgentTokenDownload(Base):
    __tablename__ = "agent_token_downloads"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    nonce: Mapped[str] = mapped_column(String(48), nullable=False, unique=True, index=True)
    token_plaintext: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    consumed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
