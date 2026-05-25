"""Асинхронный клиент для одного CalDAV-календаря.

Извлекает событие и его ORGANIZER (понадобится для контроля лимита
встреч на интервал — этап 2 разработки).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional
from urllib.parse import urlsplit, urlunsplit

import caldav

from bot.common_helpers import parse_link

_log = logging.getLogger(__name__)


def _to_utc(dt) -> datetime:
    if hasattr(dt, "tzinfo"):
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    return datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc)


def _extract_email_from_prop(prop) -> Optional[str]:
    """Из vobject ORGANIZER/ATTENDEE достаём email.

    Формат: prop.value = "mailto:user@example.com"
    Бывают параметры (CN, SENT-BY и т.п.) — игнорируем.
    """
    if prop is None:
        return None
    raw = getattr(prop, "value", None)
    if not raw:
        return None
    raw = str(raw).strip()
    if raw.lower().startswith("mailto:"):
        raw = raw[7:]
    return raw.lower() or None


# Совместимость со старым именем
_extract_organizer_email = _extract_email_from_prop


# Email владельца shared-календаря — на нём ORGANIZER во всех событиях
# shared-инбокса. Реального организатора ищем в ATTENDEE.
_SHARED_CALENDAR_OWNERS = ("calendar-owner@example.com",)


def _pick_real_organizer(vevent) -> Optional[str]:
    """Выбирает «реального» организатора встречи из VEVENT.

    Логика:
      1. Если ORGANIZER не из списка _SHARED_CALENDAR_OWNERS — возвращаем его
      2. Иначе ищем первого ATTENDEE с доменом @example.com,
         НЕ из owners-списка, желательно с PARTSTAT=ACCEPTED
      3. Если такого нет — fallback на ORGANIZER (хоть и shared)

    Этот алгоритм покрывает кейс mail.ru shared-календаря, где
    ORGANIZER всегда равен calendar-owner@example.com, а настоящий
    организатор сидит первым ATTENDEE.
    """
    organizer_prop = getattr(vevent, "organizer", None)
    organizer_email = _extract_email_from_prop(organizer_prop)

    # Если ORGANIZER не shared-владелец — берём его
    if organizer_email and organizer_email not in _SHARED_CALENDAR_OWNERS:
        return organizer_email

    # Идём по ATTENDEE: сначала с ACCEPTED, потом любой подходящий @example.com
    attendees = getattr(vevent, "attendee_list", None) or []

    accepted_match: Optional[str] = None
    any_match: Optional[str] = None

    for att in attendees:
        email = _extract_email_from_prop(att)
        if not email:
            continue
        if email in _SHARED_CALENDAR_OWNERS:
            continue
        # Корпоративный домен — настоящий организатор почти всегда @example.com
        if not email.endswith("@example.com"):
            continue

        params = dict(getattr(att, "params", {}) or {})
        partstat = params.get("PARTSTAT")
        if isinstance(partstat, list):
            partstat = partstat[0] if partstat else None

        if any_match is None:
            any_match = email
        if partstat == "ACCEPTED" and accepted_match is None:
            accepted_match = email
            # ACCEPTED — лучший кандидат, можно сразу выходить
            break

    return accepted_match or any_match or organizer_email


@dataclass
class CalDavEvent:
    event_id: str
    summary: str
    start: datetime
    end: datetime
    organizer_email: Optional[str] = None
    description: Optional[str] = None
    url: Optional[str] = None
    change_type: str = "new"  # new | updated | cancelled


class CalDavClient:
    """Асинхронный клиент для одного CalDAV-календаря."""

    def __init__(
        self,
        *,
        username: str,
        password: str,
        calendar_url: str,
    ) -> None:
        if not (username and password and calendar_url):
            raise ValueError("username, password и calendar_url обязательны")

        parts = urlsplit(calendar_url)
        base_url = urlunsplit((parts.scheme, parts.netloc, "/", "", ""))

        self._client = caldav.DAVClient(url=base_url, username=username, password=password)
        self._calendar = caldav.Calendar(client=self._client, url=calendar_url)

        try:
            pr = self._client.principal()
            _log.info(
                "CalDavClient: principal OK › %s ; calendar=%s",
                getattr(pr, "url", "(n/a)"), calendar_url,
            )
        except Exception as e:  # noqa: BLE001
            _log.warning(
                "CalDavClient: principal discovery failed (%s). "
                "Continue with calendar URL only.", e,
            )

    def _date_search(self, start: datetime, end: datetime):
        try:
            return self._calendar.search(start=start, end=end, expand=True)
        except Exception as exc:  # noqa: BLE001
            _log.warning("calendar.search failed (%s), trying date_search()", exc)
            try:
                return self._calendar.date_search(start, end, expand=True)
            except Exception as exc2:  # noqa: BLE001
                raise RuntimeError(
                    f"CalDAV date_search failed ({exc2}). "
                    "Проверьте CALDAV_CALENDAR_URL: нужен URL CalDAV-коллекции, "
                    "а не веб-страницы."
                ) from exc2

    async def get_upcoming_events(self, start: datetime, end: datetime) -> List[CalDavEvent]:
        events = await asyncio.to_thread(self._date_search, start, end)
        out: List[CalDavEvent] = []
        for ev in events:
            v = ev.vobject_instance.vevent

            uid_prop = getattr(v, "uid", None)
            event_id = uid_prop.value if uid_prop else getattr(ev, "href", str(ev))

            sum_prop = getattr(v, "summary", None)
            summary = sum_prop.value if sum_prop else "Без названия"

            dtstart = v.dtstart.value
            dtend_prop = getattr(v, "dtend", None)
            dtend = dtend_prop.value if dtend_prop else dtstart

            desc_prop = getattr(v, "description", None)
            description = desc_prop.value if desc_prop else None

            url_prop = getattr(v, "url", None)
            url = url_prop.value if url_prop else None
            if not url and description:
                url = parse_link(description)

            organizer_email = _pick_real_organizer(v)

            out.append(
                CalDavEvent(
                    event_id=event_id,
                    summary=summary,
                    start=_to_utc(dtstart),
                    end=_to_utc(dtend),
                    organizer_email=organizer_email,
                    description=description,
                    url=url,
                    change_type="new",
                )
            )
        return out
