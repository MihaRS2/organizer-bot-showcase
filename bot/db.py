"""Подключение к Postgres + контекстный менеджер сессии."""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator
from urllib.parse import quote_plus

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from bot.config import BotConfig
from bot.models.base import Base  # единый Base, импортируем для совместимости

logger = logging.getLogger(__name__)


def _build_dsn() -> str:
    """Собирает DSN с url-encoded паролем (на случай спецсимволов)."""
    pw = quote_plus(BotConfig.DB_PASSWORD or "")
    user = quote_plus(BotConfig.DB_USER or "")
    return (
        f"postgresql+psycopg2://{user}:{pw}@"
        f"{BotConfig.DB_HOST}:{BotConfig.DB_PORT}/{BotConfig.DB_NAME}"
    )


def _masked_dsn() -> str:
    return (
        f"postgresql+psycopg2://{BotConfig.DB_USER}:***@"
        f"{BotConfig.DB_HOST}:{BotConfig.DB_PORT}/{BotConfig.DB_NAME}"
    )


class Database:
    _engine: Engine | None = None
    _SessionLocal: sessionmaker[Session] | None = None

    @classmethod
    def init(cls) -> None:
        if cls._engine is not None:
            return
        logger.info("Initialising DB engine: %s", _masked_dsn())
        cls._engine = create_engine(
            _build_dsn(),
            echo=False,
            future=True,
            pool_pre_ping=True,    # автоматический ping мёртвых соединений
            pool_recycle=1800,     # пере-открывать соединения старше 30 мин
        )
        cls._SessionLocal = sessionmaker(
            autocommit=False, autoflush=False, bind=cls._engine, future=True
        )
        logger.info("DB engine initialised.")

    @classmethod
    def get_engine(cls) -> Engine:
        if cls._engine is None:
            cls.init()
        assert cls._engine is not None
        return cls._engine

    @classmethod
    def get_session(cls) -> Session:
        """Возвращает новую сессию. Закрывать ОБЯЗАТЕЛЬНО (try/finally)."""
        if cls._engine is None:
            cls.init()
        assert cls._SessionLocal is not None
        return cls._SessionLocal()

    @classmethod
    @contextmanager
    def session(cls) -> Iterator[Session]:
        """
        Контекстный менеджер с автозакрытием. Использование:
            with Database.session() as db:
                db.query(...)
        """
        sess = cls.get_session()
        try:
            yield sess
        finally:
            sess.close()


__all__ = ["Base", "Database"]
