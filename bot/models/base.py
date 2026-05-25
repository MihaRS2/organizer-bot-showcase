"""Единый Base для всех моделей.

Раньше в проекте было два declarative_base() (в bot.db и в bot.models.bot_state),
из-за чего Alembic не видел таблицу bot_state в autogenerate.
Теперь все модели наследуются от одного Base, объявленного здесь.
"""
from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase, MappedAsDataclass  # noqa: F401


class Base(DeclarativeBase):
    """Общий declarative base для всех ORM-моделей проекта."""
    pass
