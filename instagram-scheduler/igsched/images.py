"""High-resolution image preparation for the Instagram Graph API.

Instagram's publishing API only accepts JPEGs, at most 8 MB, with an aspect ratio between
4:5 (portrait) and 1.91:1 (landscape). Instagram stores feed images up to 1440 px wide, so we
keep that much detail, never upscale, use 4:4:4 chroma (no colour smearing on logos and
sponsor text) and convert to sRGB so colours don't shift after upload.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageCms, ImageFile, ImageOps

from .config import ImageConfig

MIN_RATIO = 4 / 5
MAX_RATIO = 1.91

NAMED_RATIOS = {
    "portrait": 4 / 5,
    "4:5": 4 / 5,
    "square": 1.0,
    "1:1": 1.0,
    "landscape": 1.91,
    "1.91:1": 1.91,
}


@dataclass
class PreparedImage:
    data: bytes
    width: int
    height: int
    quality: int
    source: Path
    warnings: list[str] = field(default_factory=list)

    @property
    def ratio(self) -> float:
        return self.width / self.height


def parse_ratio(value: str | None) -> float | None:
    if not value or value == "auto":
        return None
    if value in NAMED_RATIOS:
        return NAMED_RATIOS[value]
    if ":" in value:
        w, h = value.split(":", 1)
        return clamp_ratio(float(w) / float(h))
    return clamp_ratio(float(value))


def clamp_ratio(ratio: float) -> float:
    return max(MIN_RATIO, min(MAX_RATIO, ratio))


def source_ratio(path: str | Path) -> float:
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        return im.width / im.height


def prepare_image(path: str | Path, cfg: ImageConfig, ratio: float | None = None) -> PreparedImage:
    """Load, orient, colour-manage, fit to an allowed aspect ratio and encode as high-quality JPEG."""
    path = Path(path)
    warnings: list[str] = []
    with Image.open(path) as src:
        src.load()
        im = ImageOps.exif_transpose(src)
        im = _to_srgb(im, cfg.pad_color)

    original_ratio = im.width / im.height
    target_ratio = clamp_ratio(ratio if ratio else original_ratio)
    fitted = _fit_ratio(im, target_ratio, cfg)
    # Real photo detail across the final image width (padding borders don't count).
    content_width = min(im.width, fitted.width) * min(1.0, cfg.target_width / fitted.width)
    im = fitted
    if abs(target_ratio - original_ratio) > 0.01:
        warnings.append(
            f"{path.name}: aspect ratio adjusted to {target_ratio:.2f} ({cfg.fit}) to fit Instagram's 4:5–1.91:1 range"
        )

    if im.width > cfg.target_width:
        new_h = round(im.height * cfg.target_width / im.width)
        im = im.resize((cfg.target_width, new_h), Image.Resampling.LANCZOS)
    if content_width < cfg.min_width_warning:
        warnings.append(
            f"{path.name}: only ~{content_width:.0f}px of photo width; Instagram displays feed posts at {cfg.min_width_warning}px+, "
            "so this will look soft. Use a larger original if you have one."
        )

    im, data, quality = _encode(im, cfg)
    return PreparedImage(data, im.width, im.height, quality, path, warnings)


def _to_srgb(im: Image.Image, pad_color: str) -> Image.Image:
    icc = im.info.get("icc_profile")
    if im.mode == "CMYK" or icc:
        try:
            src_profile = ImageCms.ImageCmsProfile(io.BytesIO(icc)) if icc else ImageCms.createProfile("sRGB")
            if im.mode == "CMYK" and not icc:
                im = im.convert("RGB")
            else:
                im = ImageCms.profileToProfile(
                    im, src_profile, ImageCms.createProfile("sRGB"),
                    outputMode="RGBA" if "A" in im.getbands() else "RGB",
                )
        except (ImageCms.PyCMSError, OSError, ValueError):
            pass  # Fall back to a plain conversion below.

    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, pad_color)
        bg.paste(im, mask=im.getchannel("A"))
        return bg
    return im.convert("RGB")


def _fit_ratio(im: Image.Image, ratio: float, cfg: ImageConfig) -> Image.Image:
    current = im.width / im.height
    if abs(current - ratio) < 0.005:
        return im
    if cfg.fit == "crop":
        if current > ratio:  # too wide
            new_w = round(im.height * ratio)
            left = (im.width - new_w) // 2
            return im.crop((left, 0, left + new_w, im.height))
        new_h = round(im.width / ratio)
        top = (im.height - new_h) // 2
        return im.crop((0, top, im.width, top + new_h))
    # pad: keep the entire photo (sponsor boards, banners) and add borders
    if current > ratio:
        size = (im.width, round(im.width / ratio))
    else:
        size = (round(im.height * ratio), im.height)
    canvas = Image.new("RGB", size, cfg.pad_color)
    canvas.paste(im, ((size[0] - im.width) // 2, (size[1] - im.height) // 2))
    return canvas


def _save(im: Image.Image, quality: int) -> bytes:
    # Progressive + optimize needs the whole encoded image in one buffer.
    ImageFile.MAXBLOCK = max(ImageFile.MAXBLOCK, im.width * im.height * 4)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality, subsampling=0, optimize=True, progressive=True)
    return buf.getvalue()


def _encode(im: Image.Image, cfg: ImageConfig) -> tuple[Image.Image, bytes, int]:
    quality = cfg.jpeg_quality
    while True:
        data = _save(im, quality)
        if len(data) <= cfg.max_bytes or quality <= cfg.min_jpeg_quality:
            break
        quality -= 3
    if len(data) > cfg.max_bytes:
        # Last resort: shrink dimensions slightly until we fit.
        scale = 0.9
        while len(data) > cfg.max_bytes and im.width > 320:
            im = im.resize((round(im.width * scale), round(im.height * scale)), Image.Resampling.LANCZOS)
            data = _save(im, quality)
    return im, data, quality
