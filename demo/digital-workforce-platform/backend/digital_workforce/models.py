from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class WorkerRecord(Base):
    __tablename__ = "workers"

    worker_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(80), primary_key=True, default="default")
    display_name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text)
    runtime_name: Mapped[str] = mapped_column(String(80))
    enabled: Mapped[bool] = mapped_column(default=True)
    config_json: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class OrchestrationRecord(Base):
    __tablename__ = "orchestrations"

    orchestration_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(80), default="default", index=True)
    workflow_id: Mapped[str] = mapped_column(String(120), unique=True)
    session_id: Mapped[str] = mapped_column(String(120), index=True)
    goal: Mapped[str] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(40), default="queued", index=True)
    effective_json: Mapped[str] = mapped_column(Text, default="{}")
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    session_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditRecord(Base):
    __tablename__ = "audit_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(80), default="default", index=True)
    orchestration_id: Mapped[str] = mapped_column(String(80), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    payload_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SessionRecord(Base):
    __tablename__ = "shared_sessions"
    __table_args__ = (UniqueConstraint("tenant_id", "session_id", name="uq_shared_session_tenant_session"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(80), index=True)
    session_id: Mapped[str] = mapped_column(String(120), index=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class EventRecord(Base):
    __tablename__ = "orchestration_events"
    __table_args__ = (UniqueConstraint("orchestration_id", "seq", name="uq_orchestration_event_seq"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(80), index=True)
    orchestration_id: Mapped[str] = mapped_column(String(80), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(40))
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ApprovalRecord(Base):
    __tablename__ = "approvals"
    __table_args__ = (UniqueConstraint("orchestration_id", "task_id", name="uq_approval_task"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(80), index=True)
    orchestration_id: Mapped[str] = mapped_column(String(80), index=True)
    task_id: Mapped[str] = mapped_column(String(160))
    worker_id: Mapped[str] = mapped_column(String(80))
    idempotency_key: Mapped[str] = mapped_column(String(80))
    task_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(40), default="pending")
    decision_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SkillRecord(Base):
    __tablename__ = "skills"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_skill_tenant_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(80), index=True)
    name: Mapped[str] = mapped_column(String(120))
    source: Mapped[str] = mapped_column(String(30), default="inline")
    content: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class McpServerRecord(Base):
    __tablename__ = "mcp_servers"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_mcp_tenant_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(80), index=True)
    name: Mapped[str] = mapped_column(String(120))
    transport: Mapped[str] = mapped_column(String(40))
    url: Mapped[str] = mapped_column(Text, default="")
    command: Mapped[str] = mapped_column(Text, default="")
    args_json: Mapped[str] = mapped_column(Text, default="[]")
    allowed_tools_json: Mapped[str] = mapped_column(Text, default="[]")
    blocked_tools_json: Mapped[str] = mapped_column(Text, default="[]")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
