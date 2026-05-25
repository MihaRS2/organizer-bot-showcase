"""Контроль лимита «не более 3 встреч в одно время».

Логика:
  - В рамках одного календарного дня (МСК) берём все НЕ отменённые встречи
  - Сортируем по (start_time, id) — стабильный порядок
  - Для каждой встречи считаем: сколько ДРУГИХ встреч с меньшим (start_time, id)
    пересекаются по времени с ней. Это её `capacity_rank` (0-indexed)
  - Если rank >= 3 (т.е. 4-я и далее) — `is_over_capacity = True`

Алгоритм O(N²) на день, но N обычно 10-30 → пренебрежимо.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

import pytz
from sqlalchemy.orm import Session

from bot.models.events import Event

log = logging.getLogger(__name__)
MOSCOW_TZ = pytz.timezone("Europe/Moscow")

# Лимит — сколько встреч максимум разрешено в один пересекающийся интервал
CAPACITY_LIMIT = 3


def _overlap(a_start, a_end, b_start, b_end) -> bool:
    """Пересекаются ли интервалы [a) и [b)."""
    return a_start < b_end and b_start < a_end


@dataclass
class CapacityChange:
    """Информация о смене статуса is_over_capacity у события."""
    event_id: int
    raw_event_id: str
    title: str
    organizer_email: Optional[str]
    start_time_utc: datetime
    end_time_utc: datetime
    new_rank: int
    became_over_capacity: bool


def recompute_for_day(
    db: Session,
    day_start_msk: datetime,
    day_end_msk: datetime,
) -> List[CapacityChange]:
    """Пересчитывает capacity_rank/is_over_capacity для всех встреч за день.

    Возвращает список ИЗМЕНЕНИЙ: только те события, у которых статус
    is_over_capacity ПЕРЕМЕНИЛСЯ за этот вызов (false→true в первую очередь).
    Это нужно чтобы caller мог отправить мгновенный алерт только за новые
    нарушения, не спамя по старым.
    """
    start_utc = day_start_msk.astimezone(pytz.UTC).replace(tzinfo=None)
    end_utc = day_end_msk.astimezone(pytz.UTC).replace(tzinfo=None)

    events: List[Event] = (
        db.query(Event)
        .filter(Event.was_canceled.is_(False))
        .filter(Event.start_time >= start_utc, Event.start_time < end_utc)
        .order_by(Event.start_time.asc(), Event.id.asc())
        .all()
    )

    changes: List[CapacityChange] = []

    for i, ev in enumerate(events):
        # Сколько предыдущих по (start_time, id) встреч пересекается с этой
        overlap_count = 0
        for j in range(i):
            other = events[j]
            if _overlap(ev.start_time, ev.end_time, other.start_time, other.end_time):
                overlap_count += 1

        new_rank = overlap_count + 1  # 1-indexed: первая в группе = rank 1
        new_over = new_rank > CAPACITY_LIMIT

        prev_over = bool(ev.is_over_capacity)
        prev_rank = ev.capacity_rank or 0

        if new_over != prev_over or new_rank != prev_rank:
            became_over = (new_over and not prev_over)
            ev.capacity_rank = new_rank
            ev.is_over_capacity = new_over

            if became_over or (new_over and prev_over is False):
                # became_over: переход false→true
                changes.append(CapacityChange(
                    event_id=ev.id,
                    raw_event_id=ev.event_id,
                    title=ev.title or "",
                    organizer_email=ev.organizer_email,
                    start_time_utc=ev.start_time,
                    end_time_utc=ev.end_time,
                    new_rank=new_rank,
                    became_over_capacity=True,
                ))

    return changes


def recompute_for_today(db: Session) -> List[CapacityChange]:
    """Удобный wrapper: пересчитать на текущий день (МСК)."""
    now = datetime.now(MOSCOW_TZ)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    return recompute_for_day(db, day_start, day_end)


def get_violations_for_day(
    db: Session,
    day_start_msk: datetime,
    day_end_msk: datetime,
) -> List[Event]:
    """Все встречи за день, помеченные как is_over_capacity=True."""
    start_utc = day_start_msk.astimezone(pytz.UTC).replace(tzinfo=None)
    end_utc = day_end_msk.astimezone(pytz.UTC).replace(tzinfo=None)
    return (
        db.query(Event)
        .filter(Event.was_canceled.is_(False))
        .filter(Event.is_over_capacity.is_(True))
        .filter(Event.start_time >= start_utc, Event.start_time < end_utc)
        .order_by(Event.start_time.asc())
        .all()
    )
