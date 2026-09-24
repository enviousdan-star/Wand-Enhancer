"""Comment sync + moderation rules (keyword hiding and auto-replies)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .config import Config
from .db import Database
from .graph import GraphClient, GraphError

log = logging.getLogger(__name__)


@dataclass
class SyncReport:
    media: int = 0
    new_comments: int = 0
    hidden: list[str] = field(default_factory=list)
    auto_replied: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def matches(text: str, keywords: list[str]) -> bool:
    text = (text or "").lower()
    return any(k in text for k in keywords)


def sync_comments(db: Database, client: GraphClient, config: Config, apply_rules: bool = True) -> SyncReport:
    report = SyncReport()
    own = config.account.username.lower()
    for media in client.recent_media(config.comments.sync_media_limit):
        report.media += 1
        if not media.get("comments_count"):
            continue
        try:
            comments = client.comments(media["id"])
        except GraphError as exc:
            report.errors.append(f"{media['id']}: {exc}")
            continue
        for c in comments:
            if (c.get("username") or "").lower() == own:
                continue  # our own top-level comments (e.g. first-comment hashtags)
            if db.upsert_comment(c, media["id"], None, media.get("permalink")):
                report.new_comments += 1
            replies = c.get("replies", {}).get("data", [])
            for r in replies:
                db.upsert_comment(r, media["id"], c["id"], media.get("permalink"))
            if any((r.get("username") or "").lower() == own for r in replies):
                db.set_comment(c["id"], replied=1)

            if apply_rules:
                _apply_rules(db, client, config, c, report)
    return report


def _apply_rules(db: Database, client: GraphClient, config: Config, c: dict, report: SyncReport) -> None:
    row = db.get_comment(c["id"])
    if row is None or row["handled"]:
        return
    text = c.get("text") or ""
    try:
        if config.comments.hide_keywords and not c.get("hidden") and matches(text, config.comments.hide_keywords):
            client.set_hidden(c["id"], True)
            db.set_comment(c["id"], hidden=1)
            report.hidden.append(c["id"])
        elif not row["replied"]:
            for rule in config.comments.auto_replies:
                if matches(text, rule.match):
                    reply = rule.reply.replace("{username}", c.get("username") or "")
                    client.reply(c["id"], reply)
                    db.set_comment(c["id"], replied=1)
                    report.auto_replied.append(c["id"])
                    break
    except GraphError as exc:
        report.errors.append(f"{c['id']}: {exc}")
        return  # leave unhandled so the next sync retries
    db.set_comment(c["id"], handled=1)
