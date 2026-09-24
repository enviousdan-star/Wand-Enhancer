import io

from PIL import Image

from igsched.images import prepare_image


def save(tmp_path, size, mode="RGB", color=(200, 30, 30), name="in.png"):
    p = tmp_path / name
    Image.new(mode, size, color).save(p)
    return p


def test_downscales_to_target_and_keeps_ratio(tmp_path, config):
    out = prepare_image(save(tmp_path, (4000, 5000)), config.images)
    assert (out.width, out.height) == (1440, 1800)
    im = Image.open(io.BytesIO(out.data))
    assert im.format == "JPEG" and im.mode == "RGB"
    assert out.quality == 95 and not out.warnings


def test_too_tall_is_padded_to_4x5(tmp_path, config):
    out = prepare_image(save(tmp_path, (1000, 3000)), config.images)
    assert abs(out.width / out.height - 0.8) < 0.01
    assert any("aspect ratio" in w for w in out.warnings)
    assert any("soft" in w for w in out.warnings)  # 1000px < 1080px


def test_crop_mode_and_forced_ratio(tmp_path, config):
    config.images.fit = "crop"
    out = prepare_image(save(tmp_path, (3000, 2000)), config.images, ratio=1.0)
    assert out.width == out.height == 1440


def test_transparency_flattened(tmp_path, config):
    out = prepare_image(save(tmp_path, (2000, 2000), "RGBA", (0, 0, 0, 0)), config.images)
    px = Image.open(io.BytesIO(out.data)).getpixel((10, 10))
    assert all(c > 245 for c in px)  # white pad colour


def test_respects_max_bytes(tmp_path, config):
    import os
    p = tmp_path / "noise.png"
    Image.frombytes("RGB", (2000, 2000), os.urandom(2000 * 2000 * 3)).save(p)
    config.images.max_bytes = 1_000_000
    out = prepare_image(p, config.images)
    assert len(out.data) <= 1_000_000
