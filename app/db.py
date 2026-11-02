"""PostgreSQL + pgvector storage."""
from __future__ import annotations

import json
from typing import Any

import asyncpg

from . import config

SCHEMA = f"""
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS users (
    id          BIGSERIAL PRIMARY KEY,
    display_name TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One user can be reached through several channels (telegram, web).
CREATE TABLE IF NOT EXISTS identities (
    channel     TEXT NOT NULL,
    external_id TEXT NOT NULL,
    user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (channel, external_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    channel     TEXT NOT NULL DEFAULT 'web',
    attachments JSONB NOT NULL DEFAULT '[]',
    embedding   vector({config.EMBED_DIM}),
    archived    BOOLEAN NOT NULL DEFAULT false,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS messages_user_idx ON messages (user_id, id DESC);

-- Long-term facts about the user ("loves Arijit Singh", "exam on 12 May").
CREATE TABLE IF NOT EXISTS memories (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    content     TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'fact',
    embedding   vector({config.EMBED_DIM}),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS memories_user_idx ON memories (user_id);

-- User-built skills: generated Python code plus the user's API secrets.
CREATE TABLE IF NOT EXISTS skills (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    description TEXT NOT NULL,
    instructions TEXT NOT NULL DEFAULT '',
    parameters  JSONB NOT NULL DEFAULT '{{}}',
    code        TEXT NOT NULL,
    secrets     JSONB NOT NULL DEFAULT '{{}}',
    enabled     BOOLEAN NOT NULL DEFAULT true,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, name)
);

CREATE TABLE IF NOT EXISTS link_codes (
    code        TEXT PRIMARY KEY,
    user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def vec(values: list[float] | None) -> str | None:
    if not values:
        return None
    return "[" + ",".join(f"{v:.6f}" for v in values) + "]"


async def _init_conn(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


class Database:
    def __init__(self, dsn: str = config.DATABASE_URL):
        self.dsn = dsn
        self.pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        # Create the extension before the pool so codecs see the vector type.
        conn = await asyncpg.connect(self.dsn)
        try:
            await conn.execute(SCHEMA)
        finally:
            await conn.close()
        self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=8, init=_init_conn)

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()

    # --- users -----------------------------------------------------------
    async def get_or_create_user(self, channel: str, external_id: str, display_name: str = "") -> int:
        async with self.pool.acquire() as c:
            row = await c.fetchrow(
                "SELECT user_id FROM identities WHERE channel=$1 AND external_id=$2", channel, external_id)
            if row:
                if display_name:
                    await c.execute(
                        "UPDATE users SET display_name=$2 WHERE id=$1 AND display_name=''",
                        row["user_id"], display_name)
                return row["user_id"]
            async with c.transaction():
                uid = await c.fetchval("INSERT INTO users (display_name) VALUES ($1) RETURNING id", display_name)
                await c.execute(
                    "INSERT INTO identities (channel, external_id, user_id) VALUES ($1,$2,$3)",
                    channel, external_id, uid)
            return uid

    async def get_user(self, user_id: int) -> dict | None:
        row = await self.pool.fetchrow("SELECT * FROM users WHERE id=$1", user_id)
        return dict(row) if row else None

    async def set_display_name(self, user_id: int, name: str) -> None:
        await self.pool.execute("UPDATE users SET display_name=$2 WHERE id=$1", user_id, name)

    async def create_link_code(self, user_id: int, code: str) -> None:
        await self.pool.execute(
            "INSERT INTO link_codes (code, user_id) VALUES ($1,$2) ON CONFLICT (code) DO UPDATE SET user_id=$2, created_at=now()",
            code, user_id)

    async def redeem_link_code(self, code: str, channel: str, external_id: str) -> int | None:
        """Point (channel, external_id) at the user that issued the code."""
        async with self.pool.acquire() as c:
            async with c.transaction():
                row = await c.fetchrow(
                    "DELETE FROM link_codes WHERE code=$1 AND created_at > now() - interval '15 minutes' RETURNING user_id",
                    code.strip().upper())
                if not row:
                    return None
                target = row["user_id"]
                await c.execute(
                    """INSERT INTO identities (channel, external_id, user_id) VALUES ($1,$2,$3)
                       ON CONFLICT (channel, external_id) DO UPDATE SET user_id=$3""",
                    channel, external_id, target)
                return target

    # --- messages --------------------------------------------------------
    async def add_message(self, user_id: int, role: str, content: str, channel: str,
                          attachments: list | None = None, embedding: list[float] | None = None) -> int:
        return await self.pool.fetchval(
            """INSERT INTO messages (user_id, role, content, channel, attachments, embedding)
               VALUES ($1,$2,$3,$4,$5,$6::vector) RETURNING id""",
            user_id, role, content, channel, attachments or [], vec(embedding))

    async def recent_messages(self, user_id: int, limit: int) -> list[dict]:
        rows = await self.pool.fetch(
            """SELECT id, role, content, channel, attachments, created_at FROM messages
               WHERE user_id=$1 AND NOT archived ORDER BY id DESC LIMIT $2""", user_id, limit)
        return [dict(r) for r in reversed(rows)]

    async def history(self, user_id: int, limit: int = 60) -> list[dict]:
        rows = await self.pool.fetch(
            """SELECT id, role, content, channel, attachments, created_at FROM messages
               WHERE user_id=$1 ORDER BY id DESC LIMIT $2""", user_id, limit)
        return [dict(r) for r in reversed(rows)]

    async def similar_messages(self, user_id: int, embedding: list[float], exclude_after_id: int,
                               limit: int = 5, min_score: float = 0.55) -> list[dict]:
        rows = await self.pool.fetch(
            """SELECT id, role, content, created_at, 1 - (embedding <=> $2::vector) AS score
               FROM messages WHERE user_id=$1 AND embedding IS NOT NULL AND id < $3
               ORDER BY embedding <=> $2::vector LIMIT $4""",
            user_id, vec(embedding), exclude_after_id, limit)
        return [dict(r) for r in rows if r["score"] >= min_score]

    async def archive_history(self, user_id: int) -> None:
        await self.pool.execute("UPDATE messages SET archived=true WHERE user_id=$1", user_id)

    # --- memories --------------------------------------------------------
    async def upsert_memory(self, user_id: int, content: str, embedding: list[float] | None,
                            kind: str = "fact", dedupe_score: float = 0.9) -> int:
        async with self.pool.acquire() as c:
            if embedding:
                row = await c.fetchrow(
                    """SELECT id, 1 - (embedding <=> $2::vector) AS score FROM memories
                       WHERE user_id=$1 AND embedding IS NOT NULL ORDER BY embedding <=> $2::vector LIMIT 1""",
                    user_id, vec(embedding))
                if row and row["score"] >= dedupe_score:
                    await c.execute(
                        "UPDATE memories SET content=$2, embedding=$3::vector, updated_at=now() WHERE id=$1",
                        row["id"], content, vec(embedding))
                    return row["id"]
            return await c.fetchval(
                "INSERT INTO memories (user_id, content, kind, embedding) VALUES ($1,$2,$3,$4::vector) RETURNING id",
                user_id, content, kind, vec(embedding))

    async def search_memories(self, user_id: int, embedding: list[float] | None, limit: int = 8) -> list[dict]:
        if embedding:
            rows = await self.pool.fetch(
                """SELECT id, content, kind, 1 - (embedding <=> $2::vector) AS score FROM memories
                   WHERE user_id=$1 ORDER BY embedding <=> $2::vector NULLS LAST LIMIT $3""",
                user_id, vec(embedding), limit)
        else:
            rows = await self.pool.fetch(
                "SELECT id, content, kind, 0.0 AS score FROM memories WHERE user_id=$1 ORDER BY updated_at DESC LIMIT $2",
                user_id, limit)
        return [dict(r) for r in rows]

    async def list_memories(self, user_id: int) -> list[dict]:
        rows = await self.pool.fetch(
            "SELECT id, content, kind, updated_at FROM memories WHERE user_id=$1 ORDER BY updated_at DESC", user_id)
        return [dict(r) for r in rows]

    async def delete_memory(self, user_id: int, memory_id: int) -> bool:
        res = await self.pool.execute("DELETE FROM memories WHERE user_id=$1 AND id=$2", user_id, memory_id)
        return res.endswith("1")

    async def clear_memories(self, user_id: int) -> None:
        await self.pool.execute("DELETE FROM memories WHERE user_id=$1", user_id)

    # --- skills ----------------------------------------------------------
    async def save_skill(self, user_id: int, name: str, description: str, instructions: str,
                         parameters: dict, code: str, secrets: dict) -> int:
        return await self.pool.fetchval(
            """INSERT INTO skills (user_id, name, description, instructions, parameters, code, secrets)
               VALUES ($1,$2,$3,$4,$5,$6,$7)
               ON CONFLICT (user_id, name) DO UPDATE SET description=$3, instructions=$4,
                   parameters=$5, code=$6, secrets=skills.secrets || $7, enabled=true, updated_at=now()
               RETURNING id""",
            user_id, name, description, instructions, parameters, code, secrets)

    async def list_skills(self, user_id: int, enabled_only: bool = False) -> list[dict]:
        q = "SELECT * FROM skills WHERE user_id=$1" + (" AND enabled" if enabled_only else "") + " ORDER BY name"
        return [dict(r) for r in await self.pool.fetch(q, user_id)]

    async def get_skill(self, user_id: int, name: str) -> dict | None:
        row = await self.pool.fetchrow("SELECT * FROM skills WHERE user_id=$1 AND name=$2", user_id, name)
        return dict(row) if row else None

    async def update_skill_fields(self, user_id: int, name: str, **fields: Any) -> bool:
        allowed = {"description", "instructions", "parameters", "code", "secrets", "enabled"}
        sets, args = [], [user_id, name]
        for key, value in fields.items():
            if key in allowed:
                args.append(value)
                sets.append(f"{key}=${len(args)}")
        if not sets:
            return False
        res = await self.pool.execute(
            f"UPDATE skills SET {', '.join(sets)}, updated_at=now() WHERE user_id=$1 AND name=$2", *args)
        return res.endswith("1")

    async def delete_skill(self, user_id: int, name: str) -> bool:
        res = await self.pool.execute("DELETE FROM skills WHERE user_id=$1 AND name=$2", user_id, name)
        return res.endswith("1")
