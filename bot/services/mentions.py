"""Хелпер: формат тэга организатора по email.

При срабатывании лимита 3 встреч мы хотим показать в чате, кто это организовал.
Если в БД есть Employee с этим email и у него есть @username — тегаем его.
Иначе показываем ФИО + email (это «полусейлзы» без TG-привязки).
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from bot.models.employees import Employee


def format_organizer_mention(db: Session, organizer_email: Optional[str]) -> str:
    """Возвращает строку для упоминания организатора в чате.

    Примеры:
      - "@e_knyazev (Князев Элвин)"  — если есть и TG, и ФИО
      - "Князев Элвин (e.knyazev@example.com)" — только ФИО + email
      - "e.knyazev@example.com" — есть только email
      - "(организатор неизвестен)" — нет даже email
    """
    if not organizer_email:
        return "(организатор неизвестен)"

    email = organizer_email.lower().strip()
    emp = db.query(Employee).filter_by(email=email).first()

    if not emp:
        return email

    if emp.username and emp.full_name:
        return f"@{emp.username} ({emp.full_name})"
    if emp.username:
        return f"@{emp.username}"
    if emp.full_name:
        return f"{emp.full_name} ({email})"
    return email
