from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TenantRecord(Base):
    __tablename__ = "tenants"

    tenant_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(30), default="active", index=True)


class TenantContextRecord(Base):
    __tablename__ = "tenant_contexts"

    tenant_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    system_prompt: Mapped[str] = mapped_column(Text, default="")


class UserRecord(Base):
    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    username: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MembershipRecord(Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("user_id", "tenant_id", name="uq_membership_user_tenant"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(80), index=True)
    tenant_id: Mapped[str] = mapped_column(String(80), index=True)
    role: Mapped[str] = mapped_column(String(30), default="member")


class ProcessTemplateRecord(Base):
    __tablename__ = "process_templates"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_process_template_tenant_name"),)

    template_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(80), index=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(30), default="draft", index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    definition_json: Mapped[str] = mapped_column(Text, default="{}")
    created_by_user_id: Mapped[str] = mapped_column(String(80), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class OrchestrationRecord(Base):
    __tablename__ = "orchestrations"

    orchestration_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(80), default="default", index=True)
    owner_user_id: Mapped[str] = mapped_column(String(80), default="local-user", index=True)
    template_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    template_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    template_snapshot_json: Mapped[str | None] = mapped_column(Text, nullable=True)
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


class ExecutionAccessRecord(Base):
    __tablename__ = "execution_access"
    __table_args__ = (UniqueConstraint("orchestration_id", "user_id", name="uq_execution_access_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    orchestration_id: Mapped[str] = mapped_column(String(80), index=True)
    user_id: Mapped[str] = mapped_column(String(80), index=True)
    access_level: Mapped[str] = mapped_column(String(30), default="viewer")


class AuditRecord(Base):
    __tablename__ = "audit_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(80), default="default", index=True)
    orchestration_id: Mapped[str] = mapped_column(String(80), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    payload_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UserSessionRecord(Base):
    __tablename__ = "user_shared_sessions"
    __table_args__ = (UniqueConstraint("tenant_id", "owner_user_id", "session_id", name="uq_user_shared_session"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(80), index=True)
    owner_user_id: Mapped[str] = mapped_column(String(80), index=True)
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


class CatalogWorkerRecord(Base):
    __tablename__ = "catalog_workers"

    worker_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text)
    runtime_name: Mapped[str] = mapped_column(String(80))
    enabled: Mapped[bool] = mapped_column(default=True)
    config_json: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class CatalogSkillRecord(Base):
    __tablename__ = "catalog_skills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    source: Mapped[str] = mapped_column(String(30), default="inline")
    content: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class CatalogMcpServerRecord(Base):
    __tablename__ = "catalog_mcp_servers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    transport: Mapped[str] = mapped_column(String(40))
    url: Mapped[str] = mapped_column(Text, default="")
    command: Mapped[str] = mapped_column(Text, default="")
    args_json: Mapped[str] = mapped_column(Text, default="[]")
    allowed_tools_json: Mapped[str] = mapped_column(Text, default="[]")
    blocked_tools_json: Mapped[str] = mapped_column(Text, default="[]")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
