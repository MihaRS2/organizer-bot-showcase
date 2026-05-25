"""Все модели — единый импорт, чтобы Alembic видел всю metadata."""
from bot.models.base import Base
from bot.models.bot_state import BotState
from bot.models.employees import Employee
from bot.models.event_assignees import EventAssignee
from bot.models.events import Event
from bot.models.short_id_map import ShortIdMap

__all__ = [
    "Base",
    "BotState",
    "Employee",
    "EventAssignee",
    "Event",
    "ShortIdMap",
]
