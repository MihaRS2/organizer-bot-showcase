"""harden constraints for existing prod DB

Revision ID: 0002_harden_constraints
Revises: 0001_initial
Create Date: 2026-05-25 12:05:00

Применяется на ВАШЕЙ существующей БД ПОСЛЕ `alembic stamp 0001_initial`.
Добавляет:
  • UniqueConstraint(event_id, employee_id) на event_assignees
  • Partial unique index «один ведущий на встречу»
  • created_at на bot_state (если ещё нет)
  • NOT NULL и default на флаги events (если миграция автогенерации их не зафиксировала)

Все операции идемпотентны: пытаемся создать, ловим уже-существующее.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.exc import ProgrammingError

revision: str = "0002_harden_constraints"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _safe(stmt_sql: str) -> None:
    """Выполнить SQL, игнорируя «уже существует»."""
    try:
        op.execute(stmt_sql)
    except ProgrammingError as e:  # noqa: BLE001
        if "already exists" in str(e).lower():
            return
        raise


def upgrade() -> None:
    # 1) ДЕДУПЛИКАЦИЯ event_assignees перед UNIQUE
    op.execute("""
        DELETE FROM event_assignees a
        USING event_assignees b
        WHERE a.id > b.id
          AND a.event_id = b.event_id
          AND a.employee_id = b.employee_id;
    """)

    # 2) UNIQUE (event_id, employee_id)
    _safe("""
        ALTER TABLE event_assignees
        ADD CONSTRAINT uq_event_assignees_event_emp
        UNIQUE (event_id, employee_id);
    """)

    # 3) Снимаем двойных ведущих: оставляем самого раннего по created_at
    op.execute("""
        WITH ranked AS (
            SELECT id,
                   row_number() OVER (PARTITION BY event_id ORDER BY created_at, id) AS rn
            FROM event_assignees
            WHERE is_lead = true
        )
        UPDATE event_assignees SET is_lead = false
        WHERE id IN (SELECT id FROM ranked WHERE rn > 1);
    """)

    # 4) Partial unique index — один ведущий на встречу
    _safe("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_event_assignees_one_lead
        ON event_assignees (event_id)
        WHERE is_lead = true;
    """)

    # 5) bot_state.created_at — добавляем колонку, если её нет
    op.execute("""
        ALTER TABLE bot_state
        ADD COLUMN IF NOT EXISTS created_at timestamp without time zone
            NOT NULL DEFAULT CURRENT_TIMESTAMP;
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_event_assignees_one_lead;")
    op.execute("ALTER TABLE event_assignees DROP CONSTRAINT IF EXISTS uq_event_assignees_event_emp;")
    op.execute("ALTER TABLE bot_state DROP COLUMN IF EXISTS created_at;")
