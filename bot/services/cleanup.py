"""Периодическая чистка старых записей BotState (anti-spam ключи)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from bot.db import Database
from bot.models.bot_state import BotState

log = logging.getLogger(__name__)


# Эти типы ключей живут конкретно для одного дня/события, после 30 дней не нужны
_PURGE_PREFIXES = ("move_notified:", "cancel_notified:", "morning_digest_sent_")


def cleanup_old_bot_state(retention_days: int = 30) -> int:
    """Удаляет записи BotState старше retention_days. Возвращает кол-во удалённых."""
    cutoff = datetime.utcnow() - timedelta(days=retention_days)
    deleted = 0
    with Database.session() as db:
        # Ключи могут быть без created_at у легаси-записей (NULL не должен быть, но защитимся).
        q = db.query(BotState).filter(BotState.created_at < cutoff)
        # Дополнительно фильтруем по prefix — флаги «утренний дайджест» и спам-нотификации.
        # Прочие ключи (если появятся) не трогаем.
        for prefix in _PURGE_PREFIXES:
            n = q.filter(BotState.key.like(f"{prefix}%")).delete(synchronize_session=False)
            deleted += n
        db.commit()
    if deleted:
        log.info("cleanup_old_bot_state: removed %s rows older than %s days", deleted, retention_days)
    return deleted
