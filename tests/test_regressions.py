"""직접 작성 과정에서 어긋난 인증·조회·입력·문구 생성 경로를 검증한다."""

import json
import time
from urllib.parse import parse_qs

import httpx
import pytest

from app.auth import decrypt
from app.db import connect, ensure_account, owned_post
from app.instagram import InstagramError
from app.styles import FIELDS, reference_data, reference_list, save_profile
from test_generation import enable, response_body
from test_instagram import attach


def test_token_refresh_updates_the_encrypted_token_and_issued_time(app, client, settings):
    client.get("/studio")
    attach(settings)
    started = int(time.time())
    with connect(settings, write=True) as con:
        con.execute("UPDATE accounts SET token_issued_at=?, token_expires_at=? WHERE id='local-owner'",
                    (started - 172800, started + 60))
    calls = []
    def handler(request):
        calls.append(request.url.path)
        assert request.url.path == "/refresh_access_token"
        assert request.url.params["access_token"] == "test-token"
        return httpx.Response(200, json={"access_token": "refreshed-test-token", "expires_in": 5000000})
    with httpx.Client(transport=httpx.MockTransport(handler)) as external:
        app.state.instagram.client = external
        assert app.state.instagram.connection("local-owner") == ("12345", "refreshed-test-token")
        assert app.state.instagram.connection("local-owner") == ("12345", "refreshed-test-token")
    assert calls == ["/refresh_access_token"]
    with connect(settings) as con:
        row = con.execute("SELECT * FROM accounts WHERE id='local-owner'").fetchone()
        assert decrypt(settings, row["token"]) == "refreshed-test-token"
        assert row["token_issued_at"] >= started


@pytest.mark.parametrize("status,kind", [(401, "auth"), (403, "permission")])
def test_authentication_and_permission_failures_are_distinct(app, status, kind):
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(status, json={"error": {"code": 999}}))) as external:
        app.state.instagram.client = external
        with pytest.raises(InstagramError) as raised:
            app.state.instagram.request("GET", "https://graph.instagram.com/me")
    assert raised.value.kind == kind


def test_sync_history_can_be_selected_by_id_and_metrics_render(app, client, csrf, settings):
    attach(settings)
    def handler(request):
        if request.url.path.endswith("/media"):
            assert request.url.params["after"] == "page-one"
            return httpx.Response(200, json={"data": [{"id": "media-1", "caption": "기존 게시물 말투 ☕",
                "media_type": "IMAGE", "media_url": "https://images.example/past.jpg", "timestamp": "2026-10-19T10:00:00Z"}],
                "paging": {"next": "https://graph.instagram.com/next", "cursors": {"after": "page-two"}}})
        if request.url.path.endswith("/insights"):
            metric = request.url.params["metric"]
            return httpx.Response(200, json={"data": [{"name": metric, "values": [{"value": 0}]}]})
        raise AssertionError(request.url.path)
    with httpx.Client(transport=httpx.MockTransport(handler)) as external:
        app.state.instagram.client = external
        synced = client.post("/instagram/sync", data={"csrf": csrf, "after": "page-one"})
        assert synced.status_code == 200 and "page-two" in synced.text
        assert client.post("/instagram/sync", data={"csrf": csrf, "after": "x" * 2001}).status_code == 422
        assert client.post("/analytics/collect", data={"csrf": csrf, "media_id": "media-1"}).status_code == 200
    with connect(settings) as con:
        item = reference_list(con, "local-owner")[0]
        assert item["id"] == "ig:media-1" and item["caption"] == "기존 게시물 말투 ☕"
        assert item["preview"] == "https://images.example/past.jpg"
        assert reference_data(settings, con, "local-owner", item["id"])["image"] == item["preview"]
    assert "좋아요" in client.get("/analytics").text
    assert client.get("/purchase").status_code == 200


def test_invalid_edit_aspect_does_not_update_the_post(client, csrf, new_post, settings):
    identifier = new_post()
    with connect(settings) as con:
        before = owned_post(con, "local-owner", identifier)
    fields = {key: before[key] for key in ("title", "caption", "hashtags", "price", "layout", "revision")}
    assert client.post(f"/posts/{identifier}/edit", data={**fields, "csrf": csrf, "aspect": "invalid-portrait"}).status_code == 422
    with connect(settings) as con:
        assert owned_post(con, "local-owner", identifier) == before


def test_zero_token_usage_is_recorded_as_zero_cost(app, client, csrf, new_post, settings):
    identifier = new_post()
    enable(settings)
    def handler(request):
        if request.url.path.endswith("input_tokens"):
            return httpx.Response(200, json={"input_tokens": 100})
        result = response_body()
        result["usage"] = {"input_tokens": 0, "output_tokens": 0}
        return httpx.Response(200, json=result)
    with httpx.Client(transport=httpx.MockTransport(handler)) as external:
        app.state.ai.client = external
        assert client.post(f"/posts/{identifier}/generate", data={"csrf": csrf}).status_code == 200
    with connect(settings) as con:
        row = con.execute("SELECT * FROM ai_calls").fetchone()
        assert row["state"] == "completed" and row["cost_microusd"] == 0


def test_draft_uses_owned_caption_examples_and_new_product_photo(app, client, csrf, new_post, settings, photo):
    caption = "오늘도 천천히 쉬어가세요 ☕ #동네카페"
    client.post("/shop/references", data={"csrf": csrf, "caption": caption}, files={"photo": ("past.png", photo, "image/png")})
    with connect(settings, write=True) as con:
        reference = "local:" + con.execute("SELECT id FROM style_references WHERE owner_id='local-owner'").fetchone()[0]
        save_profile(con, "local-owner", {"summary": "짧고 편안한 존댓말", "attributes": {key: "" for key in FIELDS},
                     "locked_fields": []}, [reference], "confirmed")
        ensure_account(con, "another")
        con.execute("INSERT INTO style_references VALUES ('foreign', 'another', 'not-read.jpg', '다른 가게의 비공개 본문', 1)")
    identifier = new_post(facts="")
    enable(settings)
    calls = []
    def handler(request):
        calls.append(request.url.path)
        payload = json.loads(request.content)
        content = payload["input"][0]["content"]
        data = json.loads(content[0]["text"])
        assert data["past_post_examples"] == [{"id": reference, "caption": caption}]
        assert len(content) == 2 and content[1]["type"] == "input_image"
        if request.url.path.endswith("input_tokens"):
            return httpx.Response(200, json={"input_tokens": 100})
        return httpx.Response(200, json=response_body())
    with httpx.Client(transport=httpx.MockTransport(handler)) as external:
        app.state.ai.client = external
        assert client.post(f"/posts/{identifier}/generate", data={"csrf": csrf}).status_code == 200
        with connect(settings, write=True) as con:
            style = json.loads(owned_post(con, "local-owner", identifier)["style_snapshot"])
            style["reference_ids"] = ["local:foreign"]
            con.execute("UPDATE posts SET style_snapshot=? WHERE id=?", (json.dumps(style), identifier))
        assert client.post(f"/posts/{identifier}/generate", data={"csrf": csrf}).status_code == 404
    assert calls == ["/v1/responses/input_tokens", "/v1/responses"]
