"""Caption assembly: body text + sponsor mentions + hashtag sets, within Instagram's limits."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .config import Config, Sponsor

HASHTAG_RE = re.compile(r"(?<![\w&])#(\w+)", re.UNICODE)
MENTION_RE = re.compile(r"(?<![\w.])@([A-Za-z0-9._]+)")

MAX_USER_TAGS = 20
MAX_COLLABORATORS = 3


@dataclass
class ComposedCaption:
    caption: str
    first_comment: str | None
    sponsors: list[Sponsor]
    hashtags: list[str]
    warnings: list[str] = field(default_factory=list)


def select_sponsors(config: Config, seq: int, requested: str | None) -> list[Sponsor]:
    """Pick the sponsors for the post with sequence number ``seq`` (1-based).

    ``requested``: None/"auto" applies the configured cadences, "none" disables sponsors,
    anything else is a comma separated list of handles (config sponsors or ad-hoc handles).
    "auto,+extra" adds handles on top of the automatic selection.
    """
    requested = (requested or "auto").strip()
    if requested.lower() == "none":
        return []

    parts = [p.strip() for p in requested.split(",") if p.strip()]
    auto = any(p.lower() == "auto" for p in parts)
    explicit = [p.lstrip("+@") for p in parts if p.lower() != "auto"]

    chosen: list[Sponsor] = []
    if auto:
        chosen.extend(_auto_sponsors(config, seq))
    for handle in explicit:
        chosen.append(config.sponsor(handle))

    unique: dict[str, Sponsor] = {}
    for s in chosen:
        unique.setdefault(s.handle.lower(), s)
    return list(unique.values())


def _auto_sponsors(config: Config, seq: int) -> list[Sponsor]:
    picked = []
    for s in config.sponsors:
        if s.cadence == "always":
            picked.append(s)
        elif s.cadence == "every_n" and (seq - 1) % s.every_n == 0:
            picked.append(s)

    rotating = [s for s in config.sponsors if s.cadence == "rotate"]
    slots = min(config.captions.rotation_slots, len(rotating))
    if slots:
        start = ((seq - 1) * slots) % len(rotating)
        picked.extend(rotating[(start + i) % len(rotating)] for i in range(slots))
    return picked


def resolve_hashtags(config: Config, sets: list[str] | None, extra: list[str] | None) -> list[str]:
    names = config.captions.default_hashtag_sets if sets is None else sets
    tags: list[str] = []
    for name in names:
        if name not in config.hashtag_sets:
            raise KeyError(f"Unknown hashtag set {name!r}. Known: {', '.join(sorted(config.hashtag_sets))}")
        tags.extend(config.hashtag_sets[name])
    for t in extra or []:
        t = t.strip().replace(" ", "")
        if t:
            tags.append(t if t.startswith("#") else f"#{t}")
    return tags


def compose_caption(
    config: Config,
    body: str,
    sponsors: list[Sponsor],
    hashtags: list[str],
) -> ComposedCaption:
    warnings: list[str] = []
    body = body.strip()

    # Hashtags already typed into the body count toward Instagram's 30 limit.
    seen = {t.lower() for t in HASHTAG_RE.findall(body)}
    budget = config.captions.max_hashtags - len(seen)
    final_tags: list[str] = []
    for tag in hashtags:
        key = tag.lstrip("#").lower()
        if key in seen:
            continue
        if budget <= 0:
            warnings.append(f"Dropped {tag}: Instagram allows at most {config.captions.max_hashtags} hashtags")
            continue
        seen.add(key)
        final_tags.append(tag)
        budget -= 1

    mentioned = {m.lower() for m in MENTION_RE.findall(body)}
    to_mention = [s for s in sponsors if s.mention_in_caption and s.handle.lower() not in mentioned]
    sponsor_line = ""
    if to_mention:
        handles = " ".join(f"@{s.handle}" for s in to_mention)
        prefix = config.captions.sponsor_line_prefix.strip()
        sponsor_line = f"{prefix} {handles}" if prefix else handles

    tag_block = " ".join(final_tags)
    sections = [p for p in (body, sponsor_line) if p]
    first_comment = None
    if tag_block:
        if config.captions.hashtags_in_first_comment:
            first_comment = tag_block
        else:
            sections.append(tag_block)
    caption = "\n\n".join(sections)

    if len(caption) > config.captions.max_length:
        raise ValueError(
            f"Caption is {len(caption)} characters; Instagram's limit is {config.captions.max_length}."
        )

    total_mentions = len(MENTION_RE.findall(caption))
    if total_mentions > MAX_USER_TAGS:
        warnings.append(f"Caption has {total_mentions} @mentions; Instagram may reject more than {MAX_USER_TAGS}.")

    return ComposedCaption(caption, first_comment, sponsors, final_tags, warnings)


def photo_tags(sponsors: list[Sponsor]) -> list[dict]:
    """Instagram user_tags payload. Positions default to a tidy row along the bottom edge."""
    taggable = [s for s in sponsors if s.tag_in_photo][:MAX_USER_TAGS]
    tags = []
    for i, s in enumerate(taggable):
        x = s.x if s.x is not None else round((i + 1) / (len(taggable) + 1), 3)
        y = s.y if s.y is not None else 0.92
        tags.append({"username": s.handle, "x": _clamp(x), "y": _clamp(y)})
    return tags


def collaborators(sponsors: list[Sponsor]) -> list[str]:
    return [s.handle for s in sponsors if s.collaborator][:MAX_COLLABORATORS]


def _clamp(v: float) -> float:
    return max(0.0, min(1.0, float(v)))
