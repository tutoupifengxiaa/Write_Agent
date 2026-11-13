"""Index scheduler queues and related foreign keys."""

from alembic import op
import sqlalchemy as sa

revision = "20261113_01"
down_revision = "20261016_01"
branch_labels = None
depends_on = None


INDEX_COLUMNS = {
    "chapters": [("status", "next_attempt_at"), ("book_id",)],
    "publish_jobs": [("status", "next_attempt_at"), ("chapter_id",)],
    "chapter_versions": [("chapter_id",)],
    "continuity_events": [("chapter_id",)],
    "memory_embeddings": [("book_id",)],
    "character_snapshots": [("chapter_id",), ("character_id",)],
    "story_arcs": [("book_id",)],
    "characters": [("book_id",)],
}


def _indexes():
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    for table, column_groups in INDEX_COLUMNS.items():
        if table not in tables:
            continue
        existing = {index["name"] for index in inspector.get_indexes(table)}
        available = {column["name"] for column in inspector.get_columns(table)}
        for columns in column_groups:
            if not available.issuperset(columns):
                continue
            name = f"ix_{table}_{'_'.join(columns)}"
            yield table, columns, name, name in existing


def upgrade():
    for table, columns, name, exists in _indexes():
        if not exists:
            op.create_index(name, table, list(columns))


def downgrade():
    for table, _, name, exists in _indexes():
        if exists:
            op.drop_index(name, table_name=table)
