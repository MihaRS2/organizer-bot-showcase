# bot/main.py
"""Точка входа: инициализация конфига, БД, шедулера и polling."""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

import holidays
import pytz
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from bot.caldav_client import CalDavClient
from bot.callback_map import get_raw_event_id, shorten_and_store_event_id
from bot.common_helpers import (
    MOSCOW_TZ,
    build_event_text,
    detect_if_technical,
    filter_today_events,
    is_excluded_event,
    normalize_description,
    overlaps_support_planning,
    parse_link,
)
from bot.config import BotConfig
from bot.db import Database
from bot.encryption import EncryptionManager
from bot.handlers.callbacks import router as callbacks_router
from bot.handlers.commands import router as commands_router
from bot.models.bot_state import BotState
from bot.models.employees import Employee
from bot.models.event_assignees import EventAssignee
from bot.models.events import Event
from bot.services.capacity import (
    CAPACITY_LIMIT,
    CapacityChange,
    get_violations_for_day,
    recompute_for_today,
)
from bot.services.cleanup import cleanup_old_bot_state
from bot.services.employee_sync import sync_employees as hrbox_sync_employees
from bot.services.mentions import format_organizer_mention
from bot.stats import gather_stats_for_period

# ── Логирование ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=os.getenv("LOGLEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

RU_HOLIDAYS = holidays.RU()


# ── Helpers ──────────────────────────────────────────────────────────────────
def is_non_working_day(dt: datetime) -> bool:
    return dt.weekday() >= 5 or dt.date() in RU_HOLIDAYS


def _naive_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is None else dt.astimezone(pytz.UTC).replace(tzinfo=None)


def _ev_to_dict(ev: Any) -> Dict[str, Any]:
    uid = getattr(ev, "event_id", None) or getattr(ev, "url", None) or str(ev)
    summary = getattr(ev, "summary", "")
    description = getattr(ev, "description", "")
    meeting_link = getattr(ev, "url", None) or parse_link(description)
    return {
        "event_id": uid,
        "title": summary,
        "description": description,
        "start": _naive_utc(ev.start),
        "end": _naive_utc(ev.end),
        "meeting_link": meeting_link,
        "organizer_email": getattr(ev, "organizer_email", None),
    }


def _create_safe_sender(bot: Bot):
    async def safe_send(chat_id: int, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None, **kwargs):
        try:
            return await bot.send_message(chat_id, text, reply_markup=reply_markup, **kwargs)
        except TelegramRetryAfter as e:
            wait = getattr(e, "retry_after", 30)
            logger.warning("Flood control on send, retry after %s", wait)
            await asyncio.sleep(wait)
            return await bot.send_message(chat_id, text, reply_markup=reply_markup, **kwargs)
    return safe_send


def _create_safe_editor(bot: Bot):
    async def safe_edit(chat_id: int, message_id: int, text: str,
                        reply_markup: Optional[InlineKeyboardMarkup] = None):
        try:
            return await bot.edit_message_text(
                text, chat_id=chat_id, message_id=message_id, reply_markup=reply_markup,
            )
        except TelegramBadRequest as e:
            logger.info("Edit failed for msg %s: %s", message_id, e)
            return None
        except TelegramRetryAfter as e:
            wait = getattr(e, "retry_after", 30)
            logger.warning("Flood control on edit, retry after %s", wait)
            await asyncio.sleep(wait)
            return await bot.edit_message_text(
                text, chat_id=chat_id, message_id=message_id, reply_markup=reply_markup,
            )
    return safe_edit


# ── Anti-spam state helpers ──────────────────────────────────────────────────
def _state_get(db, key: str) -> Optional[BotState]:
    try:
        return db.get(BotState, key)
    except Exception:  # noqa: BLE001
        return None


def _state_set(db, key: str, value: str = "1") -> None:
    if not _state_get(db, key):
        db.add(BotState(key=key, value=value))
        db.commit()


def _already_notified_move(db, event_id: str, start_dt, end_dt) -> bool:
    return bool(_state_get(db, f"move_notified:{event_id}:{start_dt.isoformat()}-{end_dt.isoformat()}"))


def _mark_notified_move(db, event_id: str, start_dt, end_dt) -> None:
    _state_set(db, f"move_notified:{event_id}:{start_dt.isoformat()}-{end_dt.isoformat()}", "1")


def _already_notified_cancel(db, event_id: str, new_date_iso: str) -> bool:
    return bool(_state_get(db, f"cancel_notified:{event_id}:{new_date_iso}"))


def _mark_notified_cancel(db, event_id: str, new_date_iso: str) -> None:
    _state_set(db, f"cancel_notified:{event_id}:{new_date_iso}", "1")


# ── Карточка встречи: отправка/редактирование ────────────────────────────────
def _event_markup(short_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Показать повестку", callback_data=f"show_agenda:{short_id}"),
        InlineKeyboardButton(text="Взять встречу", callback_data=f"take:{short_id}"),
        InlineKeyboardButton(text="Отказаться", callback_data=f"decline:{short_id}"),
    ]])


