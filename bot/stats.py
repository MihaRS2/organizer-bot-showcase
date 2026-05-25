"""Сбор статистики по встречам за период."""
from __future__ import annotations

from datetime import datetime

import pytz

from bot.models.employees import Employee
from bot.models.event_assignees import EventAssignee
from bot.models.events import Event


def gather_stats_for_period(db_sess, start_msk: datetime, end_msk: datetime) -> str:
    start_utc = start_msk.astimezone(pytz.UTC).replace(tzinfo=None)
    end_utc = end_msk.astimezone(pytz.UTC).replace(tzinfo=None)

    events_period = (
        db_sess.query(Event)
        .filter(Event.start_time >= start_utc, Event.start_time <= end_utc)
        .all()
    )
    if not events_period:
        return "Статистика за указанный период:\n(нет событий)"

    event_ids = [ev.id for ev in events_period]

    # Один запрос на все assignees → нет N+1
    links = (
        db_sess.query(EventAssignee)
        .filter(EventAssignee.event_id.in_(event_ids))
        .all()
    ) if event_ids else []

    # employee lookup одним запросом
    employee_ids = {ln.employee_id for ln in links}
    employees = (
        {e.id: e for e in db_sess.query(Employee).filter(Employee.id.in_(employee_ids)).all()}
        if employee_ids else {}
    )

    # Группируем links по event_id
    links_by_event: dict[int, list[EventAssignee]] = {}
    for ln in links:
        links_by_event.setdefault(ln.event_id, []).append(ln)

    canceled_count = 0
    moved_count = 0
    total_taken = 0
    per_user: dict[str, dict[str, int]] = {}

    for ev in events_period:
        if ev.was_canceled:
            canceled_count += 1
        if (ev.moved_count or 0) > 0:
            moved_count += 1

        ev_links = links_by_event.get(ev.id, [])
        if ev_links and not ev.was_canceled:
            total_taken += 1

        for ln in ev_links:
            if ev.was_canceled:
                continue
            emp = employees.get(ln.employee_id)
            if not emp:
                continue
            key = f"@{emp.username}" if emp.username else f"ID_{emp.user_id}"
            st = per_user.setdefault(key, {
                "count": 0, "tech_count": 0, "leading_count": 0, "helping_count": 0
            })
            st["count"] += 1
            if ln.is_lead:
                st["leading_count"] += 1
                if ev.is_technical:
                    st["tech_count"] += 1
            else:
                st["helping_count"] += 1

    lines = [
        "Статистика за указанный период:",
        f"• Взятых встреч (не отменённых): {total_taken}",
        f"• Отменённых встреч: {canceled_count}",
        f"• Перенесённых встреч: {moved_count}",
    ]

    if per_user:
        lines.append("\nПо сотрудникам:")
        # Сортируем по убыванию total count для читаемости
        for user, st in sorted(per_user.items(), key=lambda kv: -kv[1]["count"]):
            lines.append(
                f"  {user}: {st['count']} (тех: {st['tech_count']}), "
                f"ведёт: {st['leading_count']}, помогает: {st['helping_count']}"
            )
    else:
        lines.append("\n(Никто не брал встречи или нет данных)")

    return "\n".join(lines)
