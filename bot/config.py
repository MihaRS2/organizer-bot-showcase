"""Загрузка конфигурации с поддержкой Docker secrets через *_FILE-паттерн.

Поведение:
- Если задана переменная окружения <NAME>_FILE, читаем секрет из этого файла.
- Иначе — берём <NAME> из env (обратная совместимость с .env-подходом).

Это стандарт 12-Factor App; того же поведения придерживается postgres-image,
mariadb-image, vault-agent и др.
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def _read_secret(name: str, default: str = "") -> str:
    """Прочитать секрет: сначала из <name>_FILE, потом из env <name>."""
    file_path = os.getenv(f"{name}_FILE")
    if file_path:
        try:
            with open(file_path, encoding="utf-8") as f:
                value = f.read().strip()
            if value:
                return value
            logger.warning(
                "Secret file %s exists but is empty, falling back to env %s",
                file_path, name,
            )
        except FileNotFoundError:
            logger.warning(
                "Secret file %s not found, falling back to env %s",
                file_path, name,
            )
        except OSError as exc:  # noqa: BLE001
            logger.error("Failed to read secret file %s: %s", file_path, exc)
    return os.getenv(name, default)


def _mask(value: str, visible: int = 4) -> str:
    if not value:
        return "<empty>"
    if len(value) <= visible * 2:
        return "*" * len(value)
    return f"{value[:visible]}…{'*' * 4}"


def _require(name: str, value: str) -> str:
    if not value:
        raise RuntimeError(
            f"Не задана обязательная переменная {name} "
            f"(ни в env, ни в файле {name}_FILE). См. README."
        )
    return value


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} должен быть целым числом, получено: {raw!r}") from exc


class BotConfig:
    # ── Telegram ────────────────────────────────────────────────────────────
    BOT_TOKEN: str = _read_secret("BOT_TOKEN")
    BOT_TOKEN_ENCRYPTED: str = _read_secret("BOT_TOKEN_ENCRYPTED")
    ENCRYPTION_KEY: str = _read_secret("ENCRYPTION_KEY")

    # ── CalDAV ──────────────────────────────────────────────────────────────
    CALDAV_USERNAME: str = _read_secret("CALDAV_USERNAME")
    CALDAV_PASSWORD: str = _read_secret("CALDAV_PASSWORD")
    CALDAV_CALENDAR_URL: str = _read_secret("CALDAV_CALENDAR_URL")

    # ── Postgres ────────────────────────────────────────────────────────────
    DB_HOST: str = os.getenv("DB_HOST", "bot_db")
    DB_PORT: str = os.getenv("DB_PORT", "5432")
    DB_NAME: str = os.getenv("DB_NAME", "mydb")
    DB_USER: str = os.getenv("DB_USER", "myuser")
    DB_PASSWORD: str = _read_secret("DB_PASSWORD")

    # ── Чаты Telegram (не секреты) ──────────────────────────────────────────
    SUPPORT_CHAT_ID: int = _env_int("SUPPORT_CHAT_ID", 0)
    SALES_CHAT_ID: int = _env_int("SALES_CHAT_ID", 0)

    # ── Интервалы ───────────────────────────────────────────────────────────
    CHECK_INTERVAL_MINUTES: int = _env_int("CHECK_INTERVAL_MINUTES", 30)
    DAILY_NOTIFICATION_HOUR: int = _env_int("DAILY_NOTIFICATION_HOUR", 20)
    MORNING_REPORT_HOUR: int = _env_int("MORNING_REPORT_HOUR", 7)
    MORNING_DIGEST_HOUR: int = _env_int("MORNING_DIGEST_HOUR", 7)
    MORNING_DIGEST_MIN: int = _env_int("MORNING_DIGEST_MIN", 0)

    # ── Прочее ──────────────────────────────────────────────────────────────
    LOGLEVEL: str = os.getenv("LOGLEVEL", "INFO")

    # ── HRBox (этап 2) ──────────────────────────────────────────────────────
    HRBOX_API_URL: str = os.getenv("HRBOX_API_URL", "https://hr-system.example.com/api/v1")
    HRBOX_CLIENT_ID: str = _read_secret("HRBOX_CLIENT_ID")
    HRBOX_CLIENT_SECRET: str = _read_secret("HRBOX_CLIENT_SECRET")
    HRBOX_SYNC_INTERVAL_MIN: int = _env_int("HRBOX_SYNC_INTERVAL_MIN", 60)

    @classmethod
    def validate(cls) -> None:
        if not cls.BOT_TOKEN and not (cls.BOT_TOKEN_ENCRYPTED and cls.ENCRYPTION_KEY):
            raise RuntimeError(
                "Не задан BOT_TOKEN (или пара BOT_TOKEN_ENCRYPTED + ENCRYPTION_KEY)."
            )
        _require("CALDAV_USERNAME", cls.CALDAV_USERNAME)
        _require("CALDAV_PASSWORD", cls.CALDAV_PASSWORD)
        _require("CALDAV_CALENDAR_URL", cls.CALDAV_CALENDAR_URL)
        _require("DB_PASSWORD", cls.DB_PASSWORD)
        if cls.SUPPORT_CHAT_ID == 0:
            logger.warning("SUPPORT_CHAT_ID=0 — команды emp_* будут недоступны")
        if cls.SALES_CHAT_ID == 0:
            raise RuntimeError("Не задан SALES_CHAT_ID — некуда отправлять карточки встреч.")

    @classmethod
    def log_safe_summary(cls) -> None:
        items: dict[str, Any] = {
            "BOT_TOKEN": _mask(cls.BOT_TOKEN),
            "CALDAV_USERNAME": cls.CALDAV_USERNAME,
            "CALDAV_PASSWORD": _mask(cls.CALDAV_PASSWORD),
            "CALDAV_CALENDAR_URL": cls.CALDAV_CALENDAR_URL,
            "DB_HOST": cls.DB_HOST,
            "DB_PORT": cls.DB_PORT,
            "DB_NAME": cls.DB_NAME,
            "DB_USER": cls.DB_USER,
            "DB_PASSWORD": _mask(cls.DB_PASSWORD),
            "SUPPORT_CHAT_ID": cls.SUPPORT_CHAT_ID,
            "SALES_CHAT_ID": cls.SALES_CHAT_ID,
            "CHECK_INTERVAL_MINUTES": cls.CHECK_INTERVAL_MINUTES,
            "MORNING_DIGEST": f"{cls.MORNING_DIGEST_HOUR:02d}:{cls.MORNING_DIGEST_MIN:02d}",
            "HRBOX_CLIENT_ID": _mask(cls.HRBOX_CLIENT_ID) if cls.HRBOX_CLIENT_ID else "<not set>",
            "HRBOX_CLIENT_SECRET": _mask(cls.HRBOX_CLIENT_SECRET) if cls.HRBOX_CLIENT_SECRET else "<not set>",
            "LOGLEVEL": cls.LOGLEVEL,
        }
        logger.info("Loaded config: %s", items)
