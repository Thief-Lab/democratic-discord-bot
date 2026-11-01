"""SQLite storage for songs, ratings, and per-guild settings.

Design notes
------------
- A "song" is identified by (guild_id, source_type, source_id). Songs are
  scoped per guild -- the same YouTube video played in two different servers
  produces two distinct song rows. This isolates each server's catalog and,
  by extension, its ratings.
- For YouTube / SoundCloud, source_id is the video/track ID. For Spotify, we
  resolve to YouTube and store YouTube's id. For local files, source_id is
  the relative path from LOCAL_MUSIC_DIR. For attached Discord files,
  source_id is the attachment snowflake.
- Ratings are per-user per-song. Because song rows are per-guild, ratings are
  also implicitly per-guild -- a user can rate the same track differently in
  different servers.
- Unrated-by-present-users songs use a neutral default of 3.0 (handled in
  player.py, not here -- the DB returns the raw numbers).
- guild_settings.auto_cycle_enabled controls "master playlist mode": when
  the queue empties, the bot picks random songs from this guild's song
  catalog and plays them if they pass the rating gate.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

import aiosqlite


SCHEMA = """
CREATE TABLE IF NOT EXISTS songs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    source_type  TEXT NOT NULL,
    source_id    TEXT NOT NULL,
    title        TEXT NOT NULL,
    duration_s   INTEGER,
    url          TEXT,
    first_seen   INTEGER NOT NULL,
    UNIQUE(guild_id, source_type, source_id)
);

CREATE INDEX IF NOT EXISTS idx_songs_guild ON songs(guild_id);

