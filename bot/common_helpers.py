"""Общие хелперы: парсинг ссылок, нормализация описаний, проверка тех.встреч,
определение пересечения с планёрками поддержки, рендеринг карточки встречи.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
from datetime import datetime
from typing import List, Optional, Tuple

import pytz

from bot.models.employees import Employee
from bot.models.event_assignees import EventAssignee

logger = logging.getLogger(__name__)
MOSCOW_TZ = pytz.timezone("Europe/Moscow")

# ── Парсинг ссылок ──────────────────────────────────────────────────────────
_LINK_RE = re.compile(r"(https?://[^\s<>\"]+)", re.IGNORECASE)
_TRAILING_JUNK = "\"'>).,]»”’"
_LEADING_JUNK = "<(\"'([«“’"


def _clean_href(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    s = url.strip().lstrip(_LEADING_JUNK)
    m = _LINK_RE.search(s)
    if not m:
        return None
    s = m.group(1).rstrip(_TRAILING_JUNK)
    return (
        s.replace(" ", "%20")
         .replace('"', "%22")
         .replace("<", "%3C")
         .replace(">", "%3E")
    )


def parse_link(text: Optional[str]) -> Optional[str]:
    """Достаёт первую http(s)-ссылку из произвольного текста."""
    if not text:
        return None
    m = _LINK_RE.search(text)
    return _clean_href(m.group(1)) if m else None


def normalize_description(desc: Optional[str]) -> str:
    if not desc:
        return ""
    desc = desc.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(ln.strip() for ln in desc.split("\n") if ln.strip())


# ── Тех.встречи ─────────────────────────────────────────────────────────────
_JSON_PATH = os.path.join(os.path.dirname(__file__), "keywords.json")
try:
    with open(_JSON_PATH, encoding="utf-8") as f:
        _data = json.load(f)
except FileNotFoundError:
    logger.warning("keywords.json not found at %s — tech detection disabled", _JSON_PATH)
    _data = {"tech_keywords": []}

# Все ключевые слова в lowercase — detect_if_technical lowercase-ит title
TECH_KEYWORDS: List[str] = [k.lower() for k in _data.get("tech_keywords", [])]


def detect_if_technical(title: str) -> bool:
    lower = (title or "").lower()
    return any(k in lower for k in TECH_KEYWORDS)


# ── Планёрки тех.поддержки (МСК) ─────────────────────────────────────────────
# Пн(0), Ср(2): 15:00–16:00; Пт(4): 15:00–17:00
SUPPORT_PLANNING_SLOTS: dict[int, list[Tuple[int, int, int, int]]] = {
    0: [(15, 0, 16, 0)],
    2: [(15, 0, 16, 0)],
    4: [(15, 0, 17, 0)],
}


def _overlap(s1: datetime, e1: datetime, s2: datetime, e2: datetime) -> bool:
    return s1 < e2 and s2 < e1


def overlaps_support_planning(start_naive_utc: datetime, end_naive_utc: datetime) -> bool:
    """Пересекается ли встреча с одной из планёрок поддержки (вход — naive UTC)."""
    try:
        start_local = pytz.UTC.localize(start_naive_utc).astimezone(MOSCOW_TZ)
        end_local = pytz.UTC.localize(end_naive_utc).astimezone(MOSCOW_TZ)
    except Exception:  # noqa: BLE001
        return False

    for h1, m1, h2, m2 in SUPPORT_PLANNING_SLOTS.get(start_local.weekday(), []):
        slot_start = start_local.replace(hour=h1, minute=m1, second=0, microsecond=0)
        slot_end = start_local.replace(hour=h2, minute=m2, second=0, microsecond=0)
        if _overlap(start_local, end_local, slot_start, slot_end):
            return True
    return False


# ── Исключения встреч ───────────────────────────────────────────────────────
_EXCLUDE = {
    "support планерка",
    "большая планерка",
    "работа в офисе",
    "не ставить встречи до 10:30",
}


def is_excluded_event(title: str) -> bool:
    n = (title or "").lower().replace("ё", "е").strip()
    n = re.sub(r"\s+", " ", n)
    return n in _EXCLUDE


def filter_today_events(raw_events: List[dict], now_msk: datetime) -> List[dict]:
    today = now_msk.date()
    out: List[dict] = []
    for ev in raw_events:
        start_local = pytz.UTC.localize(ev["start"]).astimezone(MOSCOW_TZ)
        if start_local.date() == today:
            out.append(ev)
    return out


# ── Карточка встречи ────────────────────────────────────────────────────────
def _fmt_emp(db_sess, emp_id: int) -> str:
    emp = db_sess.get(Employee, emp_id)
    if not emp:
        return "Unknown"
    return f"@{emp.username}" if emp.username else f"ID_{emp.user_id}"


def build_assignees_text(db_sess, event_id: int) -> str:
    links = (
        db_sess.query(EventAssignee)
        .filter_by(event_id=event_id)
        .order_by(EventAssignee.created_at)
        .all()
    )
    if not links:
        return "Встречу никто не ведёт⚠️"

    lead = next((ln for ln in links if ln.is_lead), None)
    helpers = [ln for ln in links if not ln.is_lead]

    head = (
        f"Встречу ведёт инженер {_fmt_emp(db_sess, lead.employee_id)}"
        if lead else "Встречу никто не ведёт⚠️"
    )
    if helpers:
        head += ", помогают " + ", ".join(_fmt_emp(db_sess, h.employee_id) for h in helpers)
    return head


def _escape_text(txt: str) -> str:
    return html.escape(txt, quote=False)


def build_event_text(db_sess, event) -> str:
    # Импорт внутри функции, чтобы избежать циклов в module init
    from bot.services.mentions import format_organizer_mention

    s_loc = pytz.UTC.localize(event.start_time).astimezone(MOSCOW_TZ)
    e_loc = pytz.UTC.localize(event.end_time).astimezone(MOSCOW_TZ)

    safe_title = _escape_text(event.title or "")

    # Строка с организатором: пишется ВСЕГДА если есть organizer_email
    # (не только при over-capacity). format_organizer_mention сама подберёт
    # формат: "@user (ФИО)" / "ФИО (email)" / email / "(организатор неизвестен)"
    organizer_line = ""
    org_email = getattr(event, "organizer_email", None)
    if org_email:
        mention = format_organizer_mention(db_sess, org_email)
        organizer_line = f"Организатор: {_escape_text(mention)}\n"

    txt = (
        f"Встреча: {safe_title}\n"
        f"Время: {s_loc:%H:%M} - {e_loc:%H:%M}\n"
        f"{organizer_line}"
        f"{_escape_text(build_assignees_text(db_sess, event.id))}"
    )

    # Пометка для встреч сверх лимита (этап 3): добавляется в самое начало
    # чтобы инженер сразу видел при пролистывании ленты
    if getattr(event, "is_over_capacity", False):
        rank = getattr(event, "capacity_rank", 0) or 0
        txt = (
            f"🚨 СВЕРХ ЛИМИТА (#{rank} в пересекающемся интервале)\n"
            f"Саппорт может игнорировать данную встречу — её должен перенести организатор.\n\n"
        ) + txt

    if event.is_day_in_day:
        txt = "‼️‼️ ВНИМАНИЕ! Встреча назначена день в день!\n" + txt
    if not (event.description or "").strip():
        txt += "\nВнимание, нет описания!⚠️"
    if event.is_technical:
        txt += "\n⚠️⚠️⚠️ ВНИМАНИЕ, ЭТО ТЕХ.ВСТРЕЧА!!! ⚠️⚠️⚠️"

    if event.meeting_link:
        href = _clean_href(event.meeting_link)
        if href:
            txt += f'\n<a href="{href}">Присоединиться к встрече</a>'
        else:
            txt += "\nВнимание, ссылка на встречу повреждена⚠️"
    else:
        txt += "\nВнимание, нет ссылки на встречу!!!⚠️"

    if overlaps_support_planning(event.start_time, event.end_time):
        txt += "\n‼️ Встреча пересекается с планёркой тех.поддержки⚠️!"

    return txt


__all__ = [
    "MOSCOW_TZ",
    "SUPPORT_PLANNING_SLOTS",
    "parse_link",
    "normalize_description",
    "detect_if_technical",
    "overlaps_support_planning",
    "is_excluded_event",
    "filter_today_events",
    "build_assignees_text",
    "build_event_text",
]
