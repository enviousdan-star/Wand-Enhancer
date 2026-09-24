"""Make prepared images reachable at a public HTTPS URL.

Instagram's API does not accept file uploads for feed images: it downloads the JPEG from an
``image_url`` at publish time. So each prepared image must live somewhere public until the
post is published.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from .config import HostingConfig


class HostingError(Exception):
    pass


def object_name(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:24] + ".jpg"


def store_image(cfg: HostingConfig, data: bytes) -> tuple[str, str]:
    """Store ``data`` and return ``(storage_key, public_url)``."""
    name = object_name(data)
    if cfg.mode == "local":
        if not cfg.public_base_url:
            raise HostingError("hosting.public_base_url must be set (the public URL that serves public_dir)")
        cfg.public_dir.mkdir(parents=True, exist_ok=True)
        target = cfg.public_dir / name
        if not target.exists():
            target.write_bytes(data)
        return str(target), f"{cfg.public_base_url}/{name}"

    client = _s3_client(cfg)
    key = f"{cfg.s3_prefix}{name}"
    client.put_object(
        Bucket=cfg.s3_bucket, Key=key, Body=data, ContentType="image/jpeg",
        CacheControl="public, max-age=604800",
    )
    if cfg.public_base_url:
        url = f"{cfg.public_base_url}/{key}"
    else:
        url = client.generate_presigned_url(
            "get_object", Params={"Bucket": cfg.s3_bucket, "Key": key}, ExpiresIn=7 * 24 * 3600
        )
    return key, url


def remove_image(cfg: HostingConfig, storage_key: str) -> None:
    if cfg.mode == "local":
        Path(storage_key).unlink(missing_ok=True)
    else:
        _s3_client(cfg).delete_object(Bucket=cfg.s3_bucket, Key=storage_key)


def _s3_client(cfg: HostingConfig):
    if not cfg.s3_bucket:
        raise HostingError("hosting.s3_bucket must be set when hosting.mode = 's3'")
    try:
        import boto3
    except ImportError as exc:
        raise HostingError("hosting.mode = 's3' needs boto3: pip install boto3") from exc
    kwargs = {}
    if cfg.s3_endpoint_url:
        kwargs["endpoint_url"] = cfg.s3_endpoint_url
    if cfg.s3_region:
        kwargs["region_name"] = cfg.s3_region
    return boto3.client("s3", **kwargs)