def _reminder_markup(short_id: str) -> InlineKeyboardMarkup:
    """Кнопки для напоминаний — короче чем у карточки (нет «Показать повестку»).

    Поведение take/decline идентично — переиспользуется тот же хэндлер,
    включая 2-кликовое подтверждение для over-capacity встреч.
    """
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Взять встречу", callback_data=f"take:{short_id}"),
        InlineKeyboardButton(text="Отказаться", callback_data=f"decline:{short_id}"),
    ]])


async def _send_event_card_and_remember(safe_send, db, ev_obj: Event, text: str, markup: InlineKeyboardMarkup):
    msg = await safe_send(BotConfig.SALES_CHAT_ID, text, reply_markup=markup)
    ev_obj.last_message_id = msg.message_id
    ev_obj.last_message_chat_id = BotConfig.SALES_CHAT_ID
    db.commit()
    return msg


async def _edit_event_card(safe_edit, ev_obj: Event, db) -> None:
    if not ev_obj.last_message_id or ev_obj.last_message_chat_id != BotConfig.SALES_CHAT_ID:
        return
    text = build_event_text(db, ev_obj)
    if text.startswith("‼️‼️"):
        text = text.split("\n", 1)[1]
    if overlaps_support_planning(ev_obj.start_time, ev_obj.end_time):
        text += "\n‼️ Встреча пересекается с планёркой тех.поддержки⚠️!"
    short = shorten_and_store_event_id(ev_obj.event_id)
    await safe_edit(BotConfig.SALES_CHAT_ID, ev_obj.last_message_id, text, reply_markup=_event_markup(short))


def __pick_move_candidate(
    db,
    title: str,
    link: Optional[str],
    local_start: datetime,
    day_start_local: datetime,
    day_end_local: datetime,
) -> Optional[Event]:
    """
    Подбираем старую запись, если провайдер выдал новый UID:
      1) по meeting_link;
      2) иначе по title в рамках сегодняшних суток (МСК) — берём ближайшее к local_start,
         только если |Δt| <= 12 часов.
    """
    qbase = db.query(Event).filter(Event.was_canceled.is_(False))

    if link:
        cand = qbase.filter(Event.meeting_link == link).order_by(Event.start_time.asc()).first()
        if cand:
            return cand

    start_utc = day_start_local.astimezone(pytz.UTC).replace(tzinfo=None)
    end_utc = day_end_local.astimezone(pytz.UTC).replace(tzinfo=None)
    same_title = (
        qbase.filter(Event.title == title)
        .filter(Event.start_time >= start_utc)
        .filter(Event.start_time < end_utc)
        .all()
    )
    if not same_title:
        return None

    def _abs_seconds(ev: Event) -> float:
        return abs((pytz.UTC.localize(ev.start_time).astimezone(MOSCOW_TZ) - local_start).total_seconds())

    best = min(same_title, key=_abs_seconds)
    return best if _abs_seconds(best) <= 12 * 3600 else None


# ── Глобальный CalDAV клиент (инициализируется в main()) ─────────────────────
CALDAV_CLIENT: Optional[CalDavClient] = None


