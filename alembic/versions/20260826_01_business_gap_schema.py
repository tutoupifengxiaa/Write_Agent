"""Add the persisted fields introduced by the business-gap phase."""

from __future__ import annotations

from collections.abc import Callable

from alembic import op
import sqlalchemy as sa


revision = "20260826_01"
down_revision = None
branch_labels = None
depends_on = None


ColumnFactory = Callable[[], sa.Column]

SCHEMA_COLUMN_FACTORIES: dict[str, tuple[ColumnFactory, ...]] = {
    "scheduler_runs": (
        lambda: sa.Column(
            "reason_code",
            sa.String(length=64),
            nullable=False,
            server_default="unspecified",
        ),
        lambda: sa.Column(
            "duration_ms",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        lambda: sa.Column(
            "outcome_counts",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    ),
    "chapter_versions": (
        lambda: sa.Column(
            "generation_metadata",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    ),
}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())
    for table_name, factories in SCHEMA_COLUMN_FACTORIES.items():
        if table_name not in table_names:
            continue
        existing = {
            column["name"] for column in sa.inspect(bind).get_columns(table_name)
        }
        missing_columns = [
            column
            for factory in factories
            if (column := factory()).name not in existing
        ]
        if not missing_columns:
            continue
        with op.batch_alter_table(table_name) as batch:
            for column in missing_columns:
                batch.add_column(column)


def downgrade() -> None:
    bind = op.get_bind()
    table_names = set(sa.inspect(bind).get_table_names())
    for table_name, factories in reversed(SCHEMA_COLUMN_FACTORIES.items()):
        if table_name not in table_names:
            continue
        existing = {
            column["name"] for column in sa.inspect(bind).get_columns(table_name)
        }
        column_names = [factory().name for factory in factories]
        removable = [name for name in reversed(column_names) if name in existing]
        if not removable:
            continue
        with op.batch_alter_table(table_name) as batch:
            for column_name in removable:
                batch.drop_column(column_name)

