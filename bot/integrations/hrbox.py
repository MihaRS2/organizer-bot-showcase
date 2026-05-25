"""Асинхронный клиент HRBox API (https://hr-system.example.com/api/v1).

Документация по эндпоинтам:
  - GET /org-employment?page=1&per-page=100 — список сотрудников с пагинацией
  - GET /org-department — все отделы (без пагинации)

Аутентификация — два заголовка:
  X-Hrbox-Client-Id: <id>
  X-Hrbox-Client-Secret: <secret>
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import AsyncIterator, List, Optional

import httpx

log = logging.getLogger(__name__)


# ── DTO ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class HrboxDepartment:
    id: str
    name: str
    parent_id: Optional[str]


@dataclass(frozen=True)
class HrboxEmployment:
    hrbox_id: str
    email: Optional[str]
    first_name: str
    last_name: str
    middle_name: Optional[str]
    full_name: str
    position: Optional[str]
    department_id: Optional[str]
    is_active: bool   # not is_removed AND fired_date is null


def _pick_email(emails: List[dict]) -> Optional[str]:
    """Из списка emails выбираем corporate, иначе первый непустой."""
    if not emails:
        return None
    for e in emails:
        if (e.get("type") == "corporate") and e.get("value"):
            return e["value"].lower().strip()
    for e in emails:
        v = e.get("value")
        if v:
            return v.lower().strip()
    return None


def _make_full_name(last: str, first: str, middle: Optional[str]) -> str:
    parts = [p for p in (last, first, middle or "") if p]
    return " ".join(parts).strip()


# ── Клиент ──────────────────────────────────────────────────────────────────
class HrboxClient:
    """Тонкий async-клиент. Создаётся per-call (sync/list ops короткие)."""

    def __init__(
        self,
        base_url: str,
        client_id: str,
        client_secret: str,
        timeout: float = 30.0,
    ) -> None:
        if not (client_id and client_secret):
            raise ValueError("HRBox credentials not configured (CLIENT_ID/SECRET)")
        self._base_url = base_url.rstrip("/")
        self._headers = {
            "X-Hrbox-Client-Id": client_id,
            "X-Hrbox-Client-Secret": client_secret,
            "Accept": "application/json",
            "User-Agent": "organizer-bot/2.0",
        }
        self._timeout = timeout

    async def list_departments(self, per_page: int = 200) -> List[HrboxDepartment]:
        """Возвращает ВСЕ отделы с пагинацией.

        HRBox по умолчанию отдаёт только первую страницу (~20 записей),
        даже при ?per-page=200 — нужно явно итерировать страницы.
        """
        out: List[HrboxDepartment] = []
        page = 1
        async with httpx.AsyncClient(headers=self._headers, timeout=self._timeout) as cli:
            while True:
                r = await cli.get(
                    f"{self._base_url}/org-department",
                    params={"page": page, "per-page": per_page},
                )
                r.raise_for_status()
                payload = r.json()
                rows = payload.get("data", [])
                if not rows:
                    break
                for row in rows:
                    if row.get("is_removed"):
                        continue
                    out.append(HrboxDepartment(
                        id=row["id"],
                        name=(row.get("name") or "").strip(),
                        parent_id=row.get("parent_id"),
                    ))
                if len(rows) < per_page:
                    break
                page += 1
        return out

    async def iter_employments(
        self,
        per_page: int = 100,
        include_removed: bool = True,
    ) -> AsyncIterator[HrboxEmployment]:
        """Итератор по сотрудникам с авто-пагинацией.

        include_removed=True — отдаём в т.ч. уволенных, помечая is_active=False.
        Это нужно чтобы синк мог снять активность у тех, кого уволили в HRBox.
        """
        page = 1
        async with httpx.AsyncClient(headers=self._headers, timeout=self._timeout) as cli:
            while True:
                resp = await cli.get(
                    f"{self._base_url}/org-employment",
                    params={"page": page, "per-page": per_page},
                )
                resp.raise_for_status()
                payload = resp.json()
                rows = payload.get("data", [])
                if not rows:
                    return

                for row in rows:
                    is_removed = bool(row.get("is_removed"))
                    fired_date = row.get("fired_date")

                    if is_removed and not include_removed:
                        continue

                    email = _pick_email(row.get("emails") or [])
                    first = (row.get("first_name") or "").strip()
                    last = (row.get("last_name") or "").strip()
                    middle = (row.get("middle_name") or "").strip() or None

                    yield HrboxEmployment(
                        hrbox_id=row["id"],
                        email=email,
                        first_name=first,
                        last_name=last,
                        middle_name=middle,
                        full_name=_make_full_name(last, first, middle),
                        position=(row.get("position") or "").strip() or None,
                        department_id=row.get("org_department_id"),
                        is_active=(not is_removed) and (fired_date is None),
                    )

                # последняя страница — выходим
                if len(rows) < per_page:
                    return
                page += 1
