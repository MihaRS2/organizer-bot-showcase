"""Short-id ↔ raw event_id mapping.

CalDAV UID-ы длинные и в callback_data телеграма (макс. 64 байта) не помещаются.
Храним короткий hash и сопоставление в БД, чтобы кнопки переживали рестарт.
"""
from __future__ import annotations

import hashlib
from typing import Optional

from bot.db import Database
from bot.models.short_id_map import ShortIdMap


def shorten_and_store_event_id(raw_event_id: str) -> str:
    clean_id = "".join(ch for ch in raw_event_id if 32 <= ord(ch) < 127)
    short_id = hashlib.md5(clean_id.encode()).hexdigest()[:8]

    with Database.session() as db:
        existing = db.query(ShortIdMap).filter_by(short_id=short_id).first()
        if existing:
            if existing.event_id != raw_event_id:
                existing.event_id = raw_event_id
                db.commit()
        else:
            db.add(ShortIdMap(short_id=short_id, event_id=raw_event_id))
            db.commit()
    return short_id


def get_raw_event_id(short_id: str) -> Optional[str]:
    with Database.session() as db:
        mapping = db.query(ShortIdMap).filter_by(short_id=short_id).first()
        return mapping.event_id if mapping else None
