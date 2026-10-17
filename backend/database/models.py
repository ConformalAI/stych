"""
SQLAlchemy database models.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from .connection import Base


class User(Base):
    """User account model.

    supertokens_id is the primary identity bridge to the SuperTokens Core.
    google_id is kept nullable for legacy rows created before the ST migration.
    """

    __tablename__ = "users"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    email = Column(String, unique=True, index=True, nullable=False)
    # SuperTokens user ID — set on first sign-in via ST (email/password or social)
    supertokens_id = Column(String, unique=True, index=True, nullable=True)
    # Legacy Google OAuth ID — nullable; kept so old rows are not broken
    google_id = Column(String, unique=True, index=True, nullable=True)
    # Public handle/username shown in profile/settings.
    handle = Column(String, unique=True, index=True, nullable=True)
    name = Column(String, nullable=True)
    profile_picture = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_login = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<User(id={self.id}, email={self.email})>"

    def to_dict(self):
        """Convert to dictionary for API responses."""
        return {
            "id": self.id,
            "email": self.email,
            "handle": self.handle,
            "name": self.name,
            "profile_picture": self.profile_picture,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class LLMUsageEvent(Base):
    """Per-call LLM usage and credit consumption record."""

    __tablename__ = "llm_usage_events"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, index=True, nullable=False)
    session_id = Column(String, index=True, nullable=True)
    provider = Column(String, index=True, nullable=False)
    model = Column(String, index=True, nullable=False)
    agent_name = Column(String, nullable=True)
    prompt_tokens = Column(Integer, nullable=False, default=0)
    completion_tokens = Column(Integer, nullable=False, default=0)
    total_tokens = Column(Integer, nullable=False, default=0)
    estimated_cost_usd = Column(Float, nullable=False, default=0.0)
    credits_consumed = Column(Float, nullable=False, default=0.0)
    usage_metadata = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class AppUser(Base):
    """App-domain user profile and quota owner (linked to SuperTokens user id)."""

    __tablename__ = "app_users"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    supertokens_user_id = Column(String, unique=True, index=True, nullable=False)
    email = Column(String, unique=True, index=True, nullable=False)
    handle = Column(String, unique=True, index=True, nullable=True)
    name = Column(String, nullable=True)
    profile_picture = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    preferred_model = Column(String(255), nullable=True)
    long_term_memory_enabled = Column(Boolean, nullable=False, default=True, server_default="true")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_login_at = Column(DateTime, nullable=True)


class UserQuota(Base):
    """Token/credit limits and running usage snapshot per app user."""

    __tablename__ = "user_quotas"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    app_user_id = Column(
        String, ForeignKey("app_users.id"), nullable=False, unique=True, index=True
    )
    plan = Column(String, nullable=False, default="free")
    token_limit_daily = Column(Integer, nullable=True)
    token_limit_monthly = Column(Integer, nullable=True)
    credit_limit_monthly = Column(Float, nullable=True)
    tokens_used_monthly = Column(Integer, nullable=False, default=0)
    credits_used_monthly = Column(Float, nullable=False, default=0.0)
    period_start = Column(DateTime, nullable=True)
    period_end = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class SessionHistory(Base):
    """Per-user conversation session metadata."""

    __tablename__ = "session_histories"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    app_user_id = Column(String, ForeignKey("app_users.id"), nullable=False, index=True)
    title = Column(String, nullable=True)
    started_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    last_activity_at = Column(DateTime, nullable=True, index=True)
    source_kind = Column(String, nullable=True)  # csv, postgres, mixed, etc.
    status = Column(String, nullable=False, default="active", index=True)
    total_tokens = Column(Integer, nullable=False, default=0)
    total_credits = Column(Float, nullable=False, default=0.0)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_message_at = Column(DateTime, nullable=True, index=True)
    is_anonymous = Column(Boolean, nullable=False, default=False, server_default="false")


class SessionMessage(Base):
    """Message records for each session history row."""

    __tablename__ = "session_messages"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    session_history_id = Column(
        String, ForeignKey("session_histories.id", ondelete="CASCADE"), nullable=False, index=True
    )
    app_user_id = Column(String, ForeignKey("app_users.id"), nullable=False, index=True)
    role = Column(String, nullable=False)  # user | assistant | system | tool
    content = Column(Text, nullable=False)
    content_type = Column(String, nullable=False, default="text")
    metadata_json = Column(Text, nullable=True)
    token_count = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class UploadedFile(Base):
    """
    Metadata record for every file uploaded by a user.

    The actual file bytes live in the configured storage backend (local disk or
    S3-compatible object store such as MinIO).  storage_backend tells callers
    which backend holds the file; storage_key is the opaque handle needed to
    retrieve or delete it (local path for 'local', object key for 's3').
    """

    __tablename__ = "uploaded_files"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    app_user_id = Column(String, ForeignKey("app_users.id"), nullable=False, index=True)
    session_id = Column(String, nullable=True, index=True)
    filename = Column(String, nullable=False)
    original_filename = Column(String, nullable=True)
    storage_backend = Column(String, nullable=False, default="local")  # local | s3
    storage_key = Column(String, nullable=False)  # local path or S3 object key
    size_bytes = Column(Integer, nullable=False, default=0)
    content_type = Column(String, nullable=True)
    is_deleted = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    deleted_at = Column(DateTime, nullable=True)


class UserIntegration(Base):
    """Per-user MCP server credentials (encrypted at rest)."""

    __tablename__ = "user_integrations"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    app_user_id = Column(String, ForeignKey("app_users.id"), nullable=False, index=True)
    server_id = Column(String(64), nullable=False)
    """Registry key, e.g. 'google_sheets'."""
    credentials_enc = Column(Text, nullable=True)
    """Fernet-encrypted JSON blob of credential key/value pairs."""
    is_active = Column(Boolean, nullable=False, default=True, server_default="true")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=True)

    __table_args__ = (UniqueConstraint("app_user_id", "server_id"),)


class UserConnection(Base):
    """Per-user saved datasource connection (encrypted credentials)."""

    __tablename__ = "user_connections"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    app_user_id = Column(String, ForeignKey("app_users.id"), nullable=False, index=True)
    connector_id = Column(String(32), nullable=False, index=True)
    name = Column(String, nullable=False)
    credentials_enc = Column(Text, nullable=False)
    config_json = Column(Text, nullable=True)
    description = Column(Text, nullable=True)
    # JSON array of user-defined tags, e.g. ["prod", "finance"]. Vendor/cloud
    # facets are derived from connector_id at read time, not stored here.
    tags_json = Column(Text, nullable=True)
    last_tested_at = Column(DateTime, nullable=True)
    # Outcome of the most recent connection test: "ok" | "error" | None (never tested).
    last_test_status = Column(String(16), nullable=True)
    last_test_error = Column(Text, nullable=True)
    # Last time a table from this connection was attached to a session — distinct
    # from last_tested_at, which only tracks credential checks.
    last_used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    tables = relationship(
        "UserConnectionTable",
        back_populates="connection",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class UserConnectionTable(Base):
    """Pinned table/collection bookmark on a saved connection."""

    __tablename__ = "user_connection_tables"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    connection_id = Column(
        String,
        ForeignKey("user_connections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    schema_name = Column(String, nullable=True)
    table_name = Column(String, nullable=True)
    custom_query = Column(Text, nullable=True)
    display_name = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    connection = relationship("UserConnection", back_populates="tables")


class OAuthPendingState(Base):
    """Short-lived CSRF state for MCP integration OAuth (consumed on callback)."""

    __tablename__ = "oauth_pending_states"

    state = Column(String(64), primary_key=True)
    app_user_id = Column(String, ForeignKey("app_users.id"), nullable=False, index=True)
    server_id = Column(String(64), nullable=False)
    expires_at = Column(DateTime, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class Visualization(Base):
    """A saved visualization: current VizSpec JSON bound to a session's dataset.

    The spec is the single source of truth (see backend/viz/) — no chart data
    is ever persisted; data is re-materialized from the spec on demand.
    """

    __tablename__ = "visualizations"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    app_user_id = Column(String, ForeignKey("app_users.id"), nullable=False, index=True)
    session_history_id = Column(
        String, ForeignKey("session_histories.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Stable DataContext.file_id. Kept alongside spec_json for indexed/listing
    # access and compatibility with rows created before DataRef was canonical.
    dataset_ref = Column(String, nullable=True)
    title = Column(String, nullable=True)
    spec_json = Column(Text, nullable=False)
    version = Column(Integer, nullable=False, default=1)
    is_deleted = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class VisualizationVersion(Base):
    """Full-spec version history for a visualization (undo/audit)."""

    __tablename__ = "visualization_versions"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    visualization_id = Column(
        String, ForeignKey("visualizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version_n = Column(Integer, nullable=False)
    spec_json = Column(Text, nullable=False)
    author = Column(String, nullable=False, default="user")  # user | agent
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    __table_args__ = (UniqueConstraint("visualization_id", "version_n"),)


class UsageLedger(Base):
    """Canonical billing/consumption ledger linked to app users and sessions."""

    __tablename__ = "usage_ledger"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    app_user_id = Column(String, ForeignKey("app_users.id"), nullable=False, index=True)
    session_history_id = Column(
        String, ForeignKey("session_histories.id", ondelete="SET NULL"), nullable=True, index=True
    )
    llm_usage_event_id = Column(String, nullable=True, index=True)
    provider = Column(String, nullable=False, index=True)
    model = Column(String, nullable=False, index=True)
    prompt_tokens = Column(Integer, nullable=False, default=0)
    completion_tokens = Column(Integer, nullable=False, default=0)
    total_tokens = Column(Integer, nullable=False, default=0)
    estimated_cost_usd = Column(Float, nullable=False, default=0.0)
    credits_consumed = Column(Float, nullable=False, default=0.0)
    metadata_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class SavedFilter(Base):
    """A saved preview filter: current FilterSpec JSON bound to one preview
    source (``dataset_ref``) in one session.

    The spec (see backend/filters/spec.py) is the single source of truth — no
    filtered data or annotations are persisted; everything re-materializes from
    the spec on demand.
    """

    __tablename__ = "filters"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    app_user_id = Column(String, ForeignKey("app_users.id"), nullable=False, index=True)
    session_history_id = Column(
        String, ForeignKey("session_histories.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Preview-source id: workspace path | dataset file_id | "<file_id>::base|copy:<n>"
    dataset_ref = Column(String, nullable=False, index=True)
    name = Column(String, nullable=False)
    spec_json = Column(Text, nullable=False)
    version = Column(Integer, nullable=False, default=1)
    is_deleted = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class SavedFilterVersion(Base):
    """Full-spec version history for a saved filter (undo/audit)."""

    __tablename__ = "filter_versions"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    filter_id = Column(
        String, ForeignKey("filters.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version_n = Column(Integer, nullable=False)
    spec_json = Column(Text, nullable=False)
    author = Column(String, nullable=False, default="agent")  # user | agent
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    __table_args__ = (UniqueConstraint("filter_id", "version_n"),)


class Ontology(Base):
    """One user's semantic model, spanning every dataset they work with.

    Deliberately user-scoped rather than dataset- or session-scoped (as
    visualizations and filters are). Relationships between entities are the
    point of an ontology, so a per-dataset row could not express them; and a
    definition of "revenue" that a user agreed on is a property of the user's
    domain, not of the chat session they happened to write it in — a
    session-scoped ontology made them redefine the same metric in every new
    session, which defeats the purpose of certifying one.

    Because it outlives any one session, an ontology may reference datasets not
    loaded in the session currently reading it. That is expected: the routes
    report those as ``missing_datasets`` and queries against them fail loudly
    rather than returning a number from the wrong data.
    """

    __tablename__ = "ontologies"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    app_user_id = Column(
        String, ForeignKey("app_users.id"), nullable=False, index=True, unique=True
    )
    title = Column(String, nullable=True)
    spec_json = Column(Text, nullable=False)
    version = Column(Integer, nullable=False, default=1)
    is_deleted = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class OntologyVersion(Base):
    """Full-spec version history for an ontology (undo/audit).

    ``proposed_json`` lists the definition names an agent added in this version.
    It is what makes the chat proposal card reversible: rejecting a proposal
    restores the parent version, accepting certifies exactly these names.
    """

    __tablename__ = "ontology_versions"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    ontology_id = Column(
        String, ForeignKey("ontologies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version_n = Column(Integer, nullable=False)
    spec_json = Column(Text, nullable=False)
    author = Column(String, nullable=False, default="user")  # user | agent
    # Short description of what changed, e.g. "proposed 3 metrics".
    summary = Column(String, nullable=True)
    proposed_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    __table_args__ = (UniqueConstraint("ontology_id", "version_n"),)
