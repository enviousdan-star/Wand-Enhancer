"""Configuration loading (config.toml) with typed defaults."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo


class ConfigError(Exception):
    pass


@dataclass
class AccountConfig:
    username: str
    ig_user_id: str
    access_token: str
    api_host: str = "graph.facebook.com"
    api_version: str = "v23.0"
    timezone: str = "America/New_York"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@dataclass
class HostingConfig:
    mode: str = "local"  # "local" | "s3"
    public_dir: Path = Path("public")
    public_base_url: str = ""
    s3_bucket: str = ""
    s3_prefix: str = "instagram/"
    s3_endpoint_url: str = ""
    s3_region: str = ""


@dataclass
class ImageConfig:
    target_width: int = 1440
    min_width_warning: int = 1080
    jpeg_quality: int = 95
    min_jpeg_quality: int = 82
    max_bytes: int = 8 * 1024 * 1024
    fit: str = "pad"  # "pad" keeps the whole photo, "crop" center-crops
    pad_color: str = "#ffffff"


@dataclass
class Sponsor:
    handle: str
    name: str = ""
    # "always"   -> every post
    # "every_n"  -> every Nth post (see every_n)
    # "rotate"   -> shares the rotation slots with the other rotating sponsors
    # "manual"   -> only when requested explicitly with --sponsors
    cadence: str = "always"
    every_n: int = 1
    tag_in_photo: bool = True
    mention_in_caption: bool = True
    collaborator: bool = False
    x: float | None = None
    y: float | None = None

    @property
    def label(self) -> str:
        return self.name or self.handle


@dataclass
class CaptionConfig:
    max_hashtags: int = 30
    max_length: int = 2200
    hashtags_in_first_comment: bool = False
    sponsor_line_prefix: str = "Thanks to our partners"
    default_hashtag_sets: list[str] = field(default_factory=lambda: ["default"])
    rotation_slots: int = 1


@dataclass
class AutoReply:
    match: list[str]
    reply: str


@dataclass
class CommentConfig:
    hide_keywords: list[str] = field(default_factory=list)
    auto_replies: list[AutoReply] = field(default_factory=list)
    sync_media_limit: int = 25
    sync_every_minutes: int = 15


@dataclass
class Config:
    account: AccountConfig
    hosting: HostingConfig
    images: ImageConfig
    captions: CaptionConfig
    hashtag_sets: dict[str, list[str]]
    sponsors: list[Sponsor]
    comments: CommentConfig
    db_path: Path
    base_dir: Path

    def sponsor(self, handle: str) -> Sponsor:
        key = handle.lstrip("@").lower()
        for s in self.sponsors:
            if s.handle.lower() == key:
                return s
        # Ad-hoc sponsor that isn't in the config: tag + mention only.
        return Sponsor(handle=key, cadence="manual")


def _resolve(base: Path, value: str | os.PathLike) -> Path:
    p = Path(value).expanduser()
    return p if p.is_absolute() else (base / p)


def load_config(path: str | os.PathLike) -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError(
            f"Config file not found: {path}. Copy config.example.toml to config.toml and fill it in."
        )
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    return parse_config(raw, path.parent.resolve())


def parse_config(raw: dict, base_dir: Path) -> Config:
    acc = raw.get("account", {})
    token_env = acc.get("access_token_env", "IG_ACCESS_TOKEN")
    token = os.environ.get(token_env) or acc.get("access_token", "")
    ig_user_id = os.environ.get("IG_USER_ID") or str(acc.get("ig_user_id", ""))
    account = AccountConfig(
        username=acc.get("username", "").lstrip("@"),
        ig_user_id=ig_user_id,
        access_token=token,
        api_host=acc.get("api_host", "graph.facebook.com"),
        api_version=acc.get("api_version", "v23.0"),
        timezone=acc.get("timezone", "America/New_York"),
    )
    try:
        account.tz
    except Exception as exc:  # ZoneInfoNotFoundError subclasses KeyError
        raise ConfigError(f"Unknown timezone {account.timezone!r}") from exc

    h = raw.get("hosting", {})
    hosting = HostingConfig(
        mode=h.get("mode", "local"),
        public_dir=_resolve(base_dir, h.get("public_dir", "public")),
        public_base_url=h.get("public_base_url", "").rstrip("/"),
        s3_bucket=h.get("s3_bucket", ""),
        s3_prefix=h.get("s3_prefix", "instagram/"),
        s3_endpoint_url=h.get("s3_endpoint_url", ""),
        s3_region=h.get("s3_region", ""),
    )
    if hosting.mode not in ("local", "s3"):
        raise ConfigError("hosting.mode must be 'local' or 's3'")

    images = ImageConfig(**raw.get("images", {}))
    if images.fit not in ("pad", "crop"):
        raise ConfigError("images.fit must be 'pad' or 'crop'")

    captions = CaptionConfig(**raw.get("captions", {}))

    hashtag_sets = {
        name: [_normalize_tag(t) for t in (spec.get("tags", []) if isinstance(spec, dict) else spec)]
        for name, spec in raw.get("hashtags", {}).items()
    }

    sponsors = []
    for s in raw.get("sponsors", []):
        sponsor = Sponsor(**{**s, "handle": s["handle"].lstrip("@")})
        if sponsor.cadence not in ("always", "every_n", "rotate", "manual"):
            raise ConfigError(f"Sponsor {sponsor.handle}: unknown cadence {sponsor.cadence!r}")
        if sponsor.every_n < 1:
            raise ConfigError(f"Sponsor {sponsor.handle}: every_n must be >= 1")
        sponsors.append(sponsor)

    c = raw.get("comments", {})
    comments = CommentConfig(
        hide_keywords=[k.lower() for k in c.get("hide_keywords", [])],
        auto_replies=[
            AutoReply(match=[m.lower() for m in _as_list(r["match"])], reply=r["reply"])
            for r in c.get("auto_replies", [])
        ],
        sync_media_limit=c.get("sync_media_limit", 25),
        sync_every_minutes=c.get("sync_every_minutes", 15),
    )

    return Config(
        account=account,
        hosting=hosting,
        images=images,
        captions=captions,
        hashtag_sets=hashtag_sets,
        sponsors=sponsors,
        comments=comments,
        db_path=_resolve(base_dir, raw.get("database", "igsched.db")),
        base_dir=base_dir,
    )


def _as_list(value) -> list[str]:
    return [value] if isinstance(value, str) else list(value)


def _normalize_tag(tag: str) -> str:
    tag = tag.strip().replace(" ", "")
    return tag if tag.startswith("#") else f"#{tag}"
