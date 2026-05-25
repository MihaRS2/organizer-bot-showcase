"""employees: HRBox fields (hrbox_id, email, role, etc.)

Revision ID: 0003_employees_hrbox
Revises: 0002_harden_constraints
Create Date: 2026-05-25 17:30:00

Расширяет таблицу employees полями из HRBox, делает user_id NULLABLE
(чтобы HRBox-сотрудник мог жить в БД до того как сделает /start или
будет привязан через /emp_link).

Все существующие 13 сотрудников помечаются role='engineer' — они уже
в support-чате и берут технические встречи.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003_employees_hrbox"
down_revision: Union[str, None] = "0002_harden_constraints"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1) Новые колонки. role NOT NULL с server_default='other'
    op.add_column("employees", sa.Column("hrbox_id", sa.String(), nullable=True))
    op.add_column("employees", sa.Column("email", sa.String(), nullable=True))
    op.add_column("employees", sa.Column("full_name", sa.String(), nullable=True))
    op.add_column("employees", sa.Column(
        "role", sa.String(length=20), nullable=False, server_default="other"
    ))
    op.add_column("employees", sa.Column("position", sa.String(), nullable=True))
    op.add_column("employees", sa.Column("department_id", sa.String(), nullable=True))
    op.add_column("employees", sa.Column("department_name", sa.String(), nullable=True))
    op.add_column("employees", sa.Column(
        "is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")
    ))
    op.add_column("employees", sa.Column("synced_at", sa.DateTime(), nullable=True))

    # 2) Unique-индексы для hrbox_id / email (NULLs допускаются — постгрес считает NULL != NULL)
    op.create_index("ix_employees_hrbox_id", "employees", ["hrbox_id"], unique=True)
    op.create_index("ix_employees_email", "employees", ["email"], unique=True)

    # 3) Backfill: всех существующих помечаем как инженеров.
    #    Они уже в SUPPORT_CHAT_ID и берут технические встречи — это саппорт.
    op.execute("UPDATE employees SET role = 'engineer' WHERE role = 'other'")

    # 4) user_id: было NOT NULL — делаем NULLABLE (для HRBox-сотрудников без TG).
    op.alter_column("employees", "user_id", existing_type=sa.String(), nullable=True)


def downgrade() -> None:
    # Возврат user_id в NOT NULL может упасть если в БД появились NULL'ы — это нормально.
    op.alter_column("employees", "user_id", existing_type=sa.String(), nullable=False)
    op.drop_index("ix_employees_email", table_name="employees")
    op.drop_index("ix_employees_hrbox_id", table_name="employees")
    op.drop_column("employees", "synced_at")
    op.drop_column("employees", "is_active")
    op.drop_column("employees", "department_name")
    op.drop_column("employees", "department_id")
    op.drop_column("employees", "position")
    op.drop_column("employees", "role")
    op.drop_column("employees", "full_name")
    op.drop_column("employees", "email")
    op.drop_column("employees", "hrbox_id")