CREATE TABLE IF NOT EXISTS ratings (
    user_id     INTEGER NOT NULL,
    song_id     INTEGER NOT NULL,
    rating      INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
    updated_at  INTEGER NOT NULL,
    PRIMARY KEY (user_id, song_id),
    FOREIGN KEY (song_id) REFERENCES songs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_ratings_song ON ratings(song_id);

CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id             INTEGER PRIMARY KEY,
    min_avg_rating       REAL NOT NULL DEFAULT 0.0,
    veto_enabled         INTEGER NOT NULL DEFAULT 0,
    auto_cycle_enabled   INTEGER NOT NULL DEFAULT 0,
    idle_timeout_s       INTEGER NOT NULL DEFAULT 300
);
"""

# Applied after SCHEMA so older DBs (pre-newer-columns) get upgraded.
MIGRATIONS = [
    "ALTER TABLE guild_settings ADD COLUMN auto_cycle_enabled INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE guild_settings ADD COLUMN idle_timeout_s INTEGER NOT NULL DEFAULT 300",
]


@dataclass
class SongRow:
    id: int
    guild_id: int
    source_type: str
    source_id: str
    title: str
    duration_s: Optional[int]
    url: Optional[str]


@dataclass
class GuildSettings:
    guild_id: int
    min_avg_rating: float
    veto_enabled: bool
    auto_cycle_enabled: bool
    # Seconds of nothing-playing before the bot leaves voice. 0 disables.
    idle_timeout_s: int


@dataclass
class RatingSummary:
    count: int
    average: Optional[float]
    lowest: Optional[int]
    highest: Optional[int]


@dataclass
class SongListRow:
    """A song plus its aggregate rating stats -- used by /playlist."""
    id: int
    guild_id: int
    title: str
    source_type: str
    url: Optional[str]
    avg_rating: Optional[float]
    vote_count: int


class Database:
    """Thin async wrapper. Open one instance per process; it owns a single connection."""

    def __init__(self, path: Path):
        self.path = path
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self.path)
        await self._conn.execute("PRAGMA foreign_keys = ON;")

        # Pre-multi-tenant migration: if the songs table exists but lacks
        # guild_id, the old schema is in use. Drop ratings and songs so the
        # new schema can take over. There's no clean way to back-fill guild_id
        # for old rows, so we sacrifice them.
        if await self._songs_table_lacks_guild_id():
            await self._conn.execute("DROP TABLE IF EXISTS ratings")
            await self._conn.execute("DROP TABLE IF EXISTS songs")

        await self._conn.executescript(SCHEMA)
        for stmt in MIGRATIONS:
            try:
                await self._conn.execute(stmt)
            except Exception:
                pass
        await self._conn.commit()

    async def _songs_table_lacks_guild_id(self) -> bool:
        async with self._conn.execute("PRAGMA table_info(songs)") as cur:
            rows = await cur.fetchall()
        if not rows:
            return False  # table doesn't exist yet -- nothing to migrate
        col_names = {r[1] for r in rows}
        return "guild_id" not in col_names

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected -- call connect() first.")
        return self._conn

    async def _fetchall(self, sql: str, params: tuple = ()) -> list:
        async with self.conn.execute(sql, params) as cur:
            return list(await cur.fetchall())

    async def _fetchone(self, sql: str, params: tuple = ()) -> Optional[tuple]:
        async with self.conn.execute(sql, params) as cur:
            row = await cur.fetchone()
            return tuple(row) if row is not None else None

    # ---------- songs ----------

    async def upsert_song(
        self,
        guild_id: int,
        source_type: str,
        source_id: str,
        title: str,
        duration_s: Optional[int],
        url: Optional[str],
    ) -> SongRow:
        now = int(time.time())
        await self.conn.execute(
            """
            INSERT INTO songs (guild_id, source_type, source_id, title, duration_s, url, first_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, source_type, source_id) DO UPDATE SET
                title = excluded.title,
                duration_s = COALESCE(excluded.duration_s, songs.duration_s),
                url = COALESCE(excluded.url, songs.url)
            """,
            (guild_id, source_type, source_id, title, duration_s, url, now),
        )
        await self.conn.commit()
        row = await self._fetchone(
            "SELECT id, guild_id, source_type, source_id, title, duration_s, url FROM songs "
            "WHERE guild_id = ? AND source_type = ? AND source_id = ?",
            (guild_id, source_type, source_id),
        )
        assert row is not None
        return SongRow(id=row[0], guild_id=row[1], source_type=row[2], source_id=row[3],
                       title=row[4], duration_s=row[5], url=row[6])

    async def get_song(self, song_id: int) -> Optional[SongRow]:
        row = await self._fetchone(
            "SELECT id, guild_id, source_type, source_id, title, duration_s, url FROM songs WHERE id = ?",
            (song_id,),
        )
        if row is None:
            return None
        return SongRow(id=row[0], guild_id=row[1], source_type=row[2], source_id=row[3],
                       title=row[4], duration_s=row[5], url=row[6])

    async def random_songs(
        self, guild_id: int, limit: int, exclude_ids: Iterable[int] = ()
    ) -> List[SongRow]:
        """Return up to `limit` random songs from this guild's catalog."""
        excl = list(exclude_ids)
        if excl:
            placeholders = ",".join("?" * len(excl))
            sql = (
                "SELECT id, guild_id, source_type, source_id, title, duration_s, url "
                f"FROM songs WHERE guild_id = ? AND id NOT IN ({placeholders}) "
                "ORDER BY RANDOM() LIMIT ?"
            )
            params = (guild_id, *excl, limit)
        else:
            sql = (
                "SELECT id, guild_id, source_type, source_id, title, duration_s, url "
                "FROM songs WHERE guild_id = ? ORDER BY RANDOM() LIMIT ?"
            )
            params = (guild_id, limit)
        rows = await self._fetchall(sql, params)
        return [
            SongRow(id=r[0], guild_id=r[1], source_type=r[2], source_id=r[3],
                    title=r[4], duration_s=r[5], url=r[6])
            for r in rows
        ]

    async def count_songs(self, guild_id: int) -> int:
        row = await self._fetchone(
            "SELECT COUNT(*) FROM songs WHERE guild_id = ?", (guild_id,)
        )
        return int(row[0]) if row else 0

    async def list_songs(
        self, guild_id: int, *, offset: int = 0, limit: int = 15
    ) -> List[SongListRow]:
        """List this guild's catalog, sorted by average rating (desc).

        Songs with no votes sort below songs that have votes (NULLS LAST,
        achieved via ORDER BY (avg IS NULL)). Ties broken by vote count,
        then alphabetically by title.
        """
        rows = await self._fetchall(
            """
            SELECT s.id, s.guild_id, s.title, s.source_type, s.url,
                   AVG(r.rating) AS avg_rating,
                   COUNT(r.rating) AS vote_count
            FROM songs s
            LEFT JOIN ratings r ON r.song_id = s.id
            WHERE s.guild_id = ?
            GROUP BY s.id
            ORDER BY (AVG(r.rating) IS NULL),
                     AVG(r.rating) DESC,
                     COUNT(r.rating) DESC,
                     s.title ASC
            LIMIT ? OFFSET ?
            """,
            (guild_id, limit, offset),
        )
        return [
            SongListRow(
                id=r[0],
                guild_id=r[1],
                title=r[2],
                source_type=r[3],
                url=r[4],
                avg_rating=float(r[5]) if r[5] is not None else None,
                vote_count=int(r[6]),
            )
            for r in rows
        ]

    async def delete_song(self, guild_id: int, song_id: int) -> bool:
        """Delete a song from this guild's catalog. Cascades to its ratings.

        Scoped by guild_id so a stale view from one server can't delete a
        row in another. Returns True if a row was actually deleted.
        """
        async with self.conn.execute(
            "DELETE FROM songs WHERE id = ? AND guild_id = ?",
            (song_id, guild_id),
        ) as cur:
            deleted = cur.rowcount
        await self.conn.commit()
        return deleted > 0

    # ---------- ratings ----------

    async def set_rating(self, user_id: int, song_id: int, rating: int) -> None:
        if not 1 <= rating <= 5:
            raise ValueError("rating must be 1..5")
        now = int(time.time())
        await self.conn.execute(
            """
            INSERT INTO ratings (user_id, song_id, rating, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, song_id) DO UPDATE SET
                rating = excluded.rating,
                updated_at = excluded.updated_at
            """,
            (user_id, song_id, rating, now),
        )
        await self.conn.commit()

    async def ratings_for_song_by_users(
        self, song_id: int, user_ids: Iterable[int]
    ) -> dict:
        ids = list(user_ids)
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        rows = await self._fetchall(
            f"SELECT user_id, rating FROM ratings WHERE song_id = ? AND user_id IN ({placeholders})",
            (song_id, *ids),
        )
        return {row[0]: row[1] for row in rows}

    async def rating_summary(self, song_id: int) -> RatingSummary:
        row = await self._fetchone(
            "SELECT COUNT(*), AVG(rating), MIN(rating), MAX(rating) FROM ratings WHERE song_id = ?",
            (song_id,),
        )
        assert row is not None
        count, avg, lo, hi = row
        return RatingSummary(
            count=int(count),
            average=float(avg) if avg is not None else None,
            lowest=int(lo) if lo is not None else None,
            highest=int(hi) if hi is not None else None,
        )

    # ---------- settings ----------

    async def get_settings(self, guild_id: int) -> GuildSettings:
        row = await self._fetchone(
            "SELECT guild_id, min_avg_rating, veto_enabled, auto_cycle_enabled, idle_timeout_s "
            "FROM guild_settings WHERE guild_id = ?",
            (guild_id,),
        )
        if row is None:
            await self.conn.execute(
                "INSERT INTO guild_settings (guild_id) VALUES (?)", (guild_id,)
            )
            await self.conn.commit()
            return GuildSettings(
                guild_id=guild_id,
                min_avg_rating=0.0,
                veto_enabled=False,
                auto_cycle_enabled=False,
                idle_timeout_s=300,
            )
        return GuildSettings(
            guild_id=row[0],
            min_avg_rating=float(row[1]),
            veto_enabled=bool(row[2]),
            auto_cycle_enabled=bool(row[3]),
            idle_timeout_s=int(row[4]),
        )

    async def set_threshold(self, guild_id: int, threshold: float) -> None:
        if not 0.0 <= threshold <= 5.0:
            raise ValueError("threshold must be 0..5")
        await self.get_settings(guild_id)
        await self.conn.execute(
            "UPDATE guild_settings SET min_avg_rating = ? WHERE guild_id = ?",
            (threshold, guild_id),
        )
        await self.conn.commit()

    async def set_veto(self, guild_id: int, enabled: bool) -> None:
        await self.get_settings(guild_id)
        await self.conn.execute(
            "UPDATE guild_settings SET veto_enabled = ? WHERE guild_id = ?",
            (1 if enabled else 0, guild_id),
        )
        await self.conn.commit()

    async def set_auto_cycle(self, guild_id: int, enabled: bool) -> None:
        await self.get_settings(guild_id)
        await self.conn.execute(
            "UPDATE guild_settings SET auto_cycle_enabled = ? WHERE guild_id = ?",
            (1 if enabled else 0, guild_id),
        )
        await self.conn.commit()

    async def set_idle_timeout(self, guild_id: int, seconds: int) -> None:
        if seconds < 0:
            raise ValueError("idle timeout must be >= 0 seconds")
        await self.get_settings(guild_id)
        await self.conn.execute(
            "UPDATE guild_settings SET idle_timeout_s = ? WHERE guild_id = ?",
            (seconds, guild_id),
        )
        await self.conn.commit()
