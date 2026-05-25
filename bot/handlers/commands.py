"""Команды и кнопочное меню статистики + управление сотрудниками."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional, Tuple

import pytz
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, FSInputFile, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.ai_reports import build_df_for_period, make_basic_charts
from bot.config import BotConfig
from bot.db import Database
from bot.handlers.callbacks import _promote_first_if_no_lead
from bot.models.employees import (
    Employee,
    ROLE_ENGINEER,
    ROLE_OTHER,
    ROLE_SALES,
    VALID_ROLES,
)
from bot.models.event_assignees import EventAssignee
from bot.models.events import Event
from bot.services.employee_sync import sync_employees
from bot.stats import gather_stats_for_period

logger = logging.getLogger(__name__)
router = Router(name="commands")

MOSCOW_TZ = pytz.timezone("Europe/Moscow")


# ── auth helpers ─────────────────────────────────────────────────────────────
def _is_employee_tg_id(tg_id: Optional[int]) -> bool:
    if tg_id is None:
        return False
    with Database.session() as db:
        return db.query(Employee).filter_by(user_id=str(tg_id)).first() is not None


def _is_employee_msg(message: Message) -> bool:
    return bool(message.from_user) and _is_employee_tg_id(message.from_user.id)


def _is_employee_cb(callback: CallbackQuery) -> bool:
    return bool(callback.from_user) and _is_employee_tg_id(callback.from_user.id)


def _only_support_chat(message: Message) -> bool:
    try:
        return bool(message.chat) and message.chat.id == BotConfig.SUPPORT_CHAT_ID
    except Exception:  # noqa: BLE001
        return False


# ── /start /whoami — self-link через email ──────────────────────────────────
async def _self_link_by_email(message: Message, email_raw: str) -> None:
    """Привязывает текущего TG-пользователя к HRBox-записи по email."""
    if not message.from_user:
        await message.answer("Не удалось определить пользователя Telegram.")
        return

    email = email_raw.lower().strip()
    if "@" not in email or "." not in email:
        await message.answer(
            "Это не похоже на email. Используй формат:\n"
            "<code>/start your.name@example.com</code>"
        )
        return

    tg_id = str(message.from_user.id)
    tg_username = message.from_user.username

    try:
        await _self_link_by_email_impl(message, email, tg_id, tg_username)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "self_link failed: tg_id=%s email=%s err=%s",
            tg_id, email, exc,
        )
        await message.answer(
            "⚠ Не удалось выполнить привязку.\n"
            "Скажи в саппорт-чате — мы посмотрим логи."
        )


async def _self_link_by_email_impl(
    message: Message, email: str, tg_id: str, tg_username: Optional[str]
) -> None:
    """Сам мердж — выделен из _self_link_by_email для оборачивания в try."""

    with Database.session() as db:
        # 1) Уже зарегистрирован по TG-id?
        by_tg = db.query(Employee).filter_by(user_id=tg_id).first()
        if by_tg:
            # Проверка: уволенный — отказываем
            if not by_tg.is_active:
                await message.answer(
                    "⚠ Твой аккаунт деактивирован (нет в HRBox).\n"
                    "Если это ошибка — обратись в саппорт-чат."
                )
                return

            # Одноразовая привязка: если уже привязан к email — отказываем
            if by_tg.email:
                await message.answer(
                    f"⚠ Ты уже привязан к {by_tg.email}.\n"
                    f"Роль: {by_tg.role}\n"
                    "Если хочешь сменить — напиши /unbind, а потом /start снова."
                )
                return

            # Email пустой — обычный сценарий слияния
            hr_emp = db.query(Employee).filter_by(email=email).first()
            if hr_emp and hr_emp.id != by_tg.id:
                # Проверка по уволенным: нельзя цеплять уволенного через HRBox-запись
                if not hr_emp.is_active:
                    await message.answer(
                        f"⚠ Сотрудник с email {email} деактивирован в HRBox.\n"
                        "Привязка невозможна."
                    )
                    return
                # Проверка по другому TG: занят кем-то
                if hr_emp.user_id and hr_emp.user_id != tg_id:
                    await message.answer(
                        f"⚠ Email {email} уже привязан к другому TG-аккаунту.\n"
                        "Если это ошибка — обратись в саппорт-чат."
                    )
                    return
                # Проверка по роли: «other» не пускаем
                if hr_emp.role == ROLE_OTHER:
                    await message.answer(
                        f"⚠ Сотрудник с email {email} имеет роль «other» в HRBox.\n"
                        "Бот предназначен только для саппорта и сейлзов.\n"
                        "Если это ошибка — попроси HR обновить роль / отдел в HRBox."
                    )
                    return

                # Сливаем: забираем данные из hr_emp в локальные, удаляем,
                # потом заполняем by_tg (см. фикс UniqueViolation)
                new_email = hr_emp.email
                new_hrbox_id = hr_emp.hrbox_id
                new_full_name = hr_emp.full_name or by_tg.full_name
                new_role = hr_emp.role
                new_position = hr_emp.position
                new_department_id = hr_emp.department_id
                new_department_name = hr_emp.department_name
                new_synced_at = hr_emp.synced_at

                db.delete(hr_emp)
                db.flush()

                by_tg.email = new_email
                by_tg.hrbox_id = new_hrbox_id
                by_tg.full_name = new_full_name
                by_tg.role = new_role
                by_tg.position = new_position
                by_tg.department_id = new_department_id
                by_tg.department_name = new_department_name
                by_tg.is_active = True
                by_tg.synced_at = new_synced_at
                by_tg.username = tg_username  # обновим username на свежий
                db.commit()
                await message.answer(
                    f"✅ Привязка успешна\n"
                    f"TG: @{tg_username or 'no-username'}\n"
                    f"Email: {by_tg.email}\n"
                    f"ФИО: {by_tg.full_name or '—'}\n"
                    f"Роль: {by_tg.role}\n"
                    f"Отдел: {by_tg.department_name or '?'}"
                )
            else:
                # HRBox-записи нет → не пускаем (раньше создавали фолбэк, теперь нет)
                await message.answer(
                    f"⚠ Не нашёл сотрудника с email {email} в HRBox.\n"
                    "Возможные причины:\n"
                    "• Email с опечаткой\n"
                    "• Тебя ещё нет в HRBox — попроси HR добавить\n"
                    "• Синхронизация ещё не дошла — попроси админа /emp_sync"
                )
            return

        # 2) Не зарегистрирован по TG. Ищем по email в HRBox-записях.
        hr_emp = db.query(Employee).filter_by(email=email).first()
        if not hr_emp:
            await message.answer(
                f"⚠ Не нашёл сотрудника с email {email} в HRBox.\n"
                "Возможные причины:\n"
                "• Email с опечаткой — проверь и попробуй ещё раз\n"
                "• Тебя ещё нет в HRBox — попроси HR добавить\n"
                "• Синхронизация ещё не дошла — попробуй через час, или попроси "
                "админа выполнить /emp_sync"
            )
            return

        # Проверки на уволен / занят чужим / роль other
        if not hr_emp.is_active:
            await message.answer(
                f"⚠ Сотрудник с email {email} деактивирован в HRBox.\n"
                "Привязка невозможна."
            )
            return
        if hr_emp.user_id and hr_emp.user_id != tg_id:
            await message.answer(
                f"⚠ Email {email} уже привязан к другому TG-аккаунту.\n"
                "Если это ошибка — обратись в саппорт-чат."
            )
            return
        if hr_emp.role == ROLE_OTHER:
            await message.answer(
                f"⚠ Сотрудник с email {email} имеет роль «other» в HRBox.\n"
                "Бот предназначен только для саппорта и сейлзов.\n"
                "Если это ошибка — попроси HR обновить роль / отдел в HRBox."
            )
            return

        # Всё ок — линкуем
        hr_emp.user_id = tg_id
        hr_emp.username = tg_username
        db.commit()
        await message.answer(
            f"✅ Привязка успешна\n"
            f"TG: @{tg_username or 'no-username'}\n"
            f"Email: {hr_emp.email}\n"
            f"ФИО: {hr_emp.full_name or '?'}\n"
            f"Роль: {hr_emp.role}\n"
            f"Отдел: {hr_emp.department_name or '?'}"
        )


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    """/start [email] — приветствие + self-link для инженеров."""
    args = (message.text or "").strip().split(maxsplit=1)
    if len(args) >= 2:
        await _self_link_by_email(message, args[1])
        return

    # Просто /start без email — приветствие
    if _is_employee_msg(message):
        await message.answer(
            "Привет! Ты уже зарегистрирован в системе.\n"
            "Команды смотри в /hello."
        )
    else:
        await message.answer(
            "👋 Привет! Я бот-органайзер встреч.\n\n"
            "Чтобы привязать свой Telegram к корпоративной карточке, отправь:\n"
            "<code>/start your.name@example.com</code>\n\n"
            "После этого ты сможешь брать встречи и видеть статистику."
        )


@router.message(Command("whoami"))
async def cmd_whoami(message: Message) -> None:
    """/whoami [email] — то же что /start <email>, плюс показывает текущий статус."""
    args = (message.text or "").strip().split(maxsplit=1)
    if len(args) >= 2:
        await _self_link_by_email(message, args[1])
        return

    if not message.from_user:
        await message.answer("Не удалось определить пользователя.")
        return

    with Database.session() as db:
        emp = db.query(Employee).filter_by(user_id=str(message.from_user.id)).first()
    if emp:
        active_note = "" if emp.is_active else "\n⚠ Аккаунт ДЕАКТИВИРОВАН"
        await message.answer(
            f"Ты зарегистрирован:\n"
            f"TG: @{emp.username or 'no-username'} (id={emp.user_id})\n"
            f"Email: {emp.email or '—'}\n"
            f"ФИО: {emp.full_name or '—'}\n"
            f"Роль: {emp.role}\n"
            f"Отдел: {emp.department_name or '—'}"
            f"{active_note}"
        )
    else:
        await message.answer(
            "Ты пока не зарегистрирован.\n"
            "Используй <code>/whoami your.name@example.com</code> для привязки."
        )


# ── /unbind — самостоятельная отвязка TG↔email ───────────────────────────────
@router.message(Command("unbind"))
async def cmd_unbind(message: Message) -> None:
    """/unbind в личке — юзер сам разрывает свою привязку.

    Что делает:
      • Сбрасывает user_id и username (но запись с email/hrbox_id остаётся)
      • Снимает с всех будущих встреч (EventAssignee) — другой инженер возьмёт
      • Прошедшие встречи не трогает (для статистики)
    """
    if not message.from_user:
        await message.answer("Не удалось определить пользователя Telegram.")
        return

    # Только в личке
    if message.chat.type != "private":
        await message.answer("Команда /unbind работает только в личке бота.")
        return

    tg_id = str(message.from_user.id)

    with Database.session() as db:
        emp = db.query(Employee).filter_by(user_id=tg_id).first()
        if not emp:
            await message.answer(
                "Ты и так не привязан 👌\n"
                "Если хочешь привязаться — напиши /start your.name@example.com"
            )
            return

        # Снимаем со всех БУДУЩИХ встреч
        now_utc = datetime.utcnow()
        future_assignees = (
            db.query(EventAssignee)
            .join(Event, Event.id == EventAssignee.event_id)
            .filter(EventAssignee.employee_id == emp.id)
            .filter(Event.start_time > now_utc)
            .all()
        )
        removed_count = len(future_assignees)
        for a in future_assignees:
            db.delete(a)
        db.flush()

        # Подтянем lead'а для встреч где он был ведущим
        for a in future_assignees:
            _promote_first_if_no_lead(db, a.event_id)

        # Сбрасываем TG-привязку
        old_email = emp.email
        old_role = emp.role
        emp.user_id = None
        emp.username = None
        db.commit()

        await message.answer(
            f"✅ Привязка снята.\n"
            f"Email {old_email or '—'} ({old_role}) теперь без TG.\n"
            + (f"Снят с {removed_count} будущих встреч.\n" if removed_count else "")
            + "Чтобы привязаться снова — /start your.name@example.com"
        )


# ── /hello ───────────────────────────────────────────────────────────────────
@router.message(Command("hello"))
async def cmd_hello(message: Message) -> None:
    await message.answer(
        "Привет! Я бот для встреч.\n"
        "Команды:\n"
        "/start &lt;email&gt; — привязать TG к корпоративной почте\n"
        "/unbind — отвязать TG от email (в личке)\n"
        "/whoami — показать твою привязку\n"
        "/hello — это сообщение\n"
        "/orgstat — меню статистики (выбор периода)\n"
        "/stats_day, /stats_all, /statorg dd.mm.yyyy dd.mm.yyyy\n"
        "/report today|week|month — графики\n"
        "\n"
        "<b>Управление сотрудниками</b> (только из SUPPORT-чата):\n"
        "/emp_list — список с ролями\n"
        "/emp_add tg_id [username] — добавить инженера вручную\n"
        "/emp_del tg_id — удалить\n"
        "/emp_sync — ручной запуск HRBox-синка\n"
        "/emp_link @username|tg_id email — связать TG с HRBox по email\n"
        "/emp_role @username|tg_id|email engineer|sales|other — поменять роль вручную\n"
    )


# ── /orgstat ─────────────────────────────────────────────────────────────────
def _orgstat_period(now_msk: datetime, key: str) -> Tuple[Optional[datetime], Optional[datetime], Optional[str]]:
    key = key.lower()
    if key == "today":
        return (now_msk.replace(hour=0, minute=0, second=0, microsecond=0), now_msk, "сегодня")
    if key == "week":
        start = (now_msk - timedelta(days=now_msk.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        return start, now_msk, "текущую неделю"
    if key == "month":
        return now_msk.replace(day=1, hour=0, minute=0, second=0, microsecond=0), now_msk, "текущий месяц"
    if key == "quarter":
        q = (now_msk.month - 1) // 3
        start = now_msk.replace(month=q * 3 + 1, day=1, hour=0, minute=0, second=0, microsecond=0)
        return start, now_msk, f"{q + 1}-й квартал {now_msk.year} года"
    if key == "year":
        return now_msk.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0), now_msk, f"{now_msk.year} год"
    return None, None, None


@router.message(Command("orgstat"))
async def cmd_orgstat(message: Message) -> None:
    if not _is_employee_msg(message):
        await message.answer("У вас нет прав для использования этой команды.")
        return

    text = (
        "👋 Привет! Это органайзер встреч.\n\n"
        "Здесь можно посмотреть статистику по встречам.\n\n"
        "Выберите период кнопками или задайте даты командой:\n"
        "<code>/statorg dd.mm.yyyy dd.mm.yyyy</code>"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="📍 Сегодня", callback_data="orgstat:today")
    kb.button(text="📆 Текущая неделя", callback_data="orgstat:week")
    kb.button(text="🗓 Текущий месяц", callback_data="orgstat:month")
    kb.button(text="📊 Текущий квартал", callback_data="orgstat:quarter")
    kb.button(text="📈 Текущий год", callback_data="orgstat:year")
    kb.button(text="📚 Всё время", callback_data="orgstat:all")
    kb.adjust(2, 2, 2)
    await message.answer(text, reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("orgstat:"))
async def cb_orgstat(callback: CallbackQuery) -> None:
    if not _is_employee_cb(callback):
        await callback.answer("У вас нет прав для этой статистики.", show_alert=True)
        return
    if not callback.data:
        await callback.answer("Некорректные данные.", show_alert=True)
        return

    _, key = callback.data.split(":", 1)
    now_msk = datetime.now(MOSCOW_TZ)

    with Database.session() as db:
        if key == "all":
            first_event = db.query(Event).order_by(Event.start_time.asc()).first()
            if not first_event:
                await callback.message.answer("В БД нет встреч, статистика пуста.")
                await callback.answer()
                return
            earliest = pytz.UTC.localize(first_event.start_time).astimezone(MOSCOW_TZ)
            start_msk = earliest.replace(hour=0, minute=0, second=0, microsecond=0)
            end_msk = now_msk
            period_label = f"всё время (с {start_msk.strftime('%d.%m.%Y')})"
        else:
            start_msk, end_msk, label = _orgstat_period(now_msk, key)
            if not start_msk or not end_msk:
                await callback.answer("Неизвестный период.", show_alert=True)
                return
            period_label = label

        stats_text = gather_stats_for_period(db, start_msk, end_msk)

    header = (
        f"📋 Органайзер: статистика за {period_label}\n"
        f"(с {start_msk.strftime('%d.%m.%Y %H:%M')} "
        f"по {end_msk.strftime('%d.%m.%Y %H:%M')}):\n\n"
    )
    await callback.message.answer(header + stats_text)
    await callback.answer()


# ── stats commands (без изменений) ───────────────────────────────────────────
@router.message(Command("stats_day"))
async def cmd_stats_day(message: Message) -> None:
    if not _is_employee_msg(message):
        await message.answer("У вас нет прав для использования этой команды.")
        return
    now_msk = datetime.now(MOSCOW_TZ)
    start_msk = now_msk.replace(hour=0, minute=0, second=0, microsecond=0)
    with Database.session() as db:
        body = gather_stats_for_period(db, start_msk, now_msk)
    await message.answer(
        f"Статистика за сегодня (с {start_msk:%d.%m.%Y %H:%M} по {now_msk:%d.%m.%Y %H:%M}):\n\n" + body
    )


@router.message(Command("stats_all"))
async def cmd_stats_all(message: Message) -> None:
    if not _is_employee_msg(message):
        await message.answer("У вас нет прав для использования этой команды.")
        return
    now_msk = datetime.now(MOSCOW_TZ)
    with Database.session() as db:
        first_event = db.query(Event).order_by(Event.start_time.asc()).first()
        if not first_event:
            await message.answer("В БД нет встреч, статистика пуста.")
            return
        earliest = pytz.UTC.localize(first_event.start_time).astimezone(MOSCOW_TZ)
        start_msk = earliest.replace(hour=0, minute=0, second=0, microsecond=0)
        body = gather_stats_for_period(db, start_msk, now_msk)
    await message.answer(
        f"Статистика за всё время (с {start_msk:%d.%m.%Y %H:%M} по {now_msk:%d.%m.%Y %H:%M}):\n\n" + body
    )


@router.message(Command("statorg"))
async def cmd_statorg(message: Message) -> None:
    if not _is_employee_msg(message):
        await message.answer("У вас нет прав для использования этой команды.")
        return
    args = (message.text or "").strip().split()
    if len(args) < 3:
        await message.answer("Использование: /statorg dd.mm.yyyy dd.mm.yyyy")
        return

    def parse(s: str) -> Optional[datetime]:
        try:
            d, m, y = map(int, s.split("."))
            return MOSCOW_TZ.localize(datetime(y, m, d, 0, 0, 0))
        except (ValueError, OverflowError):
            return None

    start_msk = parse(args[1])
    end_msk = parse(args[2])
    if not start_msk or not end_msk:
        await message.answer("Ошибка в формате дат. Ожидается dd.mm.yyyy dd.mm.yyyy")
        return
    if end_msk < start_msk:
        start_msk, end_msk = end_msk, start_msk
    end_msk = end_msk.replace(hour=23, minute=59, second=59)

    with Database.session() as db:
        body = gather_stats_for_period(db, start_msk, end_msk)
    await message.answer(
        f"Статистика за период: c {start_msk:%d.%m.%Y} по {end_msk:%d.%m.%Y}:\n\n" + body
    )


# ── /report ──────────────────────────────────────────────────────────────────
def _period_by_kw(now_msk: datetime, kw: str) -> Tuple[Optional[datetime], Optional[datetime]]:
    if kw == "today":
        return now_msk.replace(hour=0, minute=0, second=0, microsecond=0), now_msk
    if kw == "week":
        start = (now_msk - timedelta(days=now_msk.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        return start, now_msk
    if kw == "month":
        return now_msk.replace(day=1, hour=0, minute=0, second=0, microsecond=0), now_msk
    return None, None


@router.message(Command("report"))
async def cmd_report(message: Message) -> None:
    if not _is_employee_msg(message):
        await message.answer("У вас нет прав для использования этой команды.")
        return
    args = (message.text or "").strip().split()
    if len(args) < 2 or args[1] not in ("today", "week", "month"):
        await message.answer("Использование: /report today|week|month")
        return

    now_msk = datetime.now(MOSCOW_TZ)
    start_msk, end_msk = _period_by_kw(now_msk, args[1])
    with Database.session() as db:
        df = build_df_for_period(db, start_msk, end_msk)
    if df.empty:
        await message.answer("За период событий нет.")
        return

    pics = make_basic_charts(df)
    for name, png in pics.items():
        path = f"/tmp/{name}"
        with open(path, "wb") as f:
            f.write(png)
        await message.answer_photo(FSInputFile(path), caption=name)


# ── управление сотрудниками ──────────────────────────────────────────────────
def _find_employee(db, key: str) -> Optional[Employee]:
    """Поиск сотрудника по чему угодно: tg_id, @username, email."""
    raw = key.lstrip("@").strip()
    if not raw:
        return None
    # tg_id (число, возможно с минусом — не для пользователей, но защитимся)
    if raw.isdigit() or (raw.startswith("-") and raw[1:].isdigit()):
        emp = db.query(Employee).filter_by(user_id=raw).first()
        if emp:
            return emp
    # email
    if "@" in raw and "." in raw:
        emp = db.query(Employee).filter_by(email=raw.lower()).first()
        if emp:
            return emp
    # username
    emp = db.query(Employee).filter_by(username=raw).first()
    return emp


@router.message(Command("emp_add"))
async def cmd_emp_add(message: Message) -> None:
    """/emp_add tg_id [username] — вручную, для случаев когда HRBox недоступен."""
    if not _only_support_chat(message):
        await message.answer("Команда доступна только в SUPPORT_CHAT_ID.")
        return
    args = (message.text or "").strip().split()
    if len(args) < 2:
        await message.answer("Использование: /emp_add tg_id [username]")
        return

    tg_id = args[1]
    username = args[2].lstrip("@") if len(args) > 2 else None

    with Database.session() as db:
        emp = db.query(Employee).filter_by(user_id=str(tg_id)).first()
        if not emp:
            # role=engineer по умолчанию — обычно добавляют инженеров
            db.add(Employee(user_id=str(tg_id), username=username, role=ROLE_ENGINEER))
        elif username:
            emp.username = username
        db.commit()
    uname = f"@{username}" if username else "(без username)"
    await message.answer(f"Добавлен/обновлён сотрудник: user_id={tg_id}, username={uname}")


@router.message(Command("emp_del"))
async def cmd_emp_del(message: Message) -> None:
    """/emp_del tg_id"""
    if not _only_support_chat(message):
        await message.answer("Команда доступна только в SUPPORT_CHAT_ID.")
        return
    args = (message.text or "").strip().split()
    if len(args) < 2:
        await message.answer("Использование: /emp_del tg_id")
        return
    tg_id = args[1]
    with Database.session() as db:
        emp = db.query(Employee).filter_by(user_id=str(tg_id)).first()
        if not emp:
            await message.answer("Такого сотрудника нет.")
            return
        db.delete(emp)
        db.commit()
    await message.answer(f"Удалён сотрудник user_id={tg_id}")


@router.message(Command("emp_list"))
async def cmd_emp_list(message: Message) -> None:
    """Список инженеров с привязкой TG и email.

    Показываем ТОЛЬКО инженеров (саппорт-команда — те, кто берёт встречи).
    Сейлзы и прочие в HRBox-синке тоже есть, но в TG их не привязываем —
    они нужны только для тегов в чате при перегрузе.

    Если сообщение длиннее 3500 символов — разбивается на чанки.
    """
    if not _only_support_chat(message):
        await message.answer("Команда доступна только в SUPPORT_CHAT_ID.")
        return

    with Database.session() as db:
        engineers = (
            db.query(Employee)
            .filter_by(role=ROLE_ENGINEER, is_active=True)
            .all()
        )
        # Подсчёт прочих ролей — справочно
        sales_count = db.query(Employee).filter_by(role=ROLE_SALES, is_active=True).count()
        other_count = db.query(Employee).filter_by(role=ROLE_OTHER, is_active=True).count()
        inactive_count = db.query(Employee).filter_by(is_active=False).count()

    if not engineers:
        await message.answer("(инженеров нет)")
        return

    engineers.sort(key=lambda e: (e.full_name or e.username or "").lower())

    # Разделим: с TG (слинкованные) и без TG (только HRBox)
    linked = [e for e in engineers if e.user_id]
    not_linked = [e for e in engineers if not e.user_id]

    lines: list[str] = [f"<b>👷 Инженеры ({len(engineers)})</b>"]

    if linked:
        lines.append(f"\n✅ С привязкой Telegram ({len(linked)}):")
        for e in linked:
            tg = f"@{e.username}" if e.username else f"ID_{e.user_id}"
            mail = e.email or "—"
            fio = e.full_name or "—"
            lines.append(f"• {fio} — {tg}\n    ✉ {mail}")

    if not_linked:
        lines.append(f"\n⚠ Без привязки Telegram ({len(not_linked)}):")
        lines.append("  Им нужно нажать /start <email> в личке бота.")
        for e in not_linked:
            mail = e.email or "—"
            fio = e.full_name or "—"
            lines.append(f"• {fio}\n    ✉ {mail}")

    lines.append(
        f"\n<i>В базе также: sales={sales_count}, other={other_count}, "
        f"скрыто неактивных={inactive_count}</i>"
    )

    # Разбивка на чанки по 3500 символов (запас под HTML-теги, Telegram limit = 4096)
    CHUNK_LIMIT = 3500
    chunks: list[str] = []
    buf = ""
    for ln in lines:
        if len(buf) + len(ln) + 1 > CHUNK_LIMIT:
            chunks.append(buf)
            buf = ln
        else:
            buf = (buf + "\n" + ln) if buf else ln
    if buf:
        chunks.append(buf)

    for chunk in chunks:
        await message.answer(chunk)


@router.message(Command("emp_sync"))
async def cmd_emp_sync(message: Message) -> None:
    """Ручной запуск HRBox-синка."""
    if not _only_support_chat(message):
        await message.answer("Команда доступна только в SUPPORT_CHAT_ID.")
        return
    await message.answer("⏳ Запускаю синк с HRBox…")
    result = await sync_employees()
    await message.answer(result.summary())


@router.message(Command("emp_link"))
async def cmd_emp_link(message: Message) -> None:
    """/emp_link @username|tg_id email — связать TG-сотрудника с HRBox-email.

    Если в БД есть запись с этим email (создана HRBox-синком),
    мы сливаем её с TG-записью: переносим email/hrbox-поля в TG-запись,
    удаляем дубликат.
    """
    if not _only_support_chat(message):
        await message.answer("Команда доступна только в SUPPORT_CHAT_ID.")
        return
    args = (message.text or "").strip().split()
    if len(args) < 3:
        await message.answer("Использование: /emp_link @username|tg_id email")
        return

    key = args[1]
    email = args[2].lower().strip()

    with Database.session() as db:
        tg_emp = _find_employee(db, key)
        if not tg_emp:
            await message.answer(f"Не нашёл сотрудника по '{key}'.")
            return

        hrbox_emp = db.query(Employee).filter_by(email=email).first()

        if hrbox_emp and hrbox_emp.id == tg_emp.id:
            await message.answer(f"Сотрудник '{key}' уже связан с {email}.")
            return

        if hrbox_emp:
            # Сливаем: HRBox-данные → tg_emp, удаляем дубликат
            tg_emp.email = hrbox_emp.email
            tg_emp.hrbox_id = hrbox_emp.hrbox_id
            tg_emp.full_name = hrbox_emp.full_name or tg_emp.full_name
            tg_emp.role = hrbox_emp.role
            tg_emp.position = hrbox_emp.position
            tg_emp.department_id = hrbox_emp.department_id
            tg_emp.department_name = hrbox_emp.department_name
            tg_emp.is_active = hrbox_emp.is_active
            tg_emp.synced_at = hrbox_emp.synced_at
            db.delete(hrbox_emp)
            db.commit()
            await message.answer(
                f"✅ {key} связан с {email}\n"
                f"Роль: {tg_emp.role}, отдел: {tg_emp.department_name or '?'}"
            )
        else:
            # Просто пишем email — следующий синк дозаполнит остальное
            tg_emp.email = email
            db.commit()
            await message.answer(
                f"⚠ Email {email} записан, но в HRBox такого сотрудника пока нет.\n"
                f"После /emp_sync HRBox дозаполнит данные."
            )


@router.message(Command("emp_role"))
async def cmd_emp_role(message: Message) -> None:
    """/emp_role @username|tg_id|email engineer|sales|other"""
    if not _only_support_chat(message):
        await message.answer("Команда доступна только в SUPPORT_CHAT_ID.")
        return
    args = (message.text or "").strip().split()
    if len(args) < 3:
        await message.answer(
            "Использование: /emp_role @username|tg_id|email engineer|sales|other"
        )
        return

    key, new_role = args[1], args[2].lower()
    if new_role not in VALID_ROLES:
        await message.answer(f"Неизвестная роль '{new_role}'. Допустимо: {', '.join(VALID_ROLES)}")
        return

    with Database.session() as db:
        emp = _find_employee(db, key)
        if not emp:
            await message.answer(f"Не нашёл сотрудника по '{key}'.")
            return
        old_role = emp.role
        emp.role = new_role
        db.commit()
    await message.answer(f"✅ {key}: {old_role} → {new_role}")
