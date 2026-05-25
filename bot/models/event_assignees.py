from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer, UniqueConstraint

from bot.models.base import Base


class EventAssignee(Base):
    __tablename__ = "event_assignees"

    id = Column(Integer, primary_key=True)
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    employee_id = Column(Integer, ForeignKey("employees.id", ondelete="CASCADE"), nullable=False)

    is_lead = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        # Один и тот же сотрудник не может участвовать дважды в одной встрече
        UniqueConstraint("event_id", "employee_id", name="uq_event_assignees_event_emp"),
        # На одну встречу — максимум один ведущий (partial unique index, PostgreSQL)
        Index(
            "uq_event_assignees_one_lead",
            "event_id",
            unique=True,
            postgresql_where=(is_lead.is_(True)),
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<EventAssignee event_id={self.event_id} employee_id={self.employee_id} "
            f"is_lead={self.is_lead}>"
        )
