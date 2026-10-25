"""삭제의 소유권·요청 경합·제작권 보존과 삭제 자료의 AI 재사용 방지를 검증한다."""

import json

import httpx
import pytest

from app.credits import finalize
from app.db import balance, connect, ensure_account, owned_post
from app.styles import FIELDS, reference_list, save_profile
from test_generation import enable, response_body
from test_image_generation import enable_image
from test_instagram import attach


def test_deleted_content_is_inaccessible_without_refunding_credits(client, csrf, new_post, settings):
    identifier = new_post()
    post = finalize(settings, "local-owner", identifier)
    with connect(settings, write=True) as con:
        con.execute("INSERT INTO media_links VALUES ('old-link', ?, 9999999999)", (post["finished_file"],))
    form = {"csrf": csrf, "confirmed": "yes", "revision": 1}
    url = f"/posts/{identifier}/delete"
    assert client.post(url, data={**form, "csrf": "wrong"}).status_code == 403
    assert client.post(url, data={**form, "revision": 2}).status_code == 409
    assert client.post(url, data={**form, "confirmed": "no"}).status_code == 422
    assert client.post(url, data=form).status_code == 200
    assert f'href="/posts/{identifier}"' not in client.get('/studio').text
    for path in ("", "/original", "/image", "/download", "/image-progress"):
        assert client.get(f"/posts/{identifier}{path}").status_code == 404
    assert client.post(url, data=form).status_code == 404
    with connect(settings) as con:
        assert balance(con, "local-owner") == 4
        assert con.execute("SELECT COUNT(*) FROM media_links").fetchone()[0] == 0
        assert con.execute("SELECT deleted_at FROM posts WHERE id=?", (identifier,)).fetchone()[0]


@pytest.mark.parametrize("busy", ["publish", "image", "foreign"])
def test_busy_or_foreign_content_cannot_be_deleted(app, client, csrf, new_post, settings, busy):
    identifier = new_post(preset_id="natural")
    if busy == "image":
        enable_image(settings)
        app.state.image_ai.queue("local-owner", identifier, 1)
    else:
        with connect(settings, write=True) as con:
            if busy == "foreign":
                ensure_account(con, "other")
                con.execute("UPDATE posts SET owner_id='other' WHERE id=?", (identifier,))
            else:
                con.execute("UPDATE posts SET publish_state='unknown' WHERE id=?", (identifier,))
    response = client.post(f"/posts/{identifier}/delete", data={"csrf": csrf, "confirmed": "yes", "revision": 1})
    assert response.status_code == (404 if busy == "foreign" else 409)
    with connect(settings) as con:
        assert con.execute("SELECT deleted_at FROM posts WHERE id=?", (identifier,)).fetchone()[0] is None


def test_deleted_reference_is_not_sent_to_ai_and_confirmed_style_survives(app, client, csrf, new_post, settings, photo):
    client.post("/shop/references", data={"csrf": csrf, "caption": "삭제할 비공개 문구"}, files={"photo": ("past.png", photo, "image/png")})
    with connect(settings, write=True) as con:
        identifier = con.execute("SELECT id FROM style_references").fetchone()[0]
        save_profile(con, "local-owner", {"summary": "따뜻한 분위기", "attributes": {key: "따뜻함" for key in FIELDS}}, ["local:" + identifier], "confirmed")
    post_id = new_post()
    url = f"/shop/references/local:{identifier}/delete"
    form = {"csrf": csrf, "confirmed": "yes"}
    assert client.post(url, data={**form, "csrf": "wrong"}).status_code == 403
    assert client.post(url, data=form).status_code == 200
    assert client.get(f"/shop/references/{identifier}").status_code == 404
    assert client.post("/shop/analyze", data={"csrf": csrf, "reference_id": "local:" + identifier}).status_code == 404
    enable(settings)
    def handler(request):
        body = json.loads(request.content)
        data = json.loads(body["input"][0]["content"][0]["text"])
        assert data["past_post_examples"] == []
        assert data["resolved_visual_style"]["summary"] == "따뜻한 분위기"
        if request.url.path.endswith("input_tokens"):
            return httpx.Response(200, json={"input_tokens": 100})
        return httpx.Response(200, json=response_body())
    with httpx.Client(transport=httpx.MockTransport(handler)) as external:
        app.state.ai.client = external
        assert client.post(f"/posts/{post_id}/generate", data={"csrf": csrf}).status_code == 200
    enable_image(settings)
    with connect(settings) as con:
        revision = owned_post(con, "local-owner", post_id)["revision"]
        assert reference_list(con, "local-owner") == []
    assert app.state.image_ai.queue("local-owner", post_id, revision)


