"""Inline-кнопки «Взять встречу» / «Отказаться».

Защита от гонки: при изменении состава assignees блокируем строку Event
через SELECT … FOR UPDATE, чтобы два одновременных клика не привели
к появлению двух ведущих.
"""
from __future__ import annotations

import logging
from typing import Optional

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from bot.callback_map import get_raw_event_id
from bot.common_helpers import build_event_text
from bot.db import Database
from bot.models.employees import Employee, ROLE_ENGINEER
from bot.models.event_assignees import EventAssignee
from bot.models.events import Event

logger = logging.getLogger(__name__)
router = Router(name="callbacks")


def _markup_for_event(
    short_id: str,
    force_confirm: bool = False,
    compact: bool = False,
) -> InlineKeyboardMarkup:
    """Кнопки под карточкой встречи или под напоминанием.

    Параметры:
      short_id     — короткий ID события для callback_data
      force_confirm — заменить «Взять» на «⚠ Всё равно взять»
                      (двухэтапное подтверждение для over-capacity)
      compact       — режим для напоминаний (без «Показать повестку»)
    """
    take_text = "⚠ Всё равно взять" if force_confirm else "Взять встречу"
    take_data = f"take:{short_id}:confirm" if force_confirm else f"take:{short_id}"

    if compact:
        # Для напоминаний: только Взять/Отказаться
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=take_text, callback_data=take_data),
            InlineKeyboardButton(text="Отказаться", callback_data=f"decline:{short_id}"),
        ]])

    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Показать повестку", callback_data=f"show_agenda:{short_id}"),
        InlineKeyboardButton(text=take_text, callback_data=take_data),
        InlineKeyboardButton(text="Отказаться", callback_data=f"decline:{short_id}"),
    ]])


def _is_compact_mode(message) -> bool:
    """Определяет, под чем юзер нажал кнопку: под карточкой или под напоминанием.

    Признак: у напоминания НЕТ кнопки «Показать повестку». Если её нет
    в текущей разметке — значит compact-режим.
    """
    if not message or not message.reply_markup:
        return False
    for row in message.reply_markup.inline_keyboard:
        for btn in row:
            if btn.callback_data and btn.callback_data.startswith("show_agenda:"):
                return False
    return True


def _is_engineer(db_sess, tg_user_id: int) -> Optional[Employee]:
    """Возвращает Employee только если у него role=engineer и is_active=True.

    Сейлзы/other не могут брать встречи, даже если они привязали свой TG
    через /start <email>. Защита от случайного нажатия и от привязки
    «не того» человека.

    Уволенные (is_active=False) — тоже не могут.
    """
    emp = db_sess.query(Employee).filter_by(user_id=str(tg_user_id)).first()
    if not emp:
        return None
    if emp.role != ROLE_ENGINEER:
        return None
    if not emp.is_active:
        return None
    return emp


# Сохраняем старое имя для обратной совместимости — если где-то ещё ссылаются
def _is_employee(db_sess, tg_user_id: int) -> Optional[Employee]:
    return _is_engineer(db_sess, tg_user_id)


def _promote_first_if_no_lead(db_sess, event_id: int) -> None:
    """Если у встречи нет ведущего — назначить первого по created_at."""
    links = (
        db_sess.query(EventAssignee)
        .filter_by(event_id=event_id)
        .order_by(EventAssignee.created_at)
        .all()
    )
    if not links:
        return
    if not any(ln.is_lead for ln in links):
        links[0].is_lead = True


