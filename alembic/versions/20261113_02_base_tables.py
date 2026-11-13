"""Create missing baseline tables and indexes for databases of any prior revision."""

from alembic import op
import sqlalchemy as sa

revision = "20261113_02"
down_revision = "20261113_01"
branch_labels = None
depends_on = None


def _baseline_schema():
    schema = sa.MetaData()
    sa.Table(
        "books", schema,
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("platform", sa.String(length=64), nullable=False),
        sa.Column("genre", sa.String(length=128), nullable=False),
        sa.Column("logline", sa.Text()),
        sa.Column("book_outline", sa.JSON()),
        sa.Column("style_rules", sa.JSON()),
        sa.Column("status", sa.String(length=64), nullable=False),
    )
    sa.Table(
        "scheduler_runs", schema,
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("cycle_number", sa.Integer(), nullable=False),
        sa.Column("processed_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("outcomes", sa.JSON(), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("outcome_counts", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    sa.Table(
        "trend_snapshots", schema,
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("capture_id", sa.String(length=64), nullable=False, index=True),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("source_url", sa.String(length=512), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("items", sa.JSON(), nullable=False),
        sa.Column("error_message", sa.Text()),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    sa.Table(
        "chapters", schema,
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("book_id", sa.String(length=64), sa.ForeignKey("books.id"), nullable=False, index=True),
        sa.Column("chapter_number", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("goal", sa.JSON()),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("current_version_id", sa.String(length=64)),
        sa.Column("approved_version_id", sa.String(length=64)),
        sa.Column("claim_token", sa.String(length=64)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("error_message", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Index("ix_chapters_status_next_attempt_at", "status", "next_attempt_at"),
    )
    sa.Table(
        "characters", schema,
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("book_id", sa.String(length=64), sa.ForeignKey("books.id"), nullable=False, index=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=128)),
        sa.Column("age", sa.Integer()),
        sa.Column("profile", sa.JSON()),
        sa.Column("last_updated_chapter_number", sa.Integer()),
    )
    sa.Table(
        "memory_embeddings", schema,
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("book_id", sa.String(length=64), sa.ForeignKey("books.id"), nullable=False, index=True),
        sa.Column("source_type", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("source_chapter_number", sa.Integer()),
        sa.Column("source_text", sa.Text(), nullable=False),
        sa.Column("text_hash", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("vector", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("refreshed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    sa.Table(
        "story_arcs", schema,
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("book_id", sa.String(length=64), sa.ForeignKey("books.id"), nullable=False, index=True),
        sa.Column("arc_type", sa.String(length=64), nullable=False),
        sa.Column("chapter_start", sa.Integer(), nullable=False),
        sa.Column("chapter_end", sa.Integer()),
        sa.Column("stage_goal", sa.Text()),
        sa.Column("outline", sa.JSON()),
    )
    sa.Table(
        "chapter_versions", schema,
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("chapter_id", sa.String(length=64), sa.ForeignKey("chapters.id"), nullable=False, index=True),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("content", sa.Text()),
        sa.Column("summary", sa.Text()),
        sa.Column("review_score", sa.Float()),
        sa.Column("generation_metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    sa.Table(
        "character_snapshots", schema,
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("character_id", sa.String(length=64), sa.ForeignKey("characters.id"), nullable=False, index=True),
        sa.Column("chapter_id", sa.String(length=64), sa.ForeignKey("chapters.id"), nullable=False, index=True),
        sa.Column("state", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    sa.Table(
        "continuity_events", schema,
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("chapter_id", sa.String(length=64), sa.ForeignKey("chapters.id"), nullable=False, index=True),
        sa.Column("chapter_version_id", sa.String(length=64)),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("delta", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    sa.Table(
        "publish_jobs", schema,
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("chapter_id", sa.String(length=64), sa.ForeignKey("chapters.id"), nullable=False, index=True),
        sa.Column("chapter_version_id", sa.String(length=64)),
        sa.Column("chapter_title", sa.String(length=255)),
        sa.Column("claim_token", sa.String(length=64)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("platform", sa.String(length=64), nullable=False),
        sa.Column("mode", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("platform_draft_id", sa.String(length=255)),
        sa.Column("error_message", sa.Text()),
        sa.Column("screenshot_path", sa.String(length=255)),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Index("ix_publish_jobs_status_next_attempt_at", "status", "next_attempt_at"),
    )
    return schema


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())
    for table in _baseline_schema().sorted_tables:
        if table.name not in existing_tables:
            table.create(bind)
            continue
        if not table.indexes:
            continue
        columns = {column["name"] for column in inspector.get_columns(table.name)}
        indexes = {index["name"] for index in inspector.get_indexes(table.name)}
        for index in sorted(table.indexes, key=lambda item: item.name):
            if index.name not in indexes and columns.issuperset(index.columns.keys()):
                index.create(bind)


def downgrade() -> None:
    # Existing installations already owned these tables; downgrading must retain their data.
    pass
