"""Thin Instagram Graph API client (content publishing + comment moderation)."""

from __future__ import annotations

import json
import time
from typing import Any, Callable

import requests

TRANSIENT_CODES = {1, 2, 4, 17, 32, 613, 9004, 9007}


class GraphError(Exception):
    def __init__(self, message: str, code: int | None = None, subcode: int | None = None, status: int = 0):
        super().__init__(message)
        self.code = code
        self.subcode = subcode
        self.status = status

    @property
    def transient(self) -> bool:
        return self.status >= 500 or self.code in TRANSIENT_CODES


class GraphClient:
    def __init__(
        self,
        ig_user_id: str,
        access_token: str,
        api_host: str = "graph.facebook.com",
        api_version: str = "v23.0",
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not access_token:
            raise GraphError("No access token configured (set IG_ACCESS_TOKEN or account.access_token)")
        if not ig_user_id:
            raise GraphError("No Instagram user id configured (set IG_USER_ID or account.ig_user_id)")
        self.ig_user_id = ig_user_id
        self.token = access_token
        self.base = f"https://{api_host}/{api_version}"
        self.session = session or requests.Session()
        self.sleep = sleep

    # -- transport -------------------------------------------------------------------------
    def request(self, method: str, path: str, params: dict | None = None, retries: int = 3) -> dict:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        for key, value in list(params.items()):
            if isinstance(value, (list, dict)):
                params[key] = json.dumps(value, separators=(",", ":"))
            elif isinstance(value, bool):
                params[key] = "true" if value else "false"
        params["access_token"] = self.token
        url = f"{self.base}/{path.lstrip('/')}"
        attempt = 0
        while True:
            attempt += 1
            try:
                if method == "GET":
                    resp = self.session.request(method, url, params=params, timeout=60)
                else:
                    resp = self.session.request(method, url, data=params, timeout=60)
                payload = _json(resp)
                if resp.status_code >= 400 or "error" in payload:
                    err = payload.get("error", {})
                    raise GraphError(
                        err.get("error_user_msg") or err.get("message") or f"HTTP {resp.status_code}",
                        err.get("code"), err.get("error_subcode"), resp.status_code,
                    )
                return payload
            except requests.RequestException as exc:
                if attempt > retries:
                    raise GraphError(f"Network error: {exc}") from exc
            except GraphError as exc:
                if not exc.transient or attempt > retries:
                    raise
            self.sleep(min(2 ** attempt, 30))

    def paged(self, path: str, params: dict | None = None, limit: int | None = None) -> list[dict]:
        out: list[dict] = []
        payload = self.request("GET", path, params)
        while True:
            out.extend(payload.get("data", []))
            nxt = payload.get("paging", {}).get("cursors", {}).get("after")
            if not nxt or not payload.get("paging", {}).get("next") or (limit and len(out) >= limit):
                break
            payload = self.request("GET", path, {**(params or {}), "after": nxt})
        return out[:limit] if limit else out

    # -- account ---------------------------------------------------------------------------
    def account(self) -> dict:
        return self.request("GET", self.ig_user_id, {"fields": "id,username,name,followers_count,media_count"})

    def publishing_limit(self) -> dict:
        data = self.request(
            "GET", f"{self.ig_user_id}/content_publishing_limit", {"fields": "quota_usage,config"}
        ).get("data", [])
        return data[0] if data else {}

    # -- publishing ------------------------------------------------------------------------
    def create_image_container(
        self,
        image_url: str,
        caption: str | None = None,
        user_tags: list[dict] | None = None,
        collaborators: list[str] | None = None,
        alt_text: str | None = None,
        carousel_item: bool = False,
    ) -> str:
        params: dict[str, Any] = {"image_url": image_url, "alt_text": alt_text or None}
        if carousel_item:
            params["is_carousel_item"] = True
        else:
            params["caption"] = caption
            params["collaborators"] = collaborators or None
        params["user_tags"] = user_tags or None
        return self.request("POST", f"{self.ig_user_id}/media", params)["id"]

    def create_carousel_container(
        self, children: list[str], caption: str, collaborators: list[str] | None = None
    ) -> str:
        return self.request(
            "POST",
            f"{self.ig_user_id}/media",
            {
                "media_type": "CAROUSEL",
                "children": ",".join(children),
                "caption": caption,
                "collaborators": collaborators or None,
            },
        )["id"]

    def wait_until_ready(self, container_id: str, timeout: float = 300, interval: float = 5) -> None:
        waited = 0.0
        while True:
            status = self.request("GET", container_id, {"fields": "status_code,status"})
            code = status.get("status_code")
            if code in ("FINISHED", "PUBLISHED"):
                return
            if code in ("ERROR", "EXPIRED"):
                raise GraphError(f"Media container {container_id} failed: {status.get('status') or code}")
            if waited >= timeout:
                raise GraphError(f"Media container {container_id} not ready after {timeout:.0f}s ({code})")
            self.sleep(interval)
            waited += interval

    def publish_container(self, container_id: str) -> str:
        return self.request("POST", f"{self.ig_user_id}/media_publish", {"creation_id": container_id})["id"]

    def media(self, media_id: str, fields: str = "id,permalink,timestamp,caption") -> dict:
        return self.request("GET", media_id, {"fields": fields})

    def recent_media(self, limit: int = 25) -> list[dict]:
        return self.paged(
            f"{self.ig_user_id}/media",
            {"fields": "id,caption,permalink,timestamp,comments_count,like_count", "limit": min(limit, 50)},
            limit=limit,
        )

    # -- comments --------------------------------------------------------------------------
    def comments(self, media_id: str) -> list[dict]:
        return self.paged(
            f"{media_id}/comments",
            {
                "fields": "id,text,username,timestamp,like_count,hidden,"
                "replies{id,text,username,timestamp,hidden}",
                "limit": 50,
            },
        )

    def comment_on_media(self, media_id: str, message: str) -> str:
        return self.request("POST", f"{media_id}/comments", {"message": message})["id"]

    def reply(self, comment_id: str, message: str) -> str:
        return self.request("POST", f"{comment_id}/replies", {"message": message})["id"]

    def set_hidden(self, comment_id: str, hidden: bool) -> None:
        self.request("POST", comment_id, {"hide": hidden})

    def delete_comment(self, comment_id: str) -> None:
        self.request("DELETE", comment_id)


def _json(resp: requests.Response) -> dict:
    try:
        data = resp.json()
    except ValueError:
        return {"error": {"message": f"HTTP {resp.status_code}: {resp.text[:200]}"}} if resp.status_code >= 400 else {}
    return data if isinstance(data, dict) else {"data": data}
