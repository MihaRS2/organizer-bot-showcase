from __future__ import annotations

from sqlalchemy import Column, Integer, String

from bot.models.base import Base


class ShortIdMap(Base):
    """
    Связка (short_id → event_id) — чтобы inline-кнопки с short_id
    продолжали работать после перезапуска бота.
    """
    __tablename__ = "short_id_map"

    id = Column(Integer, primary_key=True)
    short_id = Column(String(32), unique=True, index=True, nullable=False)
    event_id = Column(String, nullable=False)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ShortIdMap short_id={self.short_id!r} event_id={self.event_id!r}>"