async def _safe_edit_text(callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as ex:
        if "message is not modified" not in str(ex):
            logger.warning("edit_text failed: %s", ex)


@router.callback_query(F.data.startswith("take:"))
async def handle_take_meeting(callback: CallbackQuery) -> None:
    """Колбэк «Взять».

    Формат callback_data:
      take:<short_id>          — обычное взятие
      take:<short_id>:confirm  — повторное подтверждение для over-capacity
    """
    parts = (callback.data or "").split(":")
    if len(parts) < 2:
        await callback.answer("Некорректные данные", show_alert=True)
        return
    short_id = parts[1]
    is_confirmed = len(parts) >= 3 and parts[2] == "confirm"

    raw_event_id = get_raw_event_id(short_id)
    if not raw_event_id:
        await callback.answer("Ошибка: неизвестный short_id!", show_alert=True)
        return
    if not callback.from_user:
        await callback.answer("Не удалось определить пользователя", show_alert=True)
        return

    with Database.session() as db:
        # Сначала смотрим, привязан ли вообще TG (любая роль)
        any_emp = db.query(Employee).filter_by(user_id=str(callback.from_user.id)).first()
        if not any_emp:
            await callback.answer(
                "Вы не зарегистрированы в системе.\n"
                "Напишите боту в личку: /start ваша.почта@example.com",
                show_alert=True,
            )
            return
        if not any_emp.is_active:
            await callback.answer(
                "Ваш аккаунт деактивирован (нет в HRBox).\n"
                "Если это ошибка — обратитесь в саппорт-чат.",
                show_alert=True,
            )
            return
        if any_emp.role != ROLE_ENGINEER:
            await callback.answer(
                f"Брать встречи может только саппорт-инженер.\n"
                f"Ваша роль: {any_emp.role}.",
                show_alert=True,
            )
            return
        emp = any_emp

        # Блокируем строку Event на всю транзакцию — защита от гонки
        event = (
            db.query(Event)
            .filter_by(event_id=raw_event_id)
            .with_for_update()
            .first()
        )
        if not event:
            await callback.answer("Встреча не найдена в БД", show_alert=True)
            return

        # Определяем, под чем юзер кликнул — карточка или напоминание
        compact = _is_compact_mode(callback.message)

        # ── Защита от over-capacity (этап 3) ──────────────────────────────
        # Если встреча сверх лимита и пользователь ещё не подтвердил —
        # показываем предупреждение, меняем кнопку, ждём повторный клик.
        if event.is_over_capacity and not is_confirmed:
            rank = event.capacity_rank or 0
            warning = (
                f"⚠ Эта встреча — #{rank} в пересекающемся интервале (лимит 3).\n"
                "Саппорт может игнорировать данную встречу — её должен перенести организатор.\n"
                "Если всё равно хочешь взять — нажми «⚠ Всё равно взять»."
            )
            await callback.answer(warning, show_alert=True)
            # Меняем markup: кнопка Взять → ⚠ Всё равно взять
            try:
                await callback.message.edit_reply_markup(
                    reply_markup=_markup_for_event(
                        short_id, force_confirm=True, compact=compact
                    )
                )
            except TelegramBadRequest as ex:
                if "message is not modified" not in str(ex):
                    logger.warning("edit_reply_markup failed: %s", ex)
            return

        existing = (
            db.query(EventAssignee)
            .filter_by(event_id=event.id, employee_id=emp.id)
            .first()
        )
        if existing:
            answer_text = "Вы уже участвуете!"
        else:
            had_lead = (
                db.query(EventAssignee)
                .filter_by(event_id=event.id, is_lead=True)
                .first()
                is not None
            )
            db.add(EventAssignee(
                event_id=event.id,
                employee_id=emp.id,
                is_lead=not had_lead,
            ))
            answer_text = (
                "Взято (сверх лимита!)" if event.is_over_capacity
                else "Теперь вы участвуете!"
            )

        _promote_first_if_no_lead(db, event.id)
        db.commit()

        text = build_event_text(db, event)

    await callback.answer(answer_text, show_alert=True)
    await _safe_edit_text(callback, text, _markup_for_event(short_id, compact=compact))


@router.callback_query(F.data.startswith("decline:"))
async def handle_decline_meeting(callback: CallbackQuery) -> None:
    short_id = (callback.data or "").split(":", 1)[1]
    raw_event_id = get_raw_event_id(short_id)
    if not raw_event_id:
        await callback.answer("Ошибка: неизвестный short_id", show_alert=True)
        return
    if not callback.from_user:
        await callback.answer("Не удалось определить пользователя", show_alert=True)
        return

    with Database.session() as db:
        any_emp = db.query(Employee).filter_by(user_id=str(callback.from_user.id)).first()
        if not any_emp:
            await callback.answer(
                "Вы не зарегистрированы в системе.\n"
                "Напишите боту в личку: /start ваша.почта@example.com",
                show_alert=True,
            )
            return
        if not any_emp.is_active:
            await callback.answer(
                "Ваш аккаунт деактивирован (нет в HRBox).\n"
                "Если это ошибка — обратитесь в саппорт-чат.",
                show_alert=True,
            )
            return
        if any_emp.role != ROLE_ENGINEER:
            await callback.answer(
                f"Отказываться от встреч может только саппорт-инженер.\n"
                f"Ваша роль: {any_emp.role}.",
                show_alert=True,
            )
            return
        emp = any_emp

        event = (
            db.query(Event)
            .filter_by(event_id=raw_event_id)
            .with_for_update()
            .first()
        )
        if not event:
            await callback.answer("Встреча не найдена", show_alert=True)
            return

        compact = _is_compact_mode(callback.message)

        link_obj = (
            db.query(EventAssignee)
            .filter_by(event_id=event.id, employee_id=emp.id)
            .first()
        )
        if link_obj:
            db.delete(link_obj)
            db.flush()  # чтобы _promote_first_if_no_lead увидел актуальное состояние
            _promote_first_if_no_lead(db, event.id)
            db.commit()
            answer_text = "Вы отказались от встречи⚠️"
        else:
            answer_text = "Вы и так не участвуете!"

        text = build_event_text(db, event)

    await callback.answer(answer_text, show_alert=True)
    await _safe_edit_text(callback, text, _markup_for_event(short_id, compact=compact))
