"""Record timestamps for new publish jobs without inventing legacy times."""

from alembic import op
import sqlalchemy as sa

revision = "20260908_01"
down_revision = "20260826_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "publish_jobs" not in inspector.get_table_names():
        return
    if "created_at" not in {column["name"] for column in inspector.get_columns("publish_jobs")}:
        with op.batch_alter_table("publish_jobs") as batch:
            batch.add_column(sa.Column("created_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "publish_jobs" not in inspector.get_table_names():
        return
    if "created_at" in {column["name"] for column in inspector.get_columns("publish_jobs")}:
        with op.batch_alter_table("publish_jobs") as batch:
            batch.drop_column("created_at")
