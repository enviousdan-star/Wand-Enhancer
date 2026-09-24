"""SQLite storage for scheduled posts and synced comments."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    seq           INTEGER NOT NULL,
    scheduled_at  TEXT NOT NULL,              -- UTC ISO-8601
    status        TEXT NOT NULL DEFAULT 'scheduled',  -- scheduled|publishing|published|failed|cancelled
    caption       TEXT NOT NULL,
    first_comment TEXT,
    images        TEXT NOT NULL,              -- JSON list of {source,url,key,width,height,alt}
    user_tags     TEXT NOT NULL DEFAULT '[]', -- JSON list of {username,x,y}
    collaborators TEXT NOT NULL DEFAULT '[]', -- JSON list of usernames
    sponsors      TEXT NOT NULL DEFAULT '[]', -- JSON list of handles (for reporting)
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    media_id      TEXT,
    permalink     TEXT,
    created_at    TEXT NOT NULL,
    published_at  TEXT
);
CREATE INDEX IF NOT EXISTS posts_due ON posts(status, scheduled_at);

CREATE TABLE IF NOT EXISTS comments (
    id          TEXT PRIMARY KEY,
    media_id    TEXT NOT NULL,
    parent_id   TEXT,
    username    TEXT,
    text        TEXT,
    timestamp   TEXT,
    like_count  INTEGER DEFAULT 0,
    hidden      INTEGER DEFAULT 0,
    replied     INTEGER DEFAULT 0,             -- we (the account) have replied
    handled     INTEGER DEFAULT 0,             -- moderation/auto-reply rules already applied
    permalink   TEXT,
    synced_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS comments_media ON comments(media_id);

CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


@dataclass
class Post:
    id: int
    seq: int
    scheduled_at: datetime
    status: str
    caption: str
    first_comment: str | None
    images: list[dict]
    user_tags: list[dict]
    collaborators: list[str]
    sponsors: list[str]
    attempts: int
    last_error: str | None
    media_id: str | None
    permalink: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Post":
        return cls(
            id=row["id"],
            seq=row["seq"],
            scheduled_at=datetime.fromisoformat(row["scheduled_at"]),
            status=row["status"],
            caption=row["caption"],
            first_comment=row["first_comment"],
            images=json.loads(row["images"]),
            user_tags=json.loads(row["user_tags"]),
            collaborators=json.loads(row["collaborators"]),
            sponsors=json.loads(row["sponsors"]),
            attempts=row["attempts"],
            last_error=row["last_error"],
            media_id=row["media_id"],
            permalink=row["permalink"],
        )


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # -- posts -----------------------------------------------------------------------------
    def next_seq(self) -> int:
        row = self.conn.execute("SELECT COALESCE(MAX(seq), 0) FROM posts WHERE status != 'cancelled'").fetchone()
        return row[0] + 1

    def add_post(
        self,
        seq: int,
        scheduled_at: datetime,
        caption: str,
        first_comment: str | None,
        images: list[dict],
        user_tags: list[dict],
        collaborators: list[str],
        sponsors: list[str],
    ) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO posts (seq, scheduled_at, caption, first_comment, images, user_tags, collaborators,"
                " sponsors, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    seq, iso(scheduled_at), caption, first_comment, json.dumps(images), json.dumps(user_tags),
                    json.dumps(collaborators), json.dumps(sponsors), iso(utcnow()),
                ),
            )
        return cur.lastrowid

    def get_post(self, post_id: int) -> Post | None:
        row = self.conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
        return Post.from_row(row) if row else None

    def list_posts(self, statuses: list[str] | None = None) -> list[Post]:
        sql = "SELECT * FROM posts"
        args: list = []
        if statuses:
            sql += f" WHERE status IN ({','.join('?' * len(statuses))})"
            args = statuses
        sql += " ORDER BY scheduled_at"
        return [Post.from_row(r) for r in self.conn.execute(sql, args)]

    def due_posts(self, now: datetime) -> list[Post]:
        rows = self.conn.execute(
            "SELECT * FROM posts WHERE status = 'scheduled' AND scheduled_at <= ? ORDER BY scheduled_at",
            (iso(now),),
        )
        return [Post.from_row(r) for r in rows]

    def claim(self, post_id: int) -> bool:
        """Atomically move a post to 'publishing' so two runners never double-post."""
        with self.conn:
            cur = self.conn.execute(
                "UPDATE posts SET status = 'publishing', attempts = attempts + 1 WHERE id = ? AND status = 'scheduled'",
                (post_id,),
            )
        return cur.rowcount == 1

    def update_post(self, post_id: int, **fields) -> None:
        for key in ("images", "user_tags", "collaborators", "sponsors"):
            if key in fields:
                fields[key] = json.dumps(fields[key])
        if "scheduled_at" in fields:
            fields["scheduled_at"] = iso(fields["scheduled_at"])
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.conn:
            self.conn.execute(f"UPDATE posts SET {cols} WHERE id = ?", (*fields.values(), post_id))

    # -- comments --------------------------------------------------------------------------
    def upsert_comment(self, c: dict, media_id: str, parent_id: str | None, permalink: str | None) -> bool:
        """Insert or refresh a comment. Returns True when the comment is new."""
        existing = self.conn.execute("SELECT 1 FROM comments WHERE id = ?", (c["id"],)).fetchone()
        with self.conn:
            if existing:
                self.conn.execute(
                    "UPDATE comments SET text = ?, like_count = ?, hidden = ?, synced_at = ? WHERE id = ?",
                    (c.get("text"), c.get("like_count", 0), int(bool(c.get("hidden"))), iso(utcnow()), c["id"]),
                )
            else:
                self.conn.execute(
                    "INSERT INTO comments (id, media_id, parent_id, username, text, timestamp, like_count, hidden,"
                    " permalink, synced_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        c["id"], media_id, parent_id, c.get("username"), c.get("text"), c.get("timestamp"),
                        c.get("like_count", 0), int(bool(c.get("hidden"))), permalink, iso(utcnow()),
                    ),
                )
        return not existing

    def set_comment(self, comment_id: str, **fields) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.conn:
            self.conn.execute(f"UPDATE comments SET {cols} WHERE id = ?", (*fields.values(), comment_id))

    def delete_comment(self, comment_id: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM comments WHERE id = ? OR parent_id = ?", (comment_id, comment_id))

    def get_comment(self, comment_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM comments WHERE id = ?", (comment_id,)).fetchone()

    def list_comments(
        self, unanswered: bool = False, include_hidden: bool = False, media_id: str | None = None, limit: int = 50
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM comments WHERE parent_id IS NULL"
        args: list = []
        if unanswered:
            sql += " AND replied = 0"
        if not include_hidden:
            sql += " AND hidden = 0"
        if media_id:
            sql += " AND media_id = ?"
            args.append(media_id)
        sql += " ORDER BY timestamp DESC LIMIT ?"
        args.append(limit)
        return list(self.conn.execute(sql, args))

    # -- state -----------------------------------------------------------------------------
    def get_state(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_state(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)", (key, value))
