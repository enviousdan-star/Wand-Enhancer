import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from igsched.config import parse_config  # noqa: E402


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("IG_ACCESS_TOKEN", "token")
    monkeypatch.setenv("IG_USER_ID", "1789")
    raw = tomllib.loads((ROOT / "config.example.toml").read_text())
    raw["database"] = str(tmp_path / "test.db")
    raw["hosting"]["public_dir"] = str(tmp_path / "public")
    return parse_config(raw, tmp_path)


class FakeGraph:
    """Records calls and returns canned Graph API results."""

    def __init__(self):
        self.calls = []
        self.n = 0
        self.fail_publish = None
        self.comments_by_media = {}

    def _id(self, prefix):
        self.n += 1
        return f"{prefix}{self.n}"

    def create_image_container(self, image_url, **kw):
        self.calls.append(("image", image_url, kw))
        return self._id("c")

    def create_carousel_container(self, children, caption, collaborators=None):
        self.calls.append(("carousel", children, caption, collaborators))
        return self._id("car")

    def wait_until_ready(self, cid):
        self.calls.append(("wait", cid))

    def publish_container(self, cid):
        if self.fail_publish:
            raise self.fail_publish
        self.calls.append(("publish", cid))
        return self._id("m")

    def comment_on_media(self, media_id, message):
        self.calls.append(("first_comment", media_id, message))
        return self._id("fc")

    def media(self, media_id, fields=""):
        return {"id": media_id, "permalink": f"https://instagram.com/p/{media_id}"}

    def recent_media(self, limit=25):
        return [{"id": mid, "permalink": f"https://instagram.com/p/{mid}", "comments_count": len(cs)}
                for mid, cs in self.comments_by_media.items()]

    def comments(self, media_id):
        return self.comments_by_media[media_id]

    def reply(self, comment_id, message):
        self.calls.append(("reply", comment_id, message))
        return self._id("r")

    def set_hidden(self, comment_id, hidden):
        self.calls.append(("hide", comment_id, hidden))
        for cs in self.comments_by_media.values():
            for c in cs:
                if c["id"] == comment_id:
                    c["hidden"] = hidden

    def delete_comment(self, comment_id):
        self.calls.append(("delete", comment_id))


@pytest.fixture
def fake_graph():
    return FakeGraph()