# ── 1. Утренний дайджест ─────────────────────────────────────────────────────
async def morning_today_events(bot: Bot) -> None:
    assert CALDAV_CLIENT is not None
    now = datetime.now(MOSCOW_TZ)
    if is_non_working_day(now):
        return

    state_key = f"morning_digest_sent_{now:%Y-%m-%d}"
    with Database.session() as db:
        if db.get(BotState, state_key):
            logger.info("Morning digest already sent (state=%s)", state_key)
            return

    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)

    raw_events = [
        _ev_to_dict(ev)
        for ev in await CALDAV_CLIENT.get_upcoming_events(day_start, day_end)
        if not is_excluded_event(ev.summary)
    ]
    events = sorted(filter_today_events(raw_events, now), key=lambda x: x["start"])

    safe_send = _create_safe_sender(bot)

    # ── Удаление «призраков» — встреч в БД на сегодня, которых нет в CalDAV ─
    # Бывает что встречу удалили в календаре ночью, когда check_for_updates
    # не работал — она остаётся в БД с was_canceled=false. Утренний дайджест
    # должен сначала отметить её отменённой, чтобы capacity_rank посчитался
    # корректно.
    raw_ids_today = {e["event_id"] for e in raw_events}
    day_start_msk = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end_msk = day_start_msk + timedelta(days=1)
    day_start_utc = day_start_msk.astimezone(pytz.UTC).replace(tzinfo=None)
    day_end_utc = day_end_msk.astimezone(pytz.UTC).replace(tzinfo=None)
    with Database.session() as db:
        ghosts = (
            db.query(Event)
            .filter(Event.was_canceled.is_(False))
            .filter(Event.start_time >= day_start_utc, Event.start_time < day_end_utc)
            .all()
        )
        for ghost in ghosts:
            if ghost.event_id not in raw_ids_today:
                logger.info("Marking ghost as canceled: %s", ghost.title)
                ghost.was_canceled = True
        db.commit()

    if not events:
        await safe_send(BotConfig.SALES_CHAT_ID, "Доброе утро!\nНа сегодня встреч нет.")
    else:
        await safe_send(BotConfig.SALES_CHAT_ID, f"Доброе утро!\nНа сегодня назначено {len(events)} встреч.")

        cutoff = now.replace(hour=7, minute=25, second=0, microsecond=0)
        # ── Шаг 1: upsert всех событий в БД (без рассылки карточек) ─────
        event_ids_in_order: list[int] = []
        with Database.session() as db:
            for ev in events:
                local_start = pytz.UTC.localize(ev["start"]).astimezone(MOSCOW_TZ)
                desc_norm = normalize_description(ev["description"])
                link = (ev["meeting_link"] or "").strip()

                obj = db.query(Event).filter_by(event_id=ev["event_id"]).first()
                if obj:
                    if obj.discovered_date != local_start.date():
                        db.query(EventAssignee).filter_by(event_id=obj.id).delete()
                        obj.is_taken = obj.was_canceled = False
                        obj.discovered_date = local_start.date()
                        obj.is_day_in_day = False

                    obj.title = ev["title"]
                    obj.start_time = ev["start"]
                    obj.end_time = ev["end"]
                    obj.description = desc_norm
                    obj.meeting_link = link
                    obj.is_technical = detect_if_technical(ev["title"])
                    obj.organizer_email = ev.get("organizer_email")
                    if obj.discovered_date == now.date() and now >= cutoff:
                        obj.is_day_in_day = True
                    db.commit()
                    cur_event = obj
                else:
                    cur_event = Event(
                        event_id=ev["event_id"],
                        title=ev["title"],
                        description=desc_norm,
                        start_time=ev["start"],
                        end_time=ev["end"],
                        is_technical=detect_if_technical(ev["title"]),
                        meeting_link=link,
                        discovered_date=local_start.date(),
                        is_day_in_day=now >= cutoff and local_start.date() == now.date(),
                        organizer_email=ev.get("organizer_email"),
                    )
                    db.add(cur_event)
                    db.commit()

                event_ids_in_order.append(cur_event.id)

            # ── Шаг 2: пересчёт capacity ДО рассылки карточек ───────────
            # Это гарантирует что флаг is_over_capacity уже выставлен,
            # когда build_event_text формирует текст карточки.
            recompute_for_today(db)
            db.commit()

        # ── Шаг 3: рассылка карточек с актуальным флагом over-capacity ─
        with Database.session() as db:
            for ev_dict, event_db_id in zip(events, event_ids_in_order):
                cur_event = db.get(Event, event_db_id)
                if not cur_event:
                    continue
                short = shorten_and_store_event_id(ev_dict["event_id"])
                text = build_event_text(db, cur_event)
                if text.startswith("‼️‼️"):
                    text = text.split("\n", 1)[1]
                if overlaps_support_planning(ev_dict["start"], ev_dict["end"]):
                    text += "\n‼️ Встреча пересекается с планёркой тех.поддержки⚠️!"
                await _send_event_card_and_remember(safe_send, db, cur_event, text, _event_markup(short))
                await asyncio.sleep(0.4)

        # ── Шаг 4: блок «🚨 Нарушения» (отдельные сообщения как reply) ──
        with Database.session() as db:
            day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            day_end = day_start + timedelta(days=1)
            violations = get_violations_for_day(db, day_start, day_end)
            if violations:
                # Сначала — общий заголовок
                await safe_send(
                    BotConfig.SALES_CHAT_ID,
                    f"🚨 <b>Нарушения лимита {CAPACITY_LIMIT} встреч на сегодня</b> "
                    f"({len(violations)} шт.):\n"
                    "Эти встречи поставлены сверх лимита — саппорт может игнорировать их. "
                    "Просьба перенести."
                )
                # Затем — по одному сообщению на нарушение, как reply на основную
                # карточку. Это даёт кликабельную цитату → автоскролл к карточке.
                for v in violations:
                    s_loc = pytz.UTC.localize(v.start_time).astimezone(MOSCOW_TZ)
                    e_loc = pytz.UTC.localize(v.end_time).astimezone(MOSCOW_TZ)
                    mention = format_organizer_mention(db, v.organizer_email)
                    txt = (
                        f"🚨 {s_loc:%H:%M}–{e_loc:%H:%M} «{v.title}» "
                        f"(#{v.capacity_rank})\n"
                        f"Организатор: {mention}"
                    )
                    reply_kw = {}
                    if v.last_message_chat_id == BotConfig.SALES_CHAT_ID and v.last_message_id:
                        reply_kw = {
                            "reply_to_message_id": v.last_message_id,
                            "allow_sending_without_reply": True,
                        }
                    await safe_send(BotConfig.SALES_CHAT_ID, txt, **reply_kw)
                    await asyncio.sleep(0.3)

    with Database.session() as db:
        if not db.get(BotState, state_key):
            db.add(BotState(key=state_key, value="1"))
            db.commit()


