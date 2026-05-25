"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-05-25 12:00:00

Эта миграция создаёт ПОЛНУЮ актуальную схему: employees, events,
event_assignees, short_id_map, bot_state — с правильными ограничениями
(UniqueConstraint на (event_id, employee_id), partial unique index для is_lead).

Если у вас УЖЕ есть БД со старой схемой — НЕ запускайте upgrade.
Вместо этого выполните:
    alembic stamp 0001_initial
и сразу после этого создайте отдельную миграцию ALTER, чтобы добавить
недостающие constraints/индексы вручную (см. README).
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── employees ───────────────────────────────────────────────────────────
    op.create_table(
        "employees",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("username", sa.String(), nullable=True),
        sa.UniqueConstraint("user_id", name="uq_employees_user_id"),
    )
    op.create_index("ix_employees_user_id", "employees", ["user_id"])
    op.create_index("ix_employees_username", "employees", ["username"])

    # ── events ──────────────────────────────────────────────────────────────
    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_id", sa.String(), nullable=True),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("start_time", sa.DateTime(), nullable=True),
        sa.Column("end_time", sa.DateTime(), nullable=True),
        sa.Column("is_technical", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("was_canceled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("moved_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("is_taken", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("meeting_link", sa.String(), nullable=True),
        sa.Column("is_day_in_day", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("discovered_date", sa.Date(), nullable=True),
        sa.Column("ten_min_alert_sent", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("morning_alert_sent", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("last_message_id", sa.Integer(), nullable=True),
        sa.Column("last_message_chat_id", sa.BigInteger(), nullable=True),
    )
    op.create_index("ix_events_event_id", "events", ["event_id"])

    # ── event_assignees ─────────────────────────────────────────────────────
    op.create_table(
        "event_assignees",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id", ondelete="CASCADE"), nullable=False),
        sa.Column("is_lead", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("event_id", "employee_id", name="uq_event_assignees_event_emp"),
    )
    # Один ведущий на встречу (PostgreSQL partial unique index)
    op.create_index(
        "uq_event_assignees_one_lead",
        "event_assignees",
        ["event_id"],
        unique=True,
        postgresql_where=sa.text("is_lead = true"),
    )

    # ── short_id_map ────────────────────────────────────────────────────────
    op.create_table(
        "short_id_map",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("short_id", sa.String(length=32), nullable=False),
        sa.Column("event_id", sa.String(), nullable=False),
        sa.UniqueConstraint("short_id", name="uq_short_id_map_short_id"),
    )
    op.create_index("ix_short_id_map_short_id", "short_id_map", ["short_id"])

    # ── bot_state ───────────────────────────────────────────────────────────
    op.create_table(
        "bot_state",
        sa.Column("key", sa.String(length=255), primary_key=True),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )


def downgrade() -> None:
    op.drop_table("bot_state")
    op.drop_index("ix_short_id_map_short_id", table_name="short_id_map")
    op.drop_table("short_id_map")
    op.drop_index("uq_event_assignees_one_lead", table_name="event_assignees")
    op.drop_table("event_assignees")
    op.drop_index("ix_events_event_id", table_name="events")
    op.drop_table("events")
    op.drop_index("ix_employees_username", table_name="employees")
    op.drop_index("ix_employees_user_id", table_name="employees")
    op.drop_table("employees")
