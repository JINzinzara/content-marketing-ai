"""Instagram 계정 인증·게시물 조회·중복 없는 발행·실제 지표 수집을 담당"""

import hashlib
import json
import math
import secrets
import time
from datetime import datetime, timezone

import httpx
from fastapi import HTTPException

from .auth import decrypt, encrypt
from .credits import finalize
from .db import account, connect, ensure_account, owned_post


class InstagramError(Exception):
    def __init__(self, message, *, kind="api", code=None):
        super().__init__(message)
        self.kind = kind
        self.code = code


class Instagram:
    def __init__(self, settings, client):
        self.settings, self.client = settings, client
        self.base = "https://graph.instagram.com/" + settings.instagram_version

    def request(self, method, url, **kwargs):
        kwargs.setdefault("timeout", 30)
        try:
            response = self.client.request(method, url, **kwargs)
        except httpx.TransportError:
            raise InstagramError(
                "Instagram이 응답하지 않습니다", kind="uncertain"
            ) from None
        try:
            body = response.json()
        except ValueError:
            raise InstagramError(
                "Instagram에서 읽을 수 없는 응답을 받았습니다", kind="uncertain"
            ) from None
        if not isinstance(body, dict):
            raise InstagramError(
                "Instagram 응답 형식을 확인할 수 없습니다", kind="uncertain"
            )
        if response.is_error or "error" in body:
            code = (
                body.get("error", {}).get("code")
                if isinstance(body.get("error"), dict)
                else None
            )
            if code == 190 or response.status_code == 401:
                raise InstagramError(
                    "Instagram 연결이 만료되었습니다. 계정을 다시 연결해주세요",
                    kind="auth",
                    code=code,
                )
            if code in {10, 200} or response.status_code == 403:
                raise InstagramError(
                    "Instagram 권한을 확인해주세요. 앱 심사 또는 계정 설정이 필요할 수 있습니다",
                    kind="permission",
                    code=code,
                )
            raise InstagramError(
                "Instagram 요청이 거절되었습니다. 계정·권한·콘텐츠 조건을 확인해주세요",
                kind="uncertain" if response.status_code >= 500 else "api",
                code=code,
            )
        return body

    def graph(self, method, path, token, **kwargs):
        return self.request(
            method,
            self.base + "/" + path,
            headers={"Authorization": "Bearer " + token},
            **kwargs,
        )

    def exchange_code(self, code):
        short = self.request(
            "POST",
            "https://api.instagram.com/oauth/access_token",
            data={
                "client_id": self.settings.instagram_app_id,
                "client_secret": self.settings.instagram_app_secret,
                "grant_type": "authorization_code",
                "redirect_uri": self.settings.redirect_url,
                "code": code.removesuffix("#_"),
            },
        )
        if not short.get("access_token"):
            raise InstagramError("Instagram 인증 토큰을 받지 못했습니다")
        long = self.request(
            "GET",
            "https://graph.instagram.com/access_token",
            params={
                "grant_type": "ig_exchange_token",
                "client_secret": self.settings.instagram_app_secret,
                "access_token": short["access_token"],
            },
        )
        if not long.get("access_token") or not isinstance(long.get("expires_in"), int):
            raise InstagramError("Instagram 장기 연결을 완료하지 못했습니다")
        me = self.graph(
            "GET", "me", long["access_token"], params={"fields": "user_id,username"}
        )
        if not me.get("user_id") or not me.get("username"):
            raise InstagramError("연결 계정의 식별자를 확인하지 못했습니다")
        return long, me

    def save_connection(self, preferred_owner, long, me):
        ig_id = str(me["user_id"])
        with connect(self.settings, write=True) as con:
            existing = con.execute(
                "SELECT id FROM accounts WHERE ig_user_id=?", (ig_id,)
            ).fetchone()
            if existing and preferred_owner and existing["id"] != preferred_owner:
                raise HTTPException(409, "이미 다른 계정에 연결된 계정입니다")
            owner_id = existing["id"] if existing else preferred_owner or "ig:" + ig_id
            ensure_account(con, owner_id)
            current = con.execute(
                "SELECT ig_user_id FROM accounts WHERE id=?", (owner_id,)
            ).fetchone()
            if current["ig_user_id"] and current["ig_user_id"] != ig_id:
                raise HTTPException(
                    409, "첫 버전에서는 계정당 Instagram 계정 1개만 연결 가능합니다"
                )
            now = int(time.time())
            con.execute(
                "UPDATE accounts SET ig_user_id=?, ig_username=?, token=?, token_issued_at=?, token_expires_at=? WHERE id=?",
                (
                    ig_id,
                    me["username"],
                    encrypt(self.settings, long["access_token"]),
                    now,
                    now + long["expires_in"],
                    owner_id,
                ),
            )
            return owner_id

    def connection(self, owner_id):
        shop = account(self.settings, owner_id)
        if not shop or not shop["token"]:
            raise HTTPException(409, "Instagram 계정을 연결해주세요")
        now = int(time.time())
        if not shop["token_expires_at"] or shop["token_expires_at"] <= now:
            raise HTTPException(
                401, "Instagram 연결이 만료되었습니다. 다시 연결해주세요"
            )
        token = decrypt(self.settings, shop["token"])
        if (
            shop["token_expires_at"] - now < 86400
            and now - shop["token_issued_at"] >= 86400
        ):
            refreshed = self.request(
                "GET",
                "https://graph.instagram.com/refresh_access_token",
                params={
                    "grant_type": "ig_refresh_token",
                    "access_token": token,
                },
            )
            if not refreshed.get("access_token") or not isinstance(
                refreshed.get("expires_in"), int
            ):
                raise InstagramError(
                    "Instagram 연결 갱신에 실패했습니다. 다시 연결해주세요"
                )
            token = refreshed["access_token"]
            with connect(self.settings, write=True) as con:
                con.execute(
                    "UPDATE accounts SET token=?, token_issued_at=?, token_expires_at=? WHERE id=?",
                    (
                        encrypt(self.settings, token),
                        now,
                        now + refreshed["expires_in"],
                        owner_id,
                    ),
                )
        return shop["ig_user_id"], token

    def sync_history(self, owner_id, after=""):
        ig_id, token = self.connection(owner_id)
        params = {
            "fields": "id,caption,media_type,media_url,thumbnail_url, timestamp, permalink",
            "limit": self.settings.history_page_size,
        }
        if after:
            params["after"] = after
        body = self.graph("GET", ig_id + "/media", token, params=params)
        if not isinstance(body.get("data"), list):
            raise InstagramError("게시물 목록의 응답 형식이 에상과 다릅니다")
        with connect(self.settings, write=True) as con:
            for item in body["data"]:
                if not item.get("id"):
                    continue
                con.execute(
                    """INSERT INTO history(owner_id, media_id, caption, media_type, media_url, thumbnail_url, published_at, permalink)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(owner_id, media_id) DO UPDATE SET
                    caption=excluded.caption, media_type=excluded.media_type, media_url=excluded.media_url,
                    thumbnail_url=excluded.thumbnail_url, published_at=COALESCE(excluded.published_at, history.published_at), permalink=excluded.permalink""",
                    (
                        owner_id,
                        str(item["id"]),
                        item.get("caption", ""),
                        item.get("media_type"),
                        item.get("media_url"),
                        item.get("thumbnail_url"),
                        item.get("timestamp"),
                        item.get("permalink"),
                    ),
                )
                con.execute(
                    "UPDATE posts SET published_at=COALESCE(?, published_at) WHERE owner_id=? AND ig_media_id=?",
                    (item.get("timestamp"), owner_id, str(item["id"])),
                )
        paging = body.get("paging", {})
        return paging.get("cursors", {}).get("after", "") if paging.get("next") else ""

    def publish(self, owner_id, post_id, *, check_only=False, expected_revision=None):
        ig_id, token = self.connection(owner_id)
        if not self.settings.instagram_ready:
            raise HTTPException(
                503, "공개 HTTPS 주소와 Instagram 앱 설정을 확인해주세요"
            )
        with connect(self.settings, write=True) as con:
            post = owned_post(con, owner_id, post_id)
        if (
            not check_only
            and expected_revision is not None
            and post["revision"] != expected_revision
        ):
            raise HTTPException(
                409, "확인한 게시물 내용이 변경되었습니다. 새 미리보기를 확인해주세요"
            )
        if post["publish_state"] == "published":
            return post
        if check_only and not post["container_id"]:
            raise HTTPException(
                409, "확인할 게시물 컨테이너가 없습니다. 계정을 확인해주세요"
            )
        if not check_only:
            if post["publish_state"] not in {"not_requested", "failed"}:
                raise HTTPException(409, "이미 요청한 게시물입니다")
            post = finalize(
                self.settings, owner_id, post_id, expected_revision=expected_revision
            )
            with connect(self.settings, write=True) as con:
                current = owned_post(con, owner_id, post_id)
                if current["publish_state"] not in {"not_requested", "failed"}:
                    raise HTTPException(409, "이미 게시를 요청했습니다")
                if (
                    current["revision"] != post["revision"]
                    or current["rendered_revision"] != current["revision"]
                ):
                    raise HTTPException(
                        409, "완성 후 내용이 변경되었습니다. 새 미리보기를 확인해주세요"
                    )
                snapshot = {
                    k: current[k]
                    for k in (
                        "title",
                        "caption",
                        "hashtags",
                        "layout",
                        "aspect",
                        "purpose",
                        "promotion",
                        "product_name",
                        "price",
                        "show_price",
                        "style_snapshot",
                        "selected_image_id",
                    )
                }
                con.execute(
                    "UPDATE posts SET publish_state='creating', container_id=NULL, published_snapshot=?, publish_message='' WHERE id=?",
                    (json.dumps(snapshot, ensure_ascii=False), post_id),
                )
                capability = secrets.token_urlsafe(32)
                con.execute(
                    "INSERT INTO media_links(token_hash, filename, expires_at) VALUES (?, ?, ?)",
                    (
                        hashlib.sha256(capability.encode()).hexdigest(),
                        current["finished_file"],
                        int(time.time()) + 86400,
                    ),
                )
            try:
                result = self.graph(
                    "POST",
                    ig_id + "/media",
                    token,
                    data={
                        "image_url": self.settings.public_url
                        + "/media/"
                        + capability
                        + ".jpg",
                        "caption": caption_text(current),
                    },
                )
                if not result.get("id"):
                    raise InstagramError(
                        "게시물 컨테이너 ID를 받지 못했습니다", kind="uncertain"
                    )
                with connect(self.settings, write=True) as con:
                    con.execute(
                        "UPDATE posts SET container_id=?, publish_state='container_ready' WHERE id=?",
                        (str(result["id"]), post_id),
                    )
            except InstagramError as exc:
                self._failure(post_id, exc)
                raise
        return self._advance(owner_id, post_id, token)

    def _advance(self, owner_id, post_id, token):
        with connect(self.settings, write=True) as con:
            post = owned_post(con, owner_id, post_id)
        try:
            status = self.graph(
                "GET", post["container_id"], token, params={"fields": "status_code"}
            ).get("status_code")
            if status == "PUBLISHED":
                # 응답 유실 시, 게시물 ID를 추측하지 않고 중복 발행 금지
                with connect(self.settings, write=True) as con:
                    con.execute(
                        "UPDATE posts SET publish_state='published', publish_message=? WHERE id=?",
                        ("게시가 완료되었습니다", post_id),
                    )
                return
            if status in {"ERROR", "EXPIRED"}:
                raise InstagramError(
                    "이미지 처리에 실패하였거나 컨테이너가 만료되었습니다"
                )
            if post["publish_state"] != "container_ready":
                raise HTTPException(409, "이전 발행 요청 결과가 불명확합니다")
            if status != "FINISHED":
                with connect(self.settings, write=True) as con:
                    con.execute(
                        "UPDATE posts SET publish_message=? WHERE id=?",
                        (
                            "Instagram에서 이미지 처리 중입니다. 잠시 후 게시 상태 확인을 눌러주세요",
                            post_id,
                        ),
                    )
                return
            with connect(self.settings, write=True) as con:
                changed = con.execute(
                    "UPDATE posts SET publish_state='publishing' WHERE id=? AND publish_state='container_ready'",
                    (post_id,),
                ).rowcount
                if not changed:
                    raise HTTPException(
                        409, "다른 요청이 처리되고 있습니다. 잠시 후 다시 시도해주세요"
                    )
            result = self.graph(
                "POST",
                account(self.settings, owner_id)["ig_user_id"] + "/media_publish",
                token,
                data={"creation_id": post["container_id"]},
            )
            if not result.get("id"):
                raise InstagramError(
                    "발행 요청의 게시물 ID를 확인할 수 없습니다", kind="uncertain"
                )
            with connect(self.settings, write=True) as con:
                con.execute(
                    "UPDATE posts SET publish_state='published', ig_media_id=?, publish_message='' WHERE id=?",
                    (str(result["id"]), post_id),
                )
            self.sync_history(owner_id)
        except InstagramError as exc:
            self._failure(post_id, exc)
            raise

    def _failure(self, post_id, exc):
        with connect(self.settings, write=True) as con:
            post = con.execute(
                "SELECT publish_state FROM posts WHERE id=?", (post_id,)
            ).fetchone()
            if post["publish_state"] == "published":
                con.execute(
                    "UPDATE posts SET publish_message=? WHERE id=?", (str(exc), post_id)
                )
                return
            state = (
                "unknown"
                if exc.kind == "uncertain"
                or post["publish_state"] in {"publishing", "unknown"}
                else "failed"
            )
            con.execute(
                "UPDATE posts SET publish_state=?, publish_message=? WHERE id=?",
                (state, str(exc), post_id),
            )

    def collect_metrics(self, owner_id, media_id):
        _, token = self.connection(owner_id)
        with connect(self.settings) as con:
            media = con.execute(
                "SELECT * FROM history WHERE owner_id=? AND media_id=?",
                (owner_id, media_id),
            ).fetchone()
        if not media:
            raise HTTPException(404, "본인 계정의 게시물만 분석할 수 있습니다")
        observed_at = datetime.now(timezone.utc).isoformat()
        metrics = {}
        for name in ("likes", "comments", "saved", "shares", "views", "reach"):
            try:
                result = self.graph(
                    "GET", media_id + "/insights", token, params={"metric": name}
                )
                entries = result.get("data", [])
                item = next((x for x in entries if x.get("name") == name), {})
                values = item.get("values", [])
                value = item.get("total_value", {}).get("value")
                if value is None and values:
                    value = values[-1].get("value")
                valid = (
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and math.isfinite(value)
                    and value >= 0
                )
                metrics[name] = {
                    "value": value if valid else None,
                    "status": "available" if valid else "pending",
                }
            except InstagramError as exc:
                if exc.kind in {"auth", "permission"}:
                    raise
                metrics[name] = {
                    "value": None,
                    "status": "unsupported" if exc.code == 100 else "error",
                }
        with connect(self.settings, write=True) as con:
            con.execute(
                "INSERT INTO observations(owner_id, media_id, observed_at, published_at, metrics) VALUES (?, ?, ?, ?, ?)",
                (
                    owner_id,
                    media_id,
                    observed_at,
                    media["published_at"],
                    json.dumps(metrics),
                ),
            )
        return metrics


def caption_text(post):
    return (post["caption"].strip() + "\n\n" + post["hashtags"].strip()).strip()