# ── 2. Периодический монитор ─────────────────────────────────────────────────
async def check_for_updates(bot: Bot) -> None:
    assert CALDAV_CLIENT is not None
    now = datetime.now(MOSCOW_TZ)
    logger.info("[check_for_updates] tick at %s", now.strftime("%Y-%m-%d %H:%M:%S %Z"))
    if now.hour < 7 or is_non_working_day(now):
        logger.info("[check_for_updates] skipped (before 07:00 or non-working day)")
        return

    scheduled = now.replace(hour=BotConfig.MORNING_DIGEST_HOUR, minute=BotConfig.MORNING_DIGEST_MIN,
                            second=0, microsecond=0)
    state_key = f"morning_digest_sent_{now:%Y-%m-%d}"

    with Database.session() as db:
        digest_sent = bool(db.get(BotState, state_key))

    suppress_new_event_notices = (now < scheduled) and (not digest_sent)

    day_start_local = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end_local = day_start_local + timedelta(days=1)

    try:
        caldav_events = await CALDAV_CLIENT.get_upcoming_events(day_start_local, day_end_local)
        raw_events = [_ev_to_dict(ev) for ev in caldav_events if not is_excluded_event(ev.summary)]
    except Exception as e:
        logger.exception("[check_for_updates] caldav fetch failed: %s", e)
        return

    raw_ids = {e["event_id"] for e in raw_events}
    events_today = sorted(filter_today_events(raw_events, now), key=lambda x: x["start"])

    safe_send = _create_safe_sender(bot)
    safe_edit = _create_safe_editor(bot)

    with Database.session() as db:
        for ev in events_today:
            desc_norm = normalize_description(ev["description"])
            link = (ev["meeting_link"] or "").strip()
            local_start = pytz.UTC.localize(ev["start"]).astimezone(MOSCOW_TZ)

            obj = db.query(Event).filter_by(event_id=ev["event_id"]).first()
            if obj:
                def moved(a, b) -> bool:
                    return abs((a - b).total_seconds()) > 60

                old_start, old_end = obj.start_time, obj.end_time
                start_moved = moved(old_start, ev["start"])
                end_moved = moved(old_end, ev["end"])
                day_moved = obj.discovered_date != local_start.date()

                if day_moved:
                    db.query(EventAssignee).filter_by(event_id=obj.id).delete()
                    obj.is_taken = obj.was_canceled = False
                    obj.discovered_date = local_start.date()
                    obj.is_day_in_day = False

                obj.title = ev["title"]
                obj.description = desc_norm
                obj.start_time = ev["start"]
                obj.end_time = ev["end"]
                obj.meeting_link = link
                obj.is_technical = detect_if_technical(ev["title"])
                obj.organizer_email = ev.get("organizer_email")
                db.commit()

                if start_moved or end_moved or day_moved:
                    obj.moved_count = (obj.moved_count or 0) + 1
                    db.commit()

                    same_day = (local_start.date() == now.date()) and (not day_moved)
                    if not same_day:
                        cancel_key_date = local_start.date().isoformat()
                        if not _already_notified_cancel(db, obj.event_id, cancel_key_date):
                            reply_kw = {}
                            if obj.last_message_chat_id == BotConfig.SALES_CHAT_ID and obj.last_message_id:
                                reply_kw = {
                                    "reply_to_message_id": obj.last_message_id,
                                    "allow_sending_without_reply": True,
                                }
                            await safe_send(
                                BotConfig.SALES_CHAT_ID,
                                "Встреча отменена (перенесена на другую дату)⚠️",
                                **reply_kw,
                            )
                            _mark_notified_cancel(db, obj.event_id, cancel_key_date)
                        await _edit_event_card(safe_edit, obj, db)
                        await asyncio.sleep(0.4)
                        continue
                    else:
                        if _already_notified_move(db, obj.event_id, ev["start"], ev["end"]):
                            await _edit_event_card(safe_edit, obj, db)
                            await asyncio.sleep(0.2)
                            continue

                    so = pytz.UTC.localize(old_start).astimezone(MOSCOW_TZ).strftime("%H:%M")
                    eo = pytz.UTC.localize(old_end).astimezone(MOSCOW_TZ).strftime("%H:%M")
                    sn = local_start.strftime("%H:%M")
                    en = pytz.UTC.localize(ev["end"]).astimezone(MOSCOW_TZ).strftime("%H:%M")
                    moved_msg = f"Встреча перенесена!⚠️\nСтарое время {so}-{eo}, новое {sn}-{en}"

                    reply_kw = {}
                    if obj.last_message_chat_id == BotConfig.SALES_CHAT_ID and obj.last_message_id:
                        reply_kw = {
                            "reply_to_message_id": obj.last_message_id,
                            "allow_sending_without_reply": True,
                        }
                    await safe_send(BotConfig.SALES_CHAT_ID, moved_msg, **reply_kw)
                    _mark_notified_move(db, obj.event_id, ev["start"], ev["end"])
                    await _edit_event_card(safe_edit, obj, db)
                    await asyncio.sleep(0.4)
            else:
                cand = __pick_move_candidate(db, ev["title"], link, local_start, day_start_local, day_end_local)
                if cand:
                    old_start, old_end = cand.start_time, cand.end_time

                    cand.event_id = ev["event_id"]
                    cand.title = ev["title"]
                    cand.description = desc_norm
                    cand.start_time = ev["start"]
                    cand.end_time = ev["end"]
                    cand.meeting_link = link
                    cand.is_technical = detect_if_technical(ev["title"])
                    cand.organizer_email = ev.get("organizer_email")
                    cand.moved_count = (cand.moved_count or 0) + 1
                    db.commit()

                    same_day = (local_start.date() == now.date())
                    if not same_day:
                        cancel_key_date = local_start.date().isoformat()
                        if not _already_notified_cancel(db, cand.event_id, cancel_key_date):
                            reply_kw = {}
                            if cand.last_message_chat_id == BotConfig.SALES_CHAT_ID and cand.last_message_id:
                                reply_kw = {
                                    "reply_to_message_id": cand.last_message_id,
                                    "allow_sending_without_reply": True,
                                }
                            await safe_send(
                                BotConfig.SALES_CHAT_ID,
                                "Встреча отменена (перенесена на другую дату)⚠️",
                                **reply_kw,
                            )
                            _mark_notified_cancel(db, cand.event_id, cancel_key_date)
                        await _edit_event_card(safe_edit, cand, db)
                        await asyncio.sleep(0.4)
                        continue
                    else:
                        if _already_notified_move(db, cand.event_id, ev["start"], ev["end"]):
                            await _edit_event_card(safe_edit, cand, db)
                            await asyncio.sleep(0.2)
                            continue

                    so = pytz.UTC.localize(old_start).astimezone(MOSCOW_TZ).strftime("%H:%M")
                    eo = pytz.UTC.localize(old_end).astimezone(MOSCOW_TZ).strftime("%H:%M")
                    sn = local_start.strftime("%H:%M")
                    en = pytz.UTC.localize(ev["end"]).astimezone(MOSCOW_TZ).strftime("%H:%M")
                    moved_msg = f"Встреча перенесена!⚠️\nСтарое время {so}-{eo}, новое {sn}-{en}"

                    reply_kw = {}
                    if cand.last_message_chat_id == BotConfig.SALES_CHAT_ID and cand.last_message_id:
                        reply_kw = {
                            "reply_to_message_id": cand.last_message_id,
                            "allow_sending_without_reply": True,
                        }
                    await safe_send(BotConfig.SALES_CHAT_ID, moved_msg, **reply_kw)
                    _mark_notified_move(db, cand.event_id, ev["start"], ev["end"])
                    await _edit_event_card(safe_edit, cand, db)
                    await asyncio.sleep(0.4)
                else:
                    # действительно новая встреча
                    new_ev = Event(
                        event_id=ev["event_id"],
                        title=ev["title"],
                        description=desc_norm,
                        start_time=ev["start"],
                        end_time=ev["end"],
                        is_technical=detect_if_technical(ev["title"]),
                        meeting_link=link,
                        discovered_date=local_start.date(),
                        organizer_email=ev.get("organizer_email"),
                    )
                    if (
                        local_start.date() == now.date()
                        and now >= now.replace(hour=7, minute=25, second=0, microsecond=0)
                    ):
                        new_ev.is_day_in_day = True
                    db.add(new_ev)
                    db.commit()

                    if not suppress_new_event_notices:
                        short = shorten_and_store_event_id(ev["event_id"])
                        text = build_event_text(db, new_ev)
                        if overlaps_support_planning(ev["start"], ev["end"]):
                            text += "\n‼️ Встреча пересекается с планёркой тех.поддержки⚠️!"
                        await _send_event_card_and_remember(safe_send, db, new_ev, text, _event_markup(short))
                        await asyncio.sleep(0.4)
                    else:
                        logger.info("New event before digest; notification suppressed: %s", ev["title"])

        # отменённые (сегодня), которых нет в raw_ids
        for obj in db.query(Event).filter(Event.was_canceled.is_(False)):
            start_local = pytz.UTC.localize(obj.start_time).astimezone(MOSCOW_TZ)
            if start_local.date() == now.date() and obj.event_id not in raw_ids:
                await safe_send(BotConfig.SALES_CHAT_ID, f"Встреча отменена⚠️:\n{obj.title}")
                obj.was_canceled = True
                db.commit()

        # ── Пересчёт capacity + мгновенные алерты на новые нарушения ──────
        changes = recompute_for_today(db)
        db.commit()

        for ch in changes:
            if not ch.became_over_capacity:
                continue
            s_loc = pytz.UTC.localize(ch.start_time_utc).astimezone(MOSCOW_TZ)
            e_loc = pytz.UTC.localize(ch.end_time_utc).astimezone(MOSCOW_TZ)
            mention = format_organizer_mention(db, ch.organizer_email)
            alert = (
                f"🚨 <b>Превышен лимит {CAPACITY_LIMIT} встреч одновременно</b>\n"
                f"Встреча #{ch.new_rank}: «{ch.title}»\n"
                f"Время: {s_loc:%H:%M}–{e_loc:%H:%M}\n"
                f"Организатор: {mention}\n"
                f"Просьба перенести — саппорт может игнорировать данную встречу."
            )
            # Reply на основную карточку встречи (если она в SALES-чате)
            # — это даст кликабельную цитату для перехода
            reply_kw = {}
            ev_obj = db.query(Event).filter_by(id=ch.event_id).first()
            if (
                ev_obj
                and ev_obj.last_message_chat_id == BotConfig.SALES_CHAT_ID
                and ev_obj.last_message_id
            ):
                reply_kw = {
                    "reply_to_message_id": ev_obj.last_message_id,
                    "allow_sending_without_reply": True,
                }
            await safe_send(BotConfig.SALES_CHAT_ID, alert, **reply_kw)
            await asyncio.sleep(0.3)

            # И перерисовываем основную карточку — теперь с пометкой
            # «🚨 СВЕРХ ЛИМИТА» сразу в тексте, чтобы инженер видел её
            # при пролистывании ленты, не только в отдельном алерте.
            if ev_obj:
                await _edit_event_card(safe_edit, ev_obj, db)
                await asyncio.sleep(0.2)

        logger.info("[check_for_updates] done")


