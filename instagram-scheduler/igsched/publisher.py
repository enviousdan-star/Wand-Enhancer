"""Turns scheduled posts into published Instagram media."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from .db import Database, Post, iso, utcnow
from .graph import GraphClient, GraphError

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
RETRY_DELAY = timedelta(minutes=5)


def publish_post(client: GraphClient, post: Post) -> tuple[str, str | None]:
    """Publish ``post`` and return ``(media_id, permalink)``."""
    images = post.images
    if not images:
        raise GraphError("Post has no images")
    if len(images) > 10:
        raise GraphError("Instagram carousels hold at most 10 items")

    if len(images) == 1:
        container = client.create_image_container(
            images[0]["url"],
            caption=post.caption,
            user_tags=post.user_tags,
            collaborators=post.collaborators,
            alt_text=images[0].get("alt"),
        )
    else:
        children = []
        for i, img in enumerate(images):
            children.append(
                client.create_image_container(
                    img["url"],
                    user_tags=post.user_tags if i == 0 else None,  # sponsor tags on the cover image
                    alt_text=img.get("alt"),
                    carousel_item=True,
                )
            )
        for child in children:
            client.wait_until_ready(child)
        container = client.create_carousel_container(children, post.caption, post.collaborators)

    client.wait_until_ready(container)
    media_id = client.publish_container(container)

    if post.first_comment:
        try:
            client.comment_on_media(media_id, post.first_comment)
        except GraphError as exc:  # the post itself is live; don't fail it over the hashtag comment
            log.warning("Post %s published but first comment failed: %s", post.id, exc)

    permalink = None
    try:
        permalink = client.media(media_id).get("permalink")
    except GraphError:
        pass
    return media_id, permalink


def run_due(db: Database, client: GraphClient, now: datetime | None = None) -> list[tuple[int, str]]:
    """Publish every due post. Returns a list of ``(post_id, outcome)``."""
    now = now or utcnow()
    results = []
    for post in db.due_posts(now):
        if not db.claim(post.id):
            continue
        attempts = post.attempts + 1
        try:
            media_id, permalink = publish_post(client, post)
        except GraphError as exc:
            retry = exc.transient and attempts < MAX_ATTEMPTS
            db.update_post(
                post.id,
                status="scheduled" if retry else "failed",
                last_error=str(exc),
                **({"scheduled_at": now + RETRY_DELAY} if retry else {}),
            )
            outcome = f"{'retrying in 5 min' if retry else 'failed'}: {exc}"
            log.error("Post %s %s", post.id, outcome)
            results.append((post.id, outcome))
            continue
        db.update_post(
            post.id, status="published", media_id=media_id, permalink=permalink,
            published_at=iso(utcnow()), last_error=None,
        )
        log.info("Post %s published: %s", post.id, permalink or media_id)
        results.append((post.id, f"published {permalink or media_id}"))
    return results


def recover_stuck(db: Database, older_than: timedelta = timedelta(minutes=30)) -> None:
    """A crash mid-publish leaves a post in 'publishing'. Surface it instead of silently re-posting."""
    cutoff = utcnow() - older_than
    for post in db.list_posts(["publishing"]):
        if post.scheduled_at <= cutoff:
            db.update_post(
                post.id, status="failed",
                last_error="Interrupted while publishing; check Instagram, then `retry` if it did not go out.",
            )
