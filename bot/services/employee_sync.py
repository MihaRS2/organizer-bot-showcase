"""Синхронизация сотрудников из HRBox в БД.

Логика:
  1. Загружаем все отделы → mapping id → HrboxDepartment
  2. Идём по сотрудникам с пагинацией, СОБИРАЕМ В ПАМЯТЬ
  3. Дедуплицируем — по hrbox_id и по email. Если HRBox отдаёт двух с одинаковым
     email (например, один уволен и его место занял другой), оставляем активного.
  4. Upsert в БД по hrbox_id, потом по email
  5. user_id и username НЕ ТРОГАЕМ — это Telegram-источник
  6. Кого не увидели за этот синк (но у них есть hrbox_id) — помечаем is_active=False
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Set

from sqlalchemy.exc import IntegrityError

from bot.config import BotConfig
from bot.db import Database
from bot.integrations.hrbox import (
    HrboxClient,
    HrboxDepartment,
    HrboxEmployment,
)
from bot.models.employees import (
    Employee,
    ROLE_ENGINEER,
    ROLE_OTHER,
    ROLE_SALES,
)

log = logging.getLogger(__name__)


# ── Определение роли ────────────────────────────────────────────────────────
# Engineer (= саппорт) — ТОЛЬКО точное совпадение названия отдела.
# Это гарантирует, что разработчики, QA и т.д. не попадут в инженеров,
# даже если у них в должности есть слово "инженер".
ENGINEER_DEPARTMENT_NAMES = (
    "Engineering Department",
)

# Sales — по ключевым словам (отдел или должность)
_SALES_KEYWORDS = (
    "продаж",
    "sales",
    "развити",
    "аккаунт",
)


def _matches_sales(text: Optional[str]) -> bool:
    if not text:
        return False
    low = text.lower()
    return any(kw in low for kw in _SALES_KEYWORDS)


def detect_role(
    emp: HrboxEmployment,
    dept_by_id: Dict[str, HrboxDepartment],
) -> str:
    """Определение роли:
      1) engineer ⇔ название отдела ∈ ENGINEER_DEPARTMENT_NAMES (точное совпадение)
      2) sales ⇔ ключевое слово в названии отдела или должности
      3) иначе other

    Engineer проверяется ПЕРВЫМ, чтобы «Руководитель отдела технической поддержки»
    стал engineer, а не sales (хотя «руководитель» не sales-keyword, на всякий случай).
    """
    # 1) engineer: точное название отдела
    dept_id = emp.department_id
    visited: Set[str] = set()
    while dept_id and dept_id not in visited:
        visited.add(dept_id)
        dept = dept_by_id.get(dept_id)
        if not dept:
            break
        if dept.name in ENGINEER_DEPARTMENT_NAMES:
            return ROLE_ENGINEER
        dept_id = dept.parent_id

    # 2) sales: keyword в отделе или должности
    dept_id = emp.department_id
    visited.clear()
    while dept_id and dept_id not in visited:
        visited.add(dept_id)
        dept = dept_by_id.get(dept_id)
        if not dept:
            break
        if _matches_sales(dept.name):
            return ROLE_SALES
        dept_id = dept.parent_id
    if _matches_sales(emp.position):
        return ROLE_SALES

    return ROLE_OTHER


# ── Дедупликация ────────────────────────────────────────────────────────────
def _dedupe_by_email(
    employments: List[HrboxEmployment],
) -> List[HrboxEmployment]:
    """Из списка HRBox-сотрудников оставляем по одному на email.

    Приоритет при коллизии (один email, два hrbox_id):
      1. Активный (is_active=True) → выигрывает у уволенного
      2. Иначе — последний встретившийся (предполагаем что HRBox отдаёт
         в каком-то порядке, более «свежий» — последним)

    Сотрудников без email пропускаем сразу — мэтчить нечем.
    """
    by_email: Dict[str, HrboxEmployment] = {}
    no_email: List[HrboxEmployment] = []

    for emp in employments:
        if not emp.email:
            no_email.append(emp)
            continue

        existing = by_email.get(emp.email)
        if existing is None:
            by_email[emp.email] = emp
            continue

        # Конфликт: оставляем активного, либо последнего
        if emp.is_active and not existing.is_active:
            log.warning(
                "Email collision: %s — keeping ACTIVE hrbox_id=%s "
                "(replacing inactive hrbox_id=%s)",
                emp.email, emp.hrbox_id, existing.hrbox_id,
            )
            by_email[emp.email] = emp
        elif existing.is_active and not emp.is_active:
            log.warning(
                "Email collision: %s — keeping ACTIVE hrbox_id=%s "
                "(ignoring inactive hrbox_id=%s)",
                emp.email, existing.hrbox_id, emp.hrbox_id,
            )
            # existing остаётся
        else:
            log.warning(
                "Email collision: %s — both have same is_active=%s, "
                "keeping latest hrbox_id=%s (was %s)",
                emp.email, emp.is_active, emp.hrbox_id, existing.hrbox_id,
            )
            by_email[emp.email] = emp

    log.info(
        "Dedupe: %d total, %d with email, %d unique emails, %d without email",
        len(employments), len(employments) - len(no_email), len(by_email), len(no_email),
    )
    return list(by_email.values())


# ── Результат ───────────────────────────────────────────────────────────────
@dataclass
class SyncResult:
    departments: int = 0
    employees_total: int = 0
    created: int = 0
    updated: int = 0
    deactivated: int = 0
    skipped_no_email: int = 0
    duplicates_resolved: int = 0
    role_counts: Dict[str, int] = field(default_factory=dict)
    error: Optional[str] = None

    def summary(self) -> str:
        if self.error:
            return f"❌ HRBox синк не удался: {self.error}"
        roles = ", ".join(f"{r}={c}" for r, c in sorted(self.role_counts.items())) or "—"
        return (
            "✅ HRBox синк завершён:\n"
            f"  • отделов: {self.departments}\n"
            f"  • сотрудников в HRBox: {self.employees_total}\n"
            f"  • создано в БД: {self.created}\n"
            f"  • обновлено: {self.updated}\n"
            f"  • деактивировано (нет в HRBox): {self.deactivated}\n"
            f"  • пропущено (без email): {self.skipped_no_email}\n"
            f"  • дубликатов по email слито: {self.duplicates_resolved}\n"
            f"  • роли: {roles}"
        )


# ── Главная функция синка ───────────────────────────────────────────────────
async def sync_employees() -> SyncResult:
    result = SyncResult()

    if not (BotConfig.HRBOX_CLIENT_ID and BotConfig.HRBOX_CLIENT_SECRET):
        result.error = "HRBox credentials не настроены (HRBOX_CLIENT_ID / HRBOX_CLIENT_SECRET)"
        return result

    try:
        cli = HrboxClient(
            base_url=BotConfig.HRBOX_API_URL,
            client_id=BotConfig.HRBOX_CLIENT_ID,
            client_secret=BotConfig.HRBOX_CLIENT_SECRET,
        )

        log.info("HRBox sync: loading departments…")
        departments = await cli.list_departments()
        dept_by_id = {d.id: d for d in departments}
        result.departments = len(departments)
        log.info("HRBox sync: %d departments loaded", len(departments))

        # Шаг 1: собираем всех в память
        log.info("HRBox sync: iterating employees…")
        all_employees: List[HrboxEmployment] = []
        async for emp in cli.iter_employments(per_page=100, include_removed=True):
            all_employees.append(emp)
        result.employees_total = len(all_employees)

        # Подсчитаем сколько без email отдельно для статистики
        result.skipped_no_email = sum(1 for e in all_employees if not e.email)

        # Шаг 2: дедуп по email
        unique = _dedupe_by_email(all_employees)
        result.duplicates_resolved = (
            (len(all_employees) - result.skipped_no_email) - len(unique)
        )

        now = datetime.utcnow()
        seen_hrbox_ids: Set[str] = set()

        # Шаг 3: upsert каждой записи в отдельной мини-транзакции
        # Так баг с одной записью не валит весь синк.
        with Database.session() as db:
            for emp in unique:
                seen_hrbox_ids.add(emp.hrbox_id)
                role = detect_role(emp, dept_by_id)
                result.role_counts[role] = result.role_counts.get(role, 0) + 1
                dept = dept_by_id.get(emp.department_id) if emp.department_id else None
                dept_name = dept.name if dept else None

                # Ищем существующего: сначала по hrbox_id, потом по email
                existing = (
                    db.query(Employee).filter_by(hrbox_id=emp.hrbox_id).first()
                    or db.query(Employee).filter_by(email=emp.email).first()
                )

                try:
                    if existing:
                        existing.hrbox_id = emp.hrbox_id
                        existing.email = emp.email
                        existing.full_name = emp.full_name
                        existing.role = role
                        existing.position = emp.position
                        existing.department_id = emp.department_id
                        existing.department_name = dept_name
                        existing.is_active = emp.is_active
                        existing.synced_at = now
                        db.flush()
                        result.updated += 1
                    else:
                        new_emp = Employee(
                            hrbox_id=emp.hrbox_id,
                            email=emp.email,
                            full_name=emp.full_name,
                            role=role,
                            position=emp.position,
                            department_id=emp.department_id,
                            department_name=dept_name,
                            is_active=emp.is_active,
                            synced_at=now,
                        )
                        db.add(new_emp)
                        db.flush()  # сразу пишем, чтобы поймать UniqueViolation тут
                        result.created += 1
                except IntegrityError as exc:
                    db.rollback()
                    log.warning(
                        "IntegrityError on hrbox_id=%s email=%s: %s",
                        emp.hrbox_id, emp.email, exc.orig,
                    )
                    # Не падаем — продолжаем с остальными записями
                    continue

            # Деактивация устаревших
            if seen_hrbox_ids:
                stale = (
                    db.query(Employee)
                    .filter(Employee.hrbox_id.isnot(None))
                    .filter(~Employee.hrbox_id.in_(seen_hrbox_ids))
                    .filter(Employee.is_active.is_(True))
                    .all()
                )
                for s in stale:
                    s.is_active = False
                    s.synced_at = now
                    result.deactivated += 1

            db.commit()

        log.info("HRBox sync: %s", result.summary().replace("\n", " "))
        return result

    except Exception as exc:  # noqa: BLE001
        log.exception("HRBox sync failed")
        result.error = str(exc)
        return result