# ── 3. show_agenda ───────────────────────────────────────────────────────────
async def callback_show_agenda(callback: CallbackQuery) -> None:
    short = (callback.data or "").split(":", 1)[1]
    raw_id = get_raw_event_id(short)
    if not raw_id:
        await callback.answer("Повестка не найдена!", show_alert=True)
        return

    with Database.session() as db:
        ev = db.query(Event).filter_by(event_id=raw_id).first()
        if not ev or not (ev.description or "").strip():
            await callback.answer("Описание пустое!", show_alert=True)
            return
        desc = " ".join(ev.description.split())

    # Telegram callback.answer text — макс. 200 символов
    await callback.answer(desc if len(desc) <= 200 else desc[:200] + "…", show_alert=True)


# ── 4. 10-минутное напоминание ───────────────────────────────────────────────
async def remind_unassigned_10m(bot: Bot) -> None:
    now = datetime.now(MOSCOW_TZ)
    if is_non_working_day(now):
        return

    wnd_start_utc = (now + timedelta(minutes=10)).astimezone(pytz.UTC).replace(tzinfo=None)
    wnd_end_utc = wnd_start_utc + timedelta(minutes=1)

    safe_send = _create_safe_sender(bot)
    with Database.session() as db:
        q = (
            db.query(Event)
            .filter(Event.was_canceled.is_(False))
            .filter(Event.start_time >= wnd_start_utc, Event.start_time < wnd_end_utc)
            .all()
        )

        for ev in q:
            if ev.ten_min_alert_sent:
                continue

            links = (
                db.query(EventAssignee)
                .filter_by(event_id=ev.id)
                .order_by(EventAssignee.created_at)
                .all()
            )
            lead_link = next((ln for ln in links if ln.is_lead), None)
            start_local = pytz.UTC.localize(ev.start_time).astimezone(MOSCOW_TZ)

            reply_kw = {}
            if ev.last_message_chat_id == BotConfig.SALES_CHAT_ID and ev.last_message_id:
                reply_kw = {
                    "reply_to_message_id": ev.last_message_id,
                    "allow_sending_without_reply": True,
                }

            # Кнопки для self-take прямо из напоминания
            short = shorten_and_store_event_id(ev.event_id)
            markup = _reminder_markup(short)

            over_cap_note = ""
            if ev.is_over_capacity:
                over_cap_note = (
                    f"\n🚨 Эта встреча — #{ev.capacity_rank} в пересекающемся "
                    f"интервале (сверх лимита {CAPACITY_LIMIT}). "
                    "Саппорт может игнорировать данную встречу!"
                )

            # Строка с организатором (если есть)
            organizer_note = ""
            if ev.organizer_email:
                mention = format_organizer_mention(db, ev.organizer_email)
                organizer_note = f"\nОрганизатор: {mention}"

            if lead_link:
                lead_emp = db.get(Employee, lead_link.employee_id)
                mention = (
                    f"@{lead_emp.username}"
                    if (lead_emp and lead_emp.username) else "ведущий инженер"
                )
                txt = (
                    f"Напоминание: через 10 мин ({start_local:%H:%M}) встреча «{ev.title}»!\n"
                    f"{mention}, пожалуйста, подключайтесь вовремя."
                    f"{organizer_note}"
                    f"{over_cap_note}"
                )
                # Если есть лид — кнопки не критичны, но оставим (вдруг кто-то ещё захочет взять)
                await safe_send(BotConfig.SALES_CHAT_ID, txt, reply_markup=markup, **reply_kw)
            else:
                txt = (
                    "⚠️⚠️⚠️ ВНИМАНИЕ!\n"
                    f"Встречу «{ev.title}» не взял ни один инженер, "
                    f"начало ВКС через 10 мин ({start_local:%H:%M})!"
                    f"{organizer_note}"
                    f"{over_cap_note}"
                )
                await safe_send(BotConfig.SALES_CHAT_ID, txt, reply_markup=markup, **reply_kw)

            ev.ten_min_alert_sent = True
            db.commit()


