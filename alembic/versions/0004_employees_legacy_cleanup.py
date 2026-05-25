"""employees: cleanup legacy columns and align user_id type with model

Revision ID: 0004_employees_legacy_cleanup
Revises: 0003_employees_hrbox
Create Date: 2026-05-25 19:50:00

В проде таблица employees была создана старой версией бота (до Alembic-миграций):
  - user_id BIGINT  (в модели — String)
  - first_name, last_name TEXT  (в модели нет, удалить)
  - created_at, updated_at TIMESTAMP  (в модели нет, удалить)

Эта миграция приводит схему к виду модели:
  - переименовываем user_id → user_id_old (для сохранности данных)
  - создаём user_id VARCHAR с переносом значений (bigint → text)
  - дропаем user_id_old и легаси-колонки
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004_employees_legacy_cleanup"
down_revision: Union[str, None] = "0003_employees_hrbox"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1) ALTER TYPE для user_id: bigint → varchar (с явным cast)
    #    Сначала снимаем UNIQUE-constraint, потом меняем тип, потом обратно вешаем.
    op.execute("ALTER TABLE employees DROP CONSTRAINT IF EXISTS employees_user_id_key;")
    op.execute(
        "ALTER TABLE employees "
        "ALTER COLUMN user_id TYPE varchar USING user_id::varchar;"
    )
    # Создаём индекс заново. UNIQUE сохраняется через unique=True у SQLAlchemy в модели.
    op.create_index(
        "ix_employees_user_id_unique",
        "employees",
        ["user_id"],
        unique=True,
    )

    # 2) Удаляем легаси-колонки
    op.drop_column("employees", "first_name")
    op.drop_column("employees", "last_name")
    op.drop_column("employees", "created_at")
    op.drop_column("employees", "updated_at")


def downgrade() -> None:
    # Восстанавливаем легаси-колонки (значения не вернутся — это просто ROLLBACK схемы)
    op.add_column("employees", sa.Column("first_name", sa.Text(), nullable=True))
    op.add_column("employees", sa.Column("last_name", sa.Text(), nullable=True))
    op.add_column("employees", sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=True,
        server_default=sa.text("now()"),
    ))
    op.add_column("employees", sa.Column(
        "updated_at", sa.DateTime(timezone=True), nullable=True,
        server_default=sa.text("now()"),
    ))

    # Возвращаем user_id обратно в bigint
    op.drop_index("ix_employees_user_id_unique", table_name="employees")
    op.execute(
        "ALTER TABLE employees "
        "ALTER COLUMN user_id TYPE bigint USING user_id::bigint;"
    )
    op.execute(
        "ALTER TABLE employees ADD CONSTRAINT employees_user_id_key UNIQUE (user_id);"
    )
