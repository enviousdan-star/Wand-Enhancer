from igsched import captions as cap


def handles(sponsors):
    return [s.handle for s in sponsors]


def test_auto_sponsor_cadence(config):
    plan = [handles(cap.select_sponsors(config, seq, "auto")) for seq in range(1, 7)]
    assert all("main_sponsor" in p for p in plan)
    # rotate sponsors alternate, one slot per post
    assert [("partner_one" in p, "partner_two" in p) for p in plan[:4]] == [
        (True, False), (False, True), (True, False), (False, True)]
    # every 3rd post, starting with the first
    assert ["venue_partner" in p for p in plan] == [True, False, False, True, False, False]


def test_sponsor_overrides(config):
    assert cap.select_sponsors(config, 1, "none") == []
    assert handles(cap.select_sponsors(config, 2, "@guest,main_sponsor")) == ["guest", "main_sponsor"]
    assert "guest" in handles(cap.select_sponsors(config, 2, "auto,+guest"))


def test_caption_hashtags_and_mentions(config):
    sponsors = cap.select_sponsors(config, 1, "auto")
    tags = cap.resolve_hashtags(config, ["default", "gameday"], ["NYAces", "extra tag"])
    composed = cap.compose_caption(config, "Big win tonight! #gameday", sponsors, tags)
    # #gameday already in body -> not duplicated; #nyaces vs NYAces deduped case-insensitively
    assert composed.caption.count("#gameday") == 1
    assert composed.hashtags == ["#nyacespb", "#nyaces", "#matchday", "#extratag"]
    assert "Thanks to our partners @main_sponsor @partner_one" in composed.caption
    # venue_partner has mention_in_caption = false
    assert "@venue_partner" not in composed.caption
    assert {t["username"] for t in cap.photo_tags(sponsors)} == {"main_sponsor", "partner_one", "venue_partner"}


def test_hashtag_limit_and_first_comment(config):
    config.captions.hashtags_in_first_comment = True
    tags = [f"#t{i}" for i in range(40)]
    composed = cap.compose_caption(config, "Hello #one", [], tags)
    assert composed.caption == "Hello #one"
    assert len(composed.first_comment.split()) == 29
    assert composed.warnings


def test_existing_mention_not_repeated(config):
    s = cap.select_sponsors(config, 2, "main_sponsor")
    composed = cap.compose_caption(config, "Shout out @Main_Sponsor", s, [])
    assert composed.caption == "Shout out @Main_Sponsor"