# ── 5. Напоминание 10:50 (без взятых) ────────────────────────────────────────
async def reminder_unassigned_1050(bot: Bot) -> None:
    now = datetime.now(MOSCOW_TZ)
    if is_non_working_day(now):
        return

    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)

    safe_send = _create_safe_sender(bot)
    with Database.session() as db:
        unassigned: list[Event] = []
        for ev in db.query(Event).filter(Event.was_canceled.is_(False)):
            local_start = pytz.UTC.localize(ev.start_time).astimezone(MOSCOW_TZ)
            if day_start <= local_start < day_end:
                links = db.query(EventAssignee).filter_by(event_id=ev.id).all()
                if not any(ln.is_lead for ln in links):
                    unassigned.append(ev)

        if unassigned:
            await safe_send(
                BotConfig.SALES_CHAT_ID,
                "⚠️⚠️⚠️ Есть встречи без ведущего инженера — нажмите «Взять встречу» "
                "под нужной карточкой, или непосредственно тут под напоминанием.",
            )
            for ev in sorted(unassigned, key=lambda x: x.start_time):
                s = pytz.UTC.localize(ev.start_time).astimezone(MOSCOW_TZ).strftime("%H:%M")
                e = pytz.UTC.localize(ev.end_time).astimezone(MOSCOW_TZ).strftime("%H:%M")
                over = " 🚨 СВЕРХ ЛИМИТА" if ev.is_over_capacity else ""
                organizer_note = ""
                if ev.organizer_email:
                    mention = format_organizer_mention(db, ev.organizer_email)
                    organizer_note = f"\n   Организатор: {mention}"
                txt = f"• {ev.title} ({s}-{e}){over}{organizer_note}"
                reply_kw = {}
                if ev.last_message_chat_id == BotConfig.SALES_CHAT_ID and ev.last_message_id:
                    reply_kw = {
                        "reply_to_message_id": ev.last_message_id,
                        "allow_sending_without_reply": True,
                    }
                short = shorten_and_store_event_id(ev.event_id)
                await safe_send(
                    BotConfig.SALES_CHAT_ID,
                    txt,
                    reply_markup=_reminder_markup(short),
                    **reply_kw,
                )


