"""Bind approvals/publications to versions and persist task ownership/retries."""

import json

from alembic import op
import sqlalchemy as sa

revision = "20261016_01"
down_revision = "20260908_01"
branch_labels = None
depends_on = None


def _task_columns():
    return [
        sa.Column("claim_token", sa.String(64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
    ]


def _columns():
    return {
        "chapters": [sa.Column("approved_version_id", sa.String(64), nullable=True),
                     sa.Column("error_message", sa.Text(), nullable=True), *_task_columns()],
        "publish_jobs": [sa.Column("chapter_version_id", sa.String(64), nullable=True),
                         sa.Column("chapter_title", sa.String(255), nullable=True), *_task_columns()],
        "continuity_events": [sa.Column("chapter_version_id", sa.String(64), nullable=True)],
    }


def upgrade():
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    for table, columns in _columns().items():
        if table not in tables:
            continue
        existing = {c["name"] for c in sa.inspect(bind).get_columns(table)}
        with op.batch_alter_table(table) as batch:
            for column in columns:
                if column.name not in existing:
                    batch.add_column(column)

    # Legacy save also used source=approved. Only explicit review evidence can
    # establish canonical history; never guess a queued job's original version.
    if {"chapters", "chapter_versions"} <= tables:
        versions = sa.Table("chapter_versions", sa.MetaData(), autoload_with=bind)
        chapters = sa.Table("chapters", sa.MetaData(), autoload_with=bind)
        if {"source", "generation_metadata", "created_at"} <= set(versions.c.keys()):
            rows = bind.execute(sa.select(versions).where(versions.c.source == "approved")
                                .order_by(versions.c.created_at, versions.c.id)).mappings()
            approved = {}
            for row in rows:
                metadata = row["generation_metadata"] or {}
                if isinstance(metadata, str):
                    try:
                        metadata = json.loads(metadata)
                    except ValueError:
                        continue
                if not isinstance(metadata, dict):
                    continue
                manual = metadata.get("manual_review") or {}
                trace = metadata.get("review_trace") or {}
                final = trace.get("final_review") if isinstance(trace, dict) else {}
                if (isinstance(manual, dict) and manual.get("action") == "approved") or (
                    isinstance(final, dict) and final.get("approved") is True
                ):
                    approved.setdefault(row["chapter_id"], []).append(row)
            for chapter in bind.execute(sa.select(chapters)).mappings():
                candidates = approved.get(chapter["id"], [])
                current = next((row for row in candidates
                                if row["id"] == chapter.get("current_version_id")), None)
                if current is not None:
                    version_id = current["id"]
                elif candidates and sum(row["created_at"] == candidates[-1]["created_at"]
                                        for row in candidates) == 1:
                    version_id = candidates[-1]["id"]
                else:
                    continue
                bind.execute(chapters.update().where(chapters.c.id == chapter["id"],
                             chapters.c.approved_version_id.is_(None)).values(approved_version_id=version_id))
    if "publish_jobs" in tables:
        jobs = sa.Table("publish_jobs", sa.MetaData(), autoload_with=bind)
        if {"status", "error_message"} <= set(jobs.c.keys()):
            bind.execute(jobs.update().where(jobs.c.chapter_version_id.is_(None),
                         jobs.c.status == "publish_pending").values(
                         status="publish_failed", error_message="legacy job has no approved version; review and create a new job"))


def downgrade():
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    for table, columns in reversed(list(_columns().items())):
        if table not in tables:
            continue
        existing = {c["name"] for c in sa.inspect(bind).get_columns(table)}
        with op.batch_alter_table(table) as batch:
            for column in reversed(columns):
                if column.name in existing:
                    batch.drop_column(column.name)
