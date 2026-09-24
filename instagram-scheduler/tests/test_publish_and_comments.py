from datetime import timedelta

from PIL import Image

from igsched.cli import main
from igsched.comments import sync_comments
from igsched.db import Database, utcnow
from igsched.graph import GraphError
from igsched.publisher import run_due


def add(db, when, images=1, first_comment=None):
    imgs = [{"url": f"https://x/{i}.jpg", "width": 1440, "height": 1800, "alt": None} for i in range(images)]
    return db.add_post(db.next_seq(), when, "cap", first_comment, imgs,
                       [{"username": "main_sponsor", "x": 0.5, "y": 0.9}], ["collab"], ["main_sponsor"])


def test_single_image_publish(config, fake_graph):
    db = Database(config.db_path)
    pid = add(db, utcnow() - timedelta(minutes=1), first_comment="#a #b")
    add(db, utcnow() + timedelta(hours=1))  # not due
    results = run_due(db, fake_graph)
    assert [r[0] for r in results] == [pid]
    kind, url, kw = fake_graph.calls[0]
    assert kind == "image" and kw["caption"] == "cap" and kw["collaborators"] == ["collab"]
    assert kw["user_tags"][0]["username"] == "main_sponsor"
    assert ("first_comment", "m2", "#a #b") in fake_graph.calls
    post = db.get_post(pid)
    assert post.status == "published" and post.permalink.endswith("m2")
    assert run_due(db, fake_graph) == []  # never double-posts


def test_carousel_tags_cover_only(config, fake_graph):
    db = Database(config.db_path)
    add(db, utcnow(), images=3)
    run_due(db, fake_graph)
    images = [c for c in fake_graph.calls if c[0] == "image"]
    assert len(images) == 3 and all(c[2]["carousel_item"] for c in images)
    assert images[0][2]["user_tags"] and images[1][2]["user_tags"] is None
    carousel = next(c for c in fake_graph.calls if c[0] == "carousel")
    assert carousel[1] == ["c1", "c2", "c3"] and carousel[3] == ["collab"]


def test_transient_failure_retries_then_fails(config, fake_graph):
    db = Database(config.db_path)
    pid = add(db, utcnow())
    fake_graph.fail_publish = GraphError("busy", code=2)
    now = utcnow()
    for i in range(3):
        run_due(db, fake_graph, now=now + timedelta(minutes=10 * i))
    post = db.get_post(pid)
    assert post.status == "failed" and post.attempts == 3

    pid2 = add(db, utcnow())
    fake_graph.fail_publish = GraphError("bad image", code=100)
    run_due(db, fake_graph)
    assert db.get_post(pid2).status == "failed"


def test_comment_sync_rules(config, fake_graph):
    db = Database(config.db_path)
    fake_graph.comments_by_media = {"m1": [
        {"id": "a", "text": "Get FREE FOLLOWERS now", "username": "bot"},
        {"id": "b", "text": "Where can I buy tickets?", "username": "fan"},
        {"id": "c", "text": "tickets?", "username": "fan2",
         "replies": {"data": [{"id": "c1", "text": "link in bio", "username": "nyacespb"}]}},
        {"id": "d", "text": "#hashtags", "username": "nyacespb"},
    ]}
    report = sync_comments(db, fake_graph, config)
    assert report.new_comments == 3 and report.hidden == ["a"] and report.auto_replied == ["b"]
    assert ("reply", "b", "Thanks @fan! All the details are at the link in our bio.") in fake_graph.calls
    # second sync doesn't act again
    fake_graph.calls.clear()
    report = sync_comments(db, fake_graph, config)
    assert report.new_comments == 0 and not fake_graph.calls
    assert [r["id"] for r in db.list_comments(unanswered=True)] == []


def test_cli_schedule_end_to_end(config, tmp_path, capsys, monkeypatch):
    cfg = tmp_path / "config.toml"
    from pathlib import Path
    text = (Path(__file__).resolve().parents[1] / "config.example.toml").read_text()
    cfg.write_text(text)
    img1, img2 = tmp_path / "a.jpg", tmp_path / "b.png"
    Image.new("RGB", (3000, 2000), "red").save(img1)
    Image.new("RGB", (2000, 2000), "blue").save(img2)
    rc = main(["-c", str(cfg), "schedule", "-i", str(img1), "-i", str(img2), "--caption", "Game day!",
               "--at", "2099-01-01 18:00", "--hashtags", "default,gameday", "--alt", "Team photo"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "@main_sponsor" in out and "#gameday" in out
    db = Database(tmp_path / "igsched.db")
    post = db.list_posts()[0]
    # carousel items all fitted to the cover's 3:2 ratio at full 1440px width
    assert {(i["width"], i["height"]) for i in post.images} == {(1440, 960)}
    assert post.images[0]["alt"] == "Team photo" and post.images[0]["url"].startswith("https://example.com/ig/")
    assert (tmp_path / "public").exists()
    assert main(["-c", str(cfg), "cancel", str(post.id)]) == 0
    assert db.get_post(post.id).status == "cancelled"