# ── 6. Ежемесячная статистика ────────────────────────────────────────────────
async def monthly_stats(bot: Bot) -> None:
    now = datetime.now(MOSCOW_TZ)
    with Database.session() as db:
        start_msk = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        stats = gather_stats_for_period(db, start_msk, now)

    header = (
        f"📊 Ежемесячная статистика по встречам\n"
        f"(с {start_msk:%d.%m.%Y %H:%M} по {now:%d.%m.%Y %H:%M}):\n\n"
    )
    await bot.send_message(BotConfig.SUPPORT_CHAT_ID, header + stats)


# ── 7. Backup БД ────────────────────────────────────────────────────────────
async def backup_database() -> None:
    now = datetime.now(MOSCOW_TZ)
    fname = f"/backup/mydb_{now:%Y%m%d_%H%M%S}.sql"
    os.makedirs("/backup", exist_ok=True)
    cmd = [
        "pg_dump",
        "-h", BotConfig.DB_HOST,
        "-p", BotConfig.DB_PORT,
        "-U", BotConfig.DB_USER,
        "-d", BotConfig.DB_NAME,
    ]
    # PGPASSWORD передаётся через env, чтобы pg_dump не интерактивно спрашивал
    env = {**os.environ, "PGPASSWORD": BotConfig.DB_PASSWORD}
    try:
        with open(fname, "w", encoding="utf-8") as f:
            subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE, text=True, check=True, env=env)
        logger.info("DB backup saved: %s", fname)
    except Exception as exc:  # noqa: BLE001
        logger.exception("DB backup failed: %s", exc)


