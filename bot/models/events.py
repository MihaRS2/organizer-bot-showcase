from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, Column, Date, DateTime, Integer, String

from bot.models.base import Base


class Event(Base):
    """CalDAV-встреча, синхронизируемая с ботом."""
    __tablename__ = "events"

    id = Column(Integer, primary_key=True)

    event_id = Column(String, index=True)        # UID в CalDAV-календаре
    title = Column(String)
    description = Column(String, nullable=True)

    start_time = Column(DateTime)                # naive UTC
    end_time = Column(DateTime)                  # naive UTC

    is_technical = Column(Boolean, default=False, nullable=False)
    was_canceled = Column(Boolean, default=False, nullable=False)
    moved_count = Column(Integer, default=0, nullable=False)
    is_taken = Column(Boolean, default=False, nullable=False)

    meeting_link = Column(String, nullable=True)

    is_day_in_day = Column(Boolean, default=False, nullable=False)
    discovered_date = Column(Date, nullable=True)

    ten_min_alert_sent = Column(Boolean, default=False, nullable=False)
    morning_alert_sent = Column(Boolean, default=False, nullable=False)

    # Привязка к карточке в чате (для reply и автоскролла)
    last_message_id = Column(Integer, nullable=True)
    last_message_chat_id = Column(BigInteger, nullable=True)

    # ── Capacity (этап 3) ───────────────────────────────────────────────────
    # organizer_email — из CalDAV ORGANIZER, для тэга в чате при перегрузе
    organizer_email = Column(String, nullable=True)
    # is_over_capacity — True если встреча 4-я+ в пересекающемся интервале
    is_over_capacity = Column(Boolean, default=False, nullable=False)
    # capacity_rank — 1/2/3 = ок, 4+ = нарушение
    capacity_rank = Column(Integer, default=0, nullable=False)

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<Event id={self.id} event_id={self.event_id!r} title={self.title!r} "
            f"canceled={self.was_canceled} moved={self.moved_count}>"
        )
