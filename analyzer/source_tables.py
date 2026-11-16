"""Local source metadata and durable time-window checkpoints; separate from analysis results."""

from sqlalchemy import JSON, BigInteger, Boolean, Float, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from analyzer.store import Base


class SourceProject(Base):
    __tablename__ = "sales_source_projects"
    source_scope: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_key: Mapped[str] = mapped_column(Text, primary_key=True)
    channel: Mapped[str] = mapped_column(Text, default="")
    in_scope: Mapped[bool] = mapped_column(Boolean, default=True)
    revision: Mapped[str] = mapped_column(String(64))
    checked_at: Mapped[float] = mapped_column(Float, default=0, server_default="0")
    pending_analysis: Mapped[bool] = mapped_column(Boolean, default=False)
    batch_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    __table_args__ = (
        Index("ix_sales_source_pending", "source_scope", "pending_analysis", "project_key"),
    )


class SourceNotice(Base):
    __tablename__ = "sales_source_notices"
    source_scope: Mapped[str] = mapped_column(String(64), primary_key=True)
    notice_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_key_name: Mapped[str] = mapped_column(Text, index=True)
    title: Mapped[str] = mapped_column(Text)
    channel: Mapped[str] = mapped_column(Text)
    revoke_status: Mapped[str] = mapped_column(Text)
    content_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    buyer: Mapped[str | None] = mapped_column(Text, nullable=True)
    publish_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    round_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    lot_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    related_notice_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class SourceAttachment(Base):
    __tablename__ = "sales_source_attachments"
    source_scope: Mapped[str] = mapped_column(String(64), primary_key=True)
    att_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    notice_id: Mapped[int] = mapped_column(BigInteger, index=True)
    filename: Mapped[str] = mapped_column(Text)
    text_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    local_path: Mapped[str | None] = mapped_column(Text, nullable=True)


class SyncBatch(Base):
    __tablename__ = "sales_sync_batches"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_scope: Mapped[str] = mapped_column(String(64), index=True)
    signature: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(20), index=True)
    window_start: Mapped[str] = mapped_column(String(40))
    window_end: Mapped[str] = mapped_column(String(40))
    read_start: Mapped[str] = mapped_column(String(40))
    counts: Mapped[dict] = mapped_column(JSON, default=dict)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(default=0)
    started_at: Mapped[float] = mapped_column(Float)
    completed_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    __table_args__ = (Index("ix_sales_sync_batches_started", "started_at"),)


class SyncRecord(Base):
    __tablename__ = "sales_sync_records"
    batch_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), primary_key=True)
    source_id: Mapped[str] = mapped_column(Text, primary_key=True)
    project_key: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSON)
