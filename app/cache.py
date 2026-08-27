"""
Persists healed semantic intents so subsequent runs bypass the Healer Agent
entirely and stay deterministic. Keyed on (target_base_url, gherkin_step_hash)
so the same step against a different environment doesn't collide.

SQLite chosen over a JSON dict per the requirement's example - gives us
concurrent-safe writes (via aiosqlite) and trivial querying for the
"Selector Cache Browser" frontend panel, while staying a single file with zero
external services.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import aiosqlite

from app.config import get_settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS healed_intents (
    cache_key           TEXT PRIMARY KEY,
    target_base_url     TEXT NOT NULL,
    gherkin_line         TEXT NOT NULL,
    original_intent       TEXT NOT NULL,
    original_intent_type   TEXT NOT NULL,
    healed_intent            TEXT NOT NULL,
    healed_intent_type        TEXT NOT NULL,
    healing_reasoning          TEXT,
    hit_count                    INTEGER NOT NULL DEFAULT 0,
    created_at                    REAL NOT NULL,
    last_used_at                   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_target ON healed_intents(target_base_url);
"""


def make_cache_key(target_base_url: str, gherkin_line: str) -> str:
    raw = f"{target_base_url}::{gherkin_line}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


@dataclass
class CachedIntent:
    healed_intent: str
    healed_intent_type: str
    healing_reasoning: str
    hit_count: int


class SelectorCache:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or get_settings().cache_db_path
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

    async def _connect(self) -> aiosqlite.Connection:
        conn = await aiosqlite.connect(self.db_path)
        await conn.executescript(_SCHEMA)
        return conn

    async def get(self, cache_key: str) -> Optional[CachedIntent]:
        async with await self._connect() as conn:
            cursor = await conn.execute(
                "SELECT healed_intent, healed_intent_type, healing_reasoning, hit_count "
                "FROM healed_intents WHERE cache_key = ?",
                (cache_key,),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            await conn.execute(
                "UPDATE healed_intents SET hit_count = hit_count + 1, last_used_at = ? WHERE cache_key = ?",
                (time.time(), cache_key),
            )
            await conn.commit()
            return CachedIntent(healed_intent=row[0], healed_intent_type=row[1], healing_reasoning=row[2] or "", hit_count=row[3])

    async def put(
        self,
        *,
        cache_key: str,
        target_base_url: str,
        gherkin_line: str,
        original_intent: str,
        original_intent_type: str,
        healed_intent: str,
        healed_intent_type: str,
        healing_reasoning: str,
    ) -> None:
        now = time.time()
        async with await self._connect() as conn:
            await conn.execute(
                """
                INSERT INTO healed_intents
                    (cache_key, target_base_url, gherkin_line, original_intent, original_intent_type,
                     healed_intent, healed_intent_type, healing_reasoning, hit_count, created_at, last_used_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    healed_intent = excluded.healed_intent,
                    healed_intent_type = excluded.healed_intent_type,
                    healing_reasoning = excluded.healing_reasoning,
                    last_used_at = excluded.last_used_at
                """,
                (
                    cache_key, target_base_url, gherkin_line, original_intent, original_intent_type,
                    healed_intent, healed_intent_type, healing_reasoning, now, now,
                ),
            )
            await conn.commit()

    async def list_all(self, target_base_url: Optional[str] = None) -> list[dict]:
        query = "SELECT * FROM healed_intents"
        params: tuple = ()
        if target_base_url:
            query += " WHERE target_base_url = ?"
            params = (target_base_url,)
        query += " ORDER BY last_used_at DESC"
        async with await self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(query, params)
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def purge(self, cache_key: str) -> None:
        async with await self._connect() as conn:
            await conn.execute("DELETE FROM healed_intents WHERE cache_key = ?", (cache_key,))
            await conn.commit()

    async def purge_all(self, target_base_url: Optional[str] = None) -> int:
        async with await self._connect() as conn:
            if target_base_url:
                cursor = await conn.execute("DELETE FROM healed_intents WHERE target_base_url = ?", (target_base_url,))
            else:
                cursor = await conn.execute("DELETE FROM healed_intents")
            await conn.commit()
            return cursor.rowcount
