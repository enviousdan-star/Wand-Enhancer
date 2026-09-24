import json

import pytest

from igsched.graph import GraphClient, GraphError


class Resp:
    def __init__(self, status, payload):
        self.status_code, self._payload, self.text = status, payload, json.dumps(payload)

    def json(self):
        return self._payload


class Session:
    def __init__(self, responses):
        self.responses, self.sent = list(responses), []

    def request(self, method, url, params=None, data=None, timeout=None):
        self.sent.append((method, url, params or data))
        return self.responses.pop(0)


def client(responses):
    s = Session(responses)
    return GraphClient("42", "tok", session=s, sleep=lambda _: None), s


def test_image_container_encodes_tags_and_collaborators():
    c, s = client([Resp(200, {"id": "c1"})])
    assert c.create_image_container("https://x/a.jpg", caption="hi",
                                    user_tags=[{"username": "acme", "x": 0.5, "y": 0.9}],
                                    collaborators=["bolt"]) == "c1"
    method, url, data = s.sent[0]
    assert method == "POST" and url == "https://graph.facebook.com/v23.0/42/media"
    assert json.loads(data["user_tags"]) == [{"username": "acme", "x": 0.5, "y": 0.9}]
    assert json.loads(data["collaborators"]) == ["bolt"]
    assert data["access_token"] == "tok" and "alt_text" not in data


def test_transient_errors_are_retried_and_permanent_are_not():
    c, s = client([Resp(500, {"error": {"message": "oops", "code": 2}}), Resp(200, {"id": "m1"})])
    assert c.publish_container("c1") == "m1" and len(s.sent) == 2

    c, s = client([Resp(400, {"error": {"message": "Invalid user", "code": 100, "error_user_msg": "Bad tag"}})])
    with pytest.raises(GraphError, match="Bad tag"):
        c.reply("x", "hi")
    assert len(s.sent) == 1


def test_wait_until_ready_reports_errors():
    c, _ = client([Resp(200, {"status_code": "IN_PROGRESS"}), Resp(200, {"status_code": "FINISHED"})])
    c.wait_until_ready("c1")
    c, _ = client([Resp(200, {"status_code": "ERROR", "status": "Unsupported format"})])
    with pytest.raises(GraphError, match="Unsupported format"):
        c.wait_until_ready("c1")


def test_hide_sends_boolean_string():
    c, s = client([Resp(200, {"success": True})])
    c.set_hidden("cm1", True)
    assert s.sent[0][2]["hide"] == "true"