def test_instagram_reference_deletion_preserves_metrics_and_ownership(client, csrf, settings):
    with connect(settings, write=True) as con:
        ensure_account(con, "other")
        con.execute("INSERT INTO history(owner_id, media_id, caption) VALUES ('local-owner','mine','내 게시물'), ('other','foreign','다른 게시물')")
        con.execute("INSERT INTO observations(owner_id, media_id, observed_at, metrics) VALUES ('local-owner','mine','2026-10-22','{}')")
    form = {"csrf": csrf, "confirmed": "yes"}
    assert client.post('/shop/references/ig:foreign/delete', data=form).status_code == 404
    assert client.post('/shop/references/ig:mine/delete', data=form).status_code == 200
    with connect(settings) as con:
        assert reference_list(con, "local-owner") == []
        assert con.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1


def test_manual_import_restores_returned_posts_without_duplicates_or_lost_metrics(app, client, csrf, settings):
    attach(settings)
    with connect(settings, write=True) as con:
        ensure_account(con, "other")
        con.execute("INSERT INTO history(owner_id, media_id, caption, deleted_at) VALUES ('local-owner','mine','기존 문구',1), ('local-owner','older','다음 페이지',1), ('other','mine','다른 계정',1)")
        con.execute("INSERT INTO observations(owner_id, media_id, observed_at, metrics) VALUES ('local-owner','mine','2026-10-25','{}')")
    calls = []
    def handler(request):
        calls.append(request.url.path)
        assert request.method == "GET" and request.url.path.endswith("/12345/media")
        assert request.url.params.get("after", "") == "older-page"
        return httpx.Response(200, json={"data": [{"id": "mine", "caption": "갱신한 문구", "media_type": "IMAGE"}]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as external:
        app.state.instagram.client = external
        assert client.post('/instagram/sync', data={"csrf": "wrong"}).status_code == 403
        assert not calls
        for _ in range(2):
            response = client.post('/instagram/sync', data={"csrf": csrf, "after": "older-page"})
            assert response.status_code == 200
    with connect(settings) as con:
        items = reference_list(con, "local-owner")
        assert [(r["id"], r["caption"]) for r in items] == [("ig:mine", "갱신한 문구")]
        assert con.execute("SELECT COUNT(*) FROM history").fetchone()[0] == 3
        assert con.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1
        assert con.execute("SELECT deleted_at FROM history WHERE owner_id='other'").fetchone()[0] == 1
        assert con.execute("SELECT deleted_at FROM history WHERE media_id='older'").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM ai_calls").fetchone()[0] == 0


def test_automatic_sync_keeps_deleted_references_hidden(app, client, csrf, settings):
    attach(settings)
    with connect(settings, write=True) as con:
        con.execute("INSERT INTO history(owner_id, media_id, caption, deleted_at) VALUES ('local-owner','mine','기존 문구',1)")
    def handler(request):
        assert request.method == "GET" and request.url.path.endswith("/12345/media")
        return httpx.Response(200, json={"data": [{"id": "mine", "caption": "갱신한 문구"}, {"id": "new", "caption": "새 게시물"}]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as external:
        app.state.instagram.client = external
        app.state.instagram.sync_history("local-owner")
    with connect(settings) as con:
        assert [r["id"] for r in reference_list(con, "local-owner")] == ["ig:new"]
        row = con.execute("SELECT deleted_at, caption FROM history WHERE media_id='mine'").fetchone()
        assert tuple(row) == (1, "갱신한 문구")


def test_failed_import_does_not_restore_deleted_references(app, client, csrf, settings):
    attach(settings)
    with connect(settings, write=True) as con:
        con.execute("INSERT INTO history(owner_id, media_id, deleted_at) VALUES ('local-owner','mine',1)")
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403, json={"error": {"code": 10}}))) as external:
        app.state.instagram.client = external
        assert client.post('/instagram/sync', data={"csrf": csrf}).status_code == 502
    with connect(settings) as con:
        assert reference_list(con, "local-owner") == []
        assert con.execute("SELECT deleted_at FROM history").fetchone()[0] == 1
