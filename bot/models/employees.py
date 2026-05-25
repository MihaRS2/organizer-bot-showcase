"""Сотрудник.

После HRBox-интеграции (этап 2) Employee — это объединённый объект:
  • HRBox-источник: hrbox_id, email, full_name, position, role, department, is_active
  • Telegram-источник: user_id, username (заполняется отдельно — через /emp_link или /emp_add)

Все поля кроме `id` и `role` — nullable, потому что:
  • запись может прилететь из HRBox без TG (нет user_id/username)
  • либо может быть создана вручную через /emp_add (нет hrbox_id/email)
  • либо может быть legacy-сотрудник, добавленный до HRBox (только user_id + username)

role — единственное обязательное; default = 'other'. После HRBox-синка обычно
становится 'engineer' или 'sales'.
"""
from __future__ import annotations

from sqlalchemy import Boolean, Column, DateTime, Integer, String

from bot.models.base import Base


# Допустимые роли — храним строкой (а не Enum) ради простоты миграций
ROLE_ENGINEER = "engineer"   # инженер тех.поддержки (саппорт)
ROLE_SALES = "sales"         # сейлз/менеджер
ROLE_OTHER = "other"         # всё прочее (не должно вести встречи)

VALID_ROLES = (ROLE_ENGINEER, ROLE_SALES, ROLE_OTHER)


class Employee(Base):
    __tablename__ = "employees"

    id = Column(Integer, primary_key=True)

    # ── HRBox identity ──────────────────────────────────────────────────────
    hrbox_id = Column(String, unique=True, nullable=True, index=True)
    email = Column(String, unique=True, nullable=True, index=True)

    # ── Telegram identity ───────────────────────────────────────────────────
    # NULLABLE: HRBox-сотрудник без TG до тех пор, пока его не связали /emp_link
    user_id = Column(String, unique=True, nullable=True, index=True)
    username = Column(String, nullable=True, index=True)

    # ── Профиль ─────────────────────────────────────────────────────────────
    full_name = Column(String, nullable=True)
    role = Column(String(20), nullable=False, default=ROLE_OTHER)
    position = Column(String, nullable=True)
    department_id = Column(String, nullable=True)
    department_name = Column(String, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)

    # ── Метаданные ──────────────────────────────────────────────────────────
    synced_at = Column(DateTime, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<Employee id={self.id} role={self.role} email={self.email!r} "
            f"username={self.username!r} active={self.is_active}>"
        )

    # ── Display helpers ─────────────────────────────────────────────────────
    @property
    def display_name(self) -> str:
        """Имя для отображения: @username, ФИО, email или ID."""
        if self.username:
            return f"@{self.username}"
        if self.full_name:
            return self.full_name
        if self.email:
            return self.email
        if self.user_id:
            return f"ID_{self.user_id}"
        return f"emp#{self.id}"

    @property
    def mention(self) -> str:
        """Тэг для чата: @username если есть, иначе ФИО или email (без tg-меншна)."""
        if self.username:
            return f"@{self.username}"
        return self.display_name
