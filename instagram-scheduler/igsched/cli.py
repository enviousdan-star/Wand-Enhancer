"""Command line interface: ``python -m igsched <command>``."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import captions as cap
from .comments import sync_comments
from .config import Config, ConfigError, load_config
from .db import Database, Post, iso, utcnow
from .graph import GraphClient, GraphError
from .hosting import HostingError, store_image
from .images import clamp_ratio, parse_ratio, prepare_image, source_ratio
from .publisher import recover_stuck, run_due

log = logging.getLogger("igsched")


# -- helpers --------------------------------------------------------------------------------
def make_client(config: Config) -> GraphClient:
    a = config.account
    return GraphClient(a.ig_user_id, a.access_token, a.api_host, a.api_version)


def parse_when(value: str, config: Config) -> datetime:
    value = value.strip()
    if value.lower() == "now":
        return utcnow()
    try:
        dt = datetime.fromisoformat(value.replace(" ", "T", 1) if " " in value else value)
    except ValueError:
        raise SystemExit(f"Can't parse time {value!r}. Use 'YYYY-MM-DD HH:MM' (in {config.account.timezone}).")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=config.account.tz)
    return dt


def local(dt: datetime, config: Config) -> str:
    return dt.astimezone(config.account.tz).strftime("%a %Y-%m-%d %H:%M %Z")


def split_csv(value: str | None) -> list[str] | None:
    if value is None:
        return None
    return [v.strip() for v in value.split(",") if v.strip()]


def print_post(post: Post, config: Config, full: bool = False) -> None:
    status = post.status.upper()
    print(f"#{post.id:<4} {status:<10} {local(post.scheduled_at, config)}  "
          f"{len(post.images)} image(s)  sponsors: {', '.join('@' + s for s in post.sponsors) or '-'}")
    if full:
        print("-" * 60)
        print(post.caption)
        if post.first_comment:
            print("\n[first comment]\n" + post.first_comment)
        print("-" * 60)
        for img in post.images:
            print(f"  {img['width']}x{img['height']}  {img['url']}  <- {img['source']}")
        if post.user_tags:
            print("  photo tags: " + ", ".join(f"@{t['username']} ({t['x']:.2f},{t['y']:.2f})" for t in post.user_tags))
        if post.collaborators:
            print("  collaborators: " + ", ".join("@" + c for c in post.collaborators))
        if post.permalink:
            print(f"  live: {post.permalink}")
        if post.last_error:
            print(f"  last error: {post.last_error}")
    elif post.last_error and post.status in ("failed", "scheduled"):
        print(f"      ! {post.last_error}")


# -- commands -------------------------------------------------------------------------------
def cmd_check(args, config: Config) -> int:
    client = make_client(config)
    acct = client.account()
    print(f"Connected as @{acct.get('username')} (id {acct.get('id')}), "
          f"{acct.get('followers_count', '?')} followers, {acct.get('media_count', '?')} posts")
    if config.account.username and acct.get("username", "").lower() != config.account.username.lower():
        print(f"WARNING: config says @{config.account.username} but the token belongs to @{acct.get('username')}")
    try:
        limit = client.publishing_limit()
        quota = limit.get("config", {}).get("quota_total", "?")
        print(f"Publishing quota used in the last 24h: {limit.get('quota_usage', '?')}/{quota}")
    except GraphError as exc:
        print(f"(could not read publishing limit: {exc})")
    return 0


def cmd_prep_image(args, config: Config) -> int:
    ratio = parse_ratio(args.ratio)
    prepared = prepare_image(args.image, config.images, ratio)
    out = Path(args.output or Path(args.image).with_suffix(".instagram.jpg"))
    out.write_bytes(prepared.data)
    print(f"{out}: {prepared.width}x{prepared.height}, JPEG q{prepared.quality}, "
          f"{len(prepared.data) / 1_000_000:.2f} MB")
    for w in prepared.warnings:
        print(f"  ! {w}")
    return 0


def cmd_schedule(args, config: Config, db: Database) -> int:
    if len(args.image) > 10:
        raise SystemExit("Instagram carousels hold at most 10 images")
    body = Path(args.caption_file).read_text(encoding="utf-8") if args.caption_file else (args.caption or "")
    when = parse_when(args.at, config)
    if when < utcnow() - timedelta(minutes=1) and not args.dry_run:
        raise SystemExit(f"{local(when, config)} is in the past. Use --at now to post immediately.")

    seq = db.next_seq()
    sponsors = cap.select_sponsors(config, seq, args.sponsors)
    hashtag_sets = [] if args.hashtags == "none" else split_csv(args.hashtags)
    hashtags = cap.resolve_hashtags(config, hashtag_sets, split_csv(args.tag))
    composed = cap.compose_caption(config, body, sponsors, hashtags)

    # Carousel items are displayed at the cover's aspect ratio, so fit them all to it.
    ratio = parse_ratio(args.ratio)
    if ratio is None and len(args.image) > 1:
        ratio = clamp_ratio(source_ratio(args.image[0]))

    alts = args.alt or []
    images, warnings = [], list(composed.warnings)
    for i, path in enumerate(args.image):
        prepared = prepare_image(path, config.images, ratio)
        warnings.extend(prepared.warnings)
        entry = {
            "source": str(Path(path).resolve()),
            "width": prepared.width,
            "height": prepared.height,
            "alt": alts[i] if i < len(alts) else None,
        }
        if not args.dry_run:
            entry["key"], entry["url"] = store_image(config.hosting, prepared.data)
        else:
            entry["key"], entry["url"] = "", "(dry run)"
        images.append(entry)

    user_tags = cap.photo_tags(sponsors)
    collaborators = cap.collaborators(sponsors)
    handles = [s.handle for s in sponsors]
    if args.dry_run:
        post = Post(0, seq, when, "dry-run", composed.caption, composed.first_comment, images, user_tags,
                    collaborators, handles, 0, None, None, None)
    else:
        post_id = db.add_post(seq, when, composed.caption, composed.first_comment, images, user_tags,
                              collaborators, handles)
        post = db.get_post(post_id)
    print_post(post, config, full=True)
    for w in warnings:
        print(f"  ! {w}")
    if not args.dry_run:
        print(f"Scheduled post #{post.id}. Keep `python -m igsched run` going (or cron `run --once`) to publish it.")
    return 0


def cmd_list(args, config: Config, db: Database) -> int:
    statuses = None if args.all else ["scheduled", "publishing", "failed"]
    posts = db.list_posts(statuses)
    if not posts:
        print("Nothing scheduled." if not args.all else "No posts yet.")
    for post in posts:
        print_post(post, config)
    return 0


def _post_or_exit(db: Database, post_id: int) -> Post:
    post = db.get_post(post_id)
    if not post:
        raise SystemExit(f"No post #{post_id}")
    return post


def cmd_show(args, config: Config, db: Database) -> int:
    print_post(_post_or_exit(db, args.id), config, full=True)
    return 0


def cmd_cancel(args, config: Config, db: Database) -> int:
    post = _post_or_exit(db, args.id)
    if post.status not in ("scheduled", "failed"):
        raise SystemExit(f"Post #{post.id} is {post.status}; only scheduled/failed posts can be cancelled")
    db.update_post(post.id, status="cancelled")
    print(f"Cancelled post #{post.id}")
    return 0


def cmd_reschedule(args, config: Config, db: Database) -> int:
    post = _post_or_exit(db, args.id)
    if post.status not in ("scheduled", "failed"):
        raise SystemExit(f"Post #{post.id} is {post.status}")
    when = parse_when(args.at, config)
    db.update_post(post.id, scheduled_at=when, status="scheduled", attempts=0, last_error=None)
    print(f"Post #{post.id} now scheduled for {local(when, config)}")
    return 0


def cmd_publish_now(args, config: Config, db: Database) -> int:
    post = _post_or_exit(db, args.id)
    if post.status not in ("scheduled", "failed"):
        raise SystemExit(f"Post #{post.id} is {post.status}")
    db.update_post(post.id, status="scheduled", scheduled_at=utcnow())
    for post_id, outcome in run_due(db, make_client(config)):
        print(f"#{post_id}: {outcome}")
    return 0


def cmd_run(args, config: Config, db: Database) -> int:
    client = make_client(config)
    recover_stuck(db)
    comment_every = timedelta(minutes=config.comments.sync_every_minutes)
    while True:
        for post_id, outcome in run_due(db, client):
            print(f"[{local(utcnow(), config)}] #{post_id}: {outcome}", flush=True)

        last = db.get_state("comments_synced_at")
        if not args.no_comments and (not last or utcnow() - datetime.fromisoformat(last) >= comment_every):
            try:
                report = sync_comments(db, client, config)
                db.set_state("comments_synced_at", iso(utcnow()))
                if report.new_comments or report.hidden or report.auto_replied:
                    print(f"[{local(utcnow(), config)}] comments: {report.new_comments} new, "
                          f"{len(report.hidden)} hidden, {len(report.auto_replied)} auto-replied", flush=True)
                for err in report.errors:
                    log.warning("comment sync: %s", err)
            except GraphError as exc:
                log.warning("comment sync failed: %s", exc)

        if args.once:
            return 0
        time.sleep(args.interval)


def cmd_sponsors(args, config: Config, db: Database) -> int:
    if not config.sponsors:
        print("No sponsors configured. Add [[sponsors]] entries to config.toml.")
        return 0
    for s in config.sponsors:
        cadence = f"every {s.every_n} posts" if s.cadence == "every_n" else s.cadence
        extras = [x for x, on in (("photo tag", s.tag_in_photo), ("caption @", s.mention_in_caption),
                                  ("collaborator", s.collaborator)) if on]
        print(f"@{s.handle:<24} {cadence:<16} {', '.join(extras)}")
    start = db.next_seq()
    print(f"\nAutomatic sponsor plan for the next {args.next} posts:")
    for seq in range(start, start + args.next):
        picked = cap.select_sponsors(config, seq, "auto")
        print(f"  post {seq}: {' '.join('@' + s.handle for s in picked) or '-'}")
    return 0


def cmd_hashtags(args, config: Config, db: Database) -> int:
    for name, tags in config.hashtag_sets.items():
        default = " (default)" if name in config.captions.default_hashtag_sets else ""
        print(f"[{name}]{default} {len(tags)} tags\n  {' '.join(tags)}")
    return 0


def cmd_comments(args, config: Config, db: Database) -> int:
    if args.action == "list":
        rows = db.list_comments(unanswered=args.unanswered, include_hidden=args.hidden, limit=args.limit)
        if not rows:
            print("No comments. Run `comments sync` first." if not args.unanswered else "Nothing unanswered.")
        for r in rows:
            flags = "".join(f for f, on in (("H", r["hidden"]), ("R", r["replied"])) if on)
            print(f"{r['id']}  [{flags:<2}] @{r['username']} ({(r['timestamp'] or '')[:16]}): {r['text']}")
            print(f"    {r['permalink'] or r['media_id']}")
        return 0

    client = make_client(config)
    if args.action == "sync":
        report = sync_comments(db, client, config, apply_rules=not args.no_rules)
        db.set_state("comments_synced_at", iso(utcnow()))
        print(f"Checked {report.media} posts: {report.new_comments} new comments, "
              f"{len(report.hidden)} hidden by keyword, {len(report.auto_replied)} auto-replied")
        for err in report.errors:
            print(f"  ! {err}")
    elif args.action == "reply":
        reply_id = client.reply(args.comment_id, args.message)
        db.set_comment(args.comment_id, replied=1, handled=1)
        print(f"Replied ({reply_id})")
    elif args.action in ("hide", "unhide"):
        client.set_hidden(args.comment_id, args.action == "hide")
        db.set_comment(args.comment_id, hidden=int(args.action == "hide"), handled=1)
        print(f"{args.action.capitalize()} ok")
    elif args.action == "delete":
        client.delete_comment(args.comment_id)
        db.delete_comment(args.comment_id)
        print("Deleted")
    return 0


# -- parser ---------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="igsched", description="Schedule Instagram posts and manage comments.")
    p.add_argument("-c", "--config", default="config.toml", help="path to config.toml (default: ./config.toml)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="verify the access token and show publishing quota")

    s = sub.add_parser("prep-image", help="preview the high-res JPEG that would be uploaded")
    s.add_argument("image")
    s.add_argument("-o", "--output")
    s.add_argument("--ratio", help="portrait (4:5) | square | landscape (1.91:1) | W:H | auto")

    s = sub.add_parser("schedule", help="schedule a photo or carousel post")
    s.add_argument("-i", "--image", action="append", required=True, help="image path; repeat for a carousel (max 10)")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--caption", help="caption text")
    g.add_argument("--caption-file", help="read caption from a UTF-8 text file")
    s.add_argument("--at", required=True, help="'YYYY-MM-DD HH:MM' in the configured timezone, or 'now'")
    s.add_argument("--hashtags", help="comma separated hashtag sets from config, or 'none' (default: captions.default_hashtag_sets)")
    s.add_argument("--tag", help="extra hashtags, comma separated")
    s.add_argument("--sponsors", default="auto",
                   help="'auto' (config cadence), 'none', or handles: 'acme,bolt' / 'auto,guestsponsor'")
    s.add_argument("--ratio", help="force aspect ratio for all images: portrait|square|landscape|W:H")
    s.add_argument("--alt", action="append", help="alt text, one per --image in order")
    s.add_argument("--dry-run", action="store_true", help="show the result without saving or uploading")

    s = sub.add_parser("list", help="list upcoming and failed posts")
    s.add_argument("--all", action="store_true", help="include published and cancelled posts")

    for name, help_ in (("show", "show a post in full"), ("cancel", "cancel a scheduled post"),
                        ("publish-now", "publish a scheduled or failed post immediately")):
        s = sub.add_parser(name, help=help_)
        s.add_argument("id", type=int)
    s = sub.add_parser("reschedule", help="move a post (also re-queues a failed post)")
    s.add_argument("id", type=int)
    s.add_argument("--at", required=True)

    s = sub.add_parser("run", help="publish due posts and sync comments (runs forever unless --once)")
    s.add_argument("--once", action="store_true", help="single pass, for cron / Task Scheduler")
    s.add_argument("--interval", type=int, default=60, help="seconds between checks (default 60)")
    s.add_argument("--no-comments", action="store_true", help="don't sync/moderate comments")

    s = sub.add_parser("sponsors", help="show sponsors and the upcoming automatic rotation")
    s.add_argument("--next", type=int, default=6)
    sub.add_parser("hashtags", help="show configured hashtag sets")

    c = sub.add_parser("comments", help="sync, list and moderate comments")
    csub = c.add_subparsers(dest="action", required=True)
    s = csub.add_parser("sync", help="fetch comments from recent posts and apply hide/auto-reply rules")
    s.add_argument("--no-rules", action="store_true")
    s = csub.add_parser("list", help="list synced comments")
    s.add_argument("--unanswered", action="store_true")
    s.add_argument("--hidden", action="store_true", help="include hidden comments")
    s.add_argument("--limit", type=int, default=50)
    s = csub.add_parser("reply")
    s.add_argument("comment_id")
    s.add_argument("message")
    for name in ("hide", "unhide", "delete"):
        csub.add_parser(name).add_argument("comment_id")
    return p


COMMANDS = {
    "schedule": cmd_schedule, "list": cmd_list, "show": cmd_show, "cancel": cmd_cancel,
    "reschedule": cmd_reschedule, "publish-now": cmd_publish_now, "run": cmd_run,
    "sponsors": cmd_sponsors, "hashtags": cmd_hashtags, "comments": cmd_comments,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")
    try:
        config = load_config(args.config)
        if args.command == "check":
            return cmd_check(args, config)
        if args.command == "prep-image":
            return cmd_prep_image(args, config)
        db = Database(config.db_path)
        try:
            return COMMANDS[args.command](args, config, db)
        finally:
            db.close()
    except (ConfigError, GraphError, HostingError, KeyError, ValueError, OSError) as exc:
        msg = exc.args[0] if isinstance(exc, KeyError) and exc.args else exc
        print(f"error: {msg}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
