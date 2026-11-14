"""Backfill act story arcs for books whose outline already has acts."""

import json
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

from app.agents.story_arcs import ACT_ARC_TYPE, build_story_arcs

revision = "20261114_01"
down_revision = "20261113_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if not {"books", "story_arcs"} <= set(sa.inspect(bind).get_table_names()):
        return
    books = sa.Table("books", sa.MetaData(), autoload_with=bind)
    arcs = sa.Table("story_arcs", sa.MetaData(), autoload_with=bind)
    has_acts = {
        row[0] for row in bind.execute(sa.select(arcs.c.book_id).where(arcs.c.arc_type == ACT_ARC_TYPE))
    }
    for book_id, outline in bind.execute(sa.select(books.c.id, books.c.book_outline)):
        if book_id in has_acts:
            continue
        if isinstance(outline, str):
            try:
                outline = json.loads(outline)
            except ValueError:
                continue
        rows = [
            {"id": f"story_arc_{uuid4().hex}", "book_id": book_id, **arc}
            for arc in build_story_arcs(outline)
        ]
        if rows:
            bind.execute(arcs.insert(), rows)


def downgrade() -> None:
    # Act arcs are derived from the outline and are rebuilt whenever it is saved.
    pass
