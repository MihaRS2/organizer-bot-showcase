"""Key-value хранилище состояния бота (anti-spam, idempotency)."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Column, DateTime, String, Text
from sqlalchemy.orm import Session

from bot.models.base import Base


class BotState(Base):
    """
    Ключ-значение для:
      - флагов «утренний дайджест уже отправлен за дату»
      - идемпотентности «перенесена/отменена» (move_notified:*, cancel_notified:*)

    created_at нужен для cron-очистки старых записей (см. bot.services.cleanup).
    """
    __tablename__ = "bot_state"

    key = Column(String(255), primary_key=True)
    value = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


def get_state(session: Session, key: str) -> Optional[str]:
    row = session.get(BotState, key)
    return row.value if row else None


def set_state(session: Session, key: str, value: Optional[str] = "1") -> None:
    row = session.get(BotState, key)
    if row:
        row.value = value
    else:
        session.add(BotState(key=key, value=value))
