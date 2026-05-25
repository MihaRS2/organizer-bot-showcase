"""Alembic environment — единая metadata от bot.models.Base."""
from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool

# ── путь к проекту ───────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(PROJECT_ROOT))

# ВАЖНО: импорт через bot.models — там единый Base видит ВСЕ модели.
from bot.models import Base  # noqa: E402  (импорт после sys.path.append)
from bot.db import Database  # noqa: E402

# ── Alembic config ───────────────────────────────────────────────────────────
config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Offline-режим: рендерим SQL без подключения."""
    url = config.get_main_option("sqlalchemy.url")
    if not url:
        Database.init()
        url = Database.get_engine().url.render_as_string(hide_password=False)

    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Online-режим: подключаемся к БД и накатываем миграции."""
    Database.init()
    connectable = Database.get_engine()

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