# ── 8. Cleanup старого BotState ─────────────────────────────────────────────
async def cleanup_state() -> None:
    try:
        cleanup_old_bot_state(retention_days=30)
    except Exception:  # noqa: BLE001
        logger.exception("cleanup_old_bot_state failed")


# ── 9. HRBox sync (этап 2) ──────────────────────────────────────────────────
async def hrbox_sync_safe() -> None:
    """Безопасная обёртка для cron — никогда не падает, только логирует."""
    try:
        result = await hrbox_sync_employees()
        logger.info("HRBox sync: %s", result.summary().replace("\n", " | "))
    except Exception:  # noqa: BLE001
        logger.exception("HRBox sync failed unexpectedly")


# ── MAIN ─────────────────────────────────────────────────────────────────────
async def main() -> None:
    BotConfig.validate()
    BotConfig.log_safe_summary()

    # Telegram token: либо plain, либо расшифрованный
    telegram_token = (
        BotConfig.BOT_TOKEN
        if BotConfig.BOT_TOKEN
        else EncryptionManager.decrypt_value(BotConfig.ENCRYPTION_KEY, BotConfig.BOT_TOKEN_ENCRYPTED)
    )

    global CALDAV_CLIENT
    CALDAV_CLIENT = CalDavClient(
        username=BotConfig.CALDAV_USERNAME,
        password=BotConfig.CALDAV_PASSWORD,
        calendar_url=BotConfig.CALDAV_CALENDAR_URL,
    )

    Database.init()
    logger.info("Bot starting…")

    # aiogram 3.7+ : parse_mode выставляется через DefaultBotProperties
    bot = Bot(
        token=telegram_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(commands_router)
    dp.include_router(callbacks_router)
    dp.callback_query.register(callback_show_agenda, F.data.startswith("show_agenda:"))

    sched = AsyncIOScheduler(timezone=MOSCOW_TZ)

    md_hour = BotConfig.MORNING_DIGEST_HOUR
    md_min = BotConfig.MORNING_DIGEST_MIN
    digest_trigger = CronTrigger(hour=md_hour, minute=md_min, timezone=MOSCOW_TZ)
    sched.add_job(
        morning_today_events, digest_trigger, args=[bot],
        misfire_grace_time=3600, coalesce=True, max_instances=1,
        replace_existing=True, id="morning_digest",
    )
    sched.add_job(
        check_for_updates, "interval", minutes=BotConfig.CHECK_INTERVAL_MINUTES, args=[bot],
        max_instances=1, coalesce=True, misfire_grace_time=600, jitter=20,
        replace_existing=True, id="check_for_updates",
    )
    sched.add_job(
        reminder_unassigned_1050, "cron", hour=10, minute=50, args=[bot],
        id="reminder_1050",
    )
    sched.add_job(
        remind_unassigned_10m, "interval", minutes=1, args=[bot],
        id="remind_10m",
    )
    sched.add_job(backup_database, "cron", hour=3, minute=0, id="backup_db")
    sched.add_job(
        monthly_stats, "cron", day="last", hour=BotConfig.DAILY_NOTIFICATION_HOUR, minute=0,
        args=[bot], id="monthly_stats",
    )
    sched.add_job(cleanup_state, "cron", hour=4, minute=0, id="cleanup_state")

    # HRBox sync — только если заданы credentials
    hrbox_enabled = bool(BotConfig.HRBOX_CLIENT_ID and BotConfig.HRBOX_CLIENT_SECRET)
    if hrbox_enabled:
        sched.add_job(
            hrbox_sync_safe, "interval",
            minutes=BotConfig.HRBOX_SYNC_INTERVAL_MIN,
            max_instances=1, coalesce=True, misfire_grace_time=300,
            replace_existing=True, id="hrbox_sync",
        )
        logger.info("HRBox sync scheduled every %d min", BotConfig.HRBOX_SYNC_INTERVAL_MIN)
    else:
        logger.info("HRBox sync disabled (HRBOX_CLIENT_ID/SECRET not set)")

    sched.start()
    logger.info("Scheduler started; morning digest at %02d:%02d MSK", md_hour, md_min)

    # Стартовый HRBox-синк в фоне (не блокируем запуск polling)
    if hrbox_enabled:
        asyncio.create_task(hrbox_sync_safe())

    # Catch-up утреннего дайджеста
    now = datetime.now(MOSCOW_TZ)
    scheduled = now.replace(hour=md_hour, minute=md_min, second=0, microsecond=0)
    with Database.session() as db:
        sent = db.get(BotState, f"morning_digest_sent_{now:%Y-%m-%d}")
    if now >= scheduled and not sent:
        logger.info("Catch-up: запускаю утренний дайджест немедленно")
        await morning_today_events(bot)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
