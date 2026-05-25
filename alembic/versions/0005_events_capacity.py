"""events: add organizer_email + capacity fields

Revision ID: 0005_events_capacity
Revises: 0004_employees_legacy_cleanup
Create Date: 2026-05-25 21:00:00

Добавляет поля для контроля лимита «не более 3 встреч в один интервал»:
  - organizer_email (из CalDAV ORGANIZER, нужен для тэга организатора в чате)
  - is_over_capacity (bool): True если встреча — 4-я+ пересекающаяся
  - capacity_rank (int): 1/2/3 = ок, 4+ = нарушение
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005_events_capacity"
down_revision: Union[str, None] = "0004_employees_legacy_cleanup"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("events", sa.Column("organizer_email", sa.String(), nullable=True))
    op.add_column("events", sa.Column(
        "is_over_capacity", sa.Boolean(), nullable=False, server_default=sa.text("false")
    ))
    op.add_column("events", sa.Column(
        "capacity_rank", sa.Integer(), nullable=False, server_default=sa.text("0")
    ))
    # Для быстрого поиска нарушений за день
    op.create_index(
        "ix_events_over_capacity",
        "events",
        ["start_time"],
        postgresql_where=sa.text("is_over_capacity = true AND was_canceled = false"),
    )


def downgrade() -> None:
    op.drop_index("ix_events_over_capacity", table_name="events")
    op.drop_column("events", "capacity_rank")
    op.drop_column("events", "is_over_capacity")
    op.drop_column("events", "organizer_email")
