"""Instagram 인증·토큰·발행·응답 유실·지표 요청 모의 검증"""

import hashlib
import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.auth import decrypt, encrypt
from app.db import account, balance, connect, owned_post


def ready(settings):
    settings.public_url = "https://cafe.example"
    settings.instagram_app_id = "example-app-id"
    settings.instagram_app_secret = "example-secret"


def attach(settings):
    with connect(settings, write=True) as con:
        con.execute(
            "UPDATE accounts SET ig_user_id='12345', ig_username='test_cafe', token=?, token_issued_at=?, token_expires_at=? WHERE id='local-owner'",
            (
                encrypt(settings, "test-token"),
                int(time.time()),
                int(time.time()) + 86400 * 60,
            ),
        )


def test_oauth_state_exchange_encrypted_token_and_no_regrant(
    app, client, csrf, settings, new_post
):
    ready(settings)
    identifier = new_post()
    from app.credits import finalize

    finalize(settings, "local-owner", identifier)
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/oauth/access_token":
            assert b"grant_type=authorization_code" in request.content
            fields = parse_qs(request.content.decode())
            assert fields["client_id"] == [settings.instagram_app_id]
            assert fields["client_secret"] == [settings.instagram_app_secret]
            assert fields["redirect_uri"] == [settings.redirect_url]
            assert "redirect_url" not in fields
            return httpx.Response(
                200, json={"access_token": "short-test", "user_id": "12345"}
            )
        if request.url.path == "/access_token":
            assert request.url.params["grant_type"] == "ig_exchange_token"
            assert request.url.params["client_secret"] == settings.instagram_app_secret
            return httpx.Response(
                200, json={"access_token": "long-test", "expires_in": 5_000_000}
            )
        if request.url.path.endswith("/me"):
            return httpx.Response(
                200, json={"user_id": "12345", "username": "test_cafe"}
            )
        raise AssertionError(request.url.path)

    with httpx.Client(transport=httpx.MockTransport(handler)) as external:
        app.state.instagram.client = external
        redirect = client.get("/auth/instagram", follow_redirects=False)
        query = parse_qs(urlparse(redirect.headers["location"]).query)
        assert "instagram_business_content_publish" in query["scope"][0]
        assert query["redirect_uri"] == ["https://cafe.example/auth/instagram/callback"]
        assert (
            client.get("/auth/instagram/callback?state=invalid&code=code").status_code
            == 400
        )
        assert not calls
        redirect = client.get("/auth/instagram", follow_redirects=False)
        state = parse_qs(urlparse(redirect.headers["location"]).query)["state"][0]
        response = client.get(
            "/auth/instagram/callback",
            params={"state": state, "code": "auth-code"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert (
            client.get(
                "/auth/instagram/callback", params={"state": state, "code": "auth-code"}
            ).status_code
            == 400
        )
    shop = account(settings, "local-owner")
    assert shop["token"] != b"long-test"
    assert decrypt(settings, shop["token"]) == "long-test"
    with connect(settings) as con:
        assert balance(con, "local-owner") == 4
        assert (
            con.execute(
                "SELECT COUNT(*) FROM credits WHERE reference='trial'"
            ).fetchone()[0]
            == 1
        )


def publisher_handler(
    calls, *, timeout=False, initially_processing=False, rejected=False
):
    def handler(request):
        calls.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path.endswith("/media"):
            assert b"image_url=https%3A%2F%2Fcafe.example%2Fmedia%2F" in request.content
            if rejected:
                return httpx.Response(400, json={"error": {"code": 100}})
            return httpx.Response(200, json={"id": "container-1"})
        if request.url.path.endswith("/container-1"):
            return httpx.Response(
                200,
                json={
                    "status_code": "IN_PROGRESS" if initially_processing else "FINISHED"
                },
            )
        if request.url.path.endswith("/media_publish"):
            if timeout:
                raise httpx.ReadTimeout("simulated timeout", request=request)
            return httpx.Response(200, json={"id": "published-1"})
        if request.method == "GET" and request.url.path.endswith("/media"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "published-1",
                            "timestamp": "2026-10-19T10:00:00+0000",
                            "caption": "내 게시물",
                            "media_type": "IMAGE",
                        }
                    ]
                },
            )
        raise AssertionError(request.url.path)

    return handler


def test_publish_real_request_contract_and_same_post_never_published_twice(
    app, client, csrf, settings, new_post
):
    identifier = new_post()
    ready(settings)
    attach(settings)
    calls = []
    with httpx.Client(
        transport=httpx.MockTransport(publisher_handler(calls))
    ) as external:
        app.state.instagram.client = external
        for _ in range(2):
            response = client.post(
                f"/posts/{identifier}/publish",
                data={"csrf": csrf, "confirmed": "yes", "revision": 1},
            )
            assert response.status_code == 200, response.text
    assert (
        sum(
            method == "POST" and path.endswith("/media_publish")
            for method, path in calls
        )
        == 1
    )
    with connect(settings) as con:
        post = owned_post(con, "local-owner", identifier)
        assert (
            post["publish_state"] == "published"
            and post["ig_media_id"] == "published-1"
        )
        assert post["published_at"] == "2026-10-19T10:00:00+0000"
        assert balance(con, "local-owner") == 4
    assert client.get(f"/posts/{identifier}/download").status_code == 200


def test_concurrently_refinalized_revision_is_not_published_without_new_confirmation(
    app, client, csrf, settings, new_post, monkeypatch
):
    import app.instagram as instagram

    identifier = new_post()
    ready(settings)
    attach(settings)
    original_finalize = instagram.finalize

    def finalize_then_other_edit(*args, **kwargs):
        confirmed_post = original_finalize(*args, **kwargs)
        # 별도 요청이 첫 확정 직후 수정·재확정한 상황을 재현한다.
        with connect(settings, write=True) as con:
            con.execute(
                "UPDATE posts SET caption='다른 요청의 새 본문', revision=revision+1 WHERE id=?",
                (identifier,),
            )
        original_finalize(settings, "local-owner", identifier, expected_revision=2)
        return confirmed_post

    monkeypatch.setattr(instagram, "finalize", finalize_then_other_edit)
    calls = []
    with httpx.Client(
        transport=httpx.MockTransport(publisher_handler(calls))
    ) as external:
        app.state.instagram.client = external
        response = client.post(
            f"/posts/{identifier}/publish",
            data={"csrf": csrf, "confirmed": "yes", "revision": 1},
        )
    assert response.status_code == 409 and calls == []
    with connect(settings) as con:
        post = owned_post(con, "local-owner", identifier)
        assert post["publish_state"] == "not_requested" and post["revision"] == 2
        assert balance(con, "local-owner") == 4


def test_publish_timeout_retains_unknown_and_does_not_repeat(
    app, client, csrf, settings, new_post
):
    identifier = new_post()
    ready(settings)
    attach(settings)
    calls = []
    with httpx.Client(
        transport=httpx.MockTransport(publisher_handler(calls, timeout=True))
    ) as external:
        app.state.instagram.client = external
        assert (
            client.post(
                f"/posts/{identifier}/publish",
                data={"csrf": csrf, "confirmed": "yes", "revision": 1},
            ).status_code
            == 502
        )
        assert (
            client.post(
                f"/posts/{identifier}/publish",
                data={"csrf": csrf, "confirmed": "yes", "revision": 1},
            ).status_code
            == 409
        )
        assert (
            client.post(f"/posts/{identifier}/check", data={"csrf": csrf}).status_code
            == 409
        )
    assert (
        sum(
            method == "POST" and path.endswith("/media_publish")
            for method, path in calls
        )
        == 1
    )
    with connect(settings) as con:
        assert owned_post(con, "local-owner", identifier)["publish_state"] == "unknown"
        assert balance(con, "local-owner") == 4


def test_failed_publish_leaves_downloadable_pack_and_credit_used_once(
    app, client, csrf, settings, new_post
):
    identifier = new_post()
    ready(settings)
    attach(settings)
    with httpx.Client(
        transport=httpx.MockTransport(publisher_handler([], rejected=True))
    ) as external:
        app.state.instagram.client = external
        assert (
            client.post(
                f"/posts/{identifier}/publish",
                data={"csrf": csrf, "confirmed": "yes", "revision": 1},
            ).status_code
            == 502
        )
    assert client.get(f"/posts/{identifier}/download").status_code == 200
    with httpx.Client(transport=httpx.MockTransport(publisher_handler([]))) as external:
        app.state.instagram.client = external
        assert (
            client.post(
                f"/posts/{identifier}/publish",
                data={"csrf": csrf, "confirmed": "yes", "revision": 1},
            ).status_code
            == 200
        )
    with connect(settings) as con:
        assert balance(con, "local-owner") == 4


def test_waiting_container_is_resumed_not_recreated(
    app, client, csrf, settings, new_post
):
    identifier = new_post()
    ready(settings)
    attach(settings)
    calls = []
    with httpx.Client(
        transport=httpx.MockTransport(
            publisher_handler(calls, initially_processing=True)
        )
    ) as external:
        app.state.instagram.client = external
        assert (
            client.post(
                f"/posts/{identifier}/publish",
                data={"csrf": csrf, "confirmed": "yes", "revision": 1},
            ).status_code
            == 200
        )
    with httpx.Client(
        transport=httpx.MockTransport(publisher_handler(calls))
    ) as external:
        app.state.instagram.client = external
        assert (
            client.post(f"/posts/{identifier}/check", data={"csrf": csrf}).status_code
            == 200
        )
    assert (
        sum(method == "POST" and path.endswith("/media") for method, path in calls) == 1
    )
    assert (
        sum(
            method == "POST" and path.endswith("/media_publish")
            for method, path in calls
        )
        == 1
    )


def test_insights_zero_missing_unsupported_and_errors_remain_distinct(
    app, client, csrf, settings
):
    client.get("/studio")
    attach(settings)
    with connect(settings, write=True) as con:
        con.execute(
            "INSERT INTO history(owner_id, media_id, published_at) VALUES ('local-owner', 'media-1', '2026-10-18T00:00:00Z')"
        )

    def handler(request):
        name = request.url.params["metric"]
        if name == "saved":
            return httpx.Response(400, json={"error": {"code": 100}})
        if name == "shares":
            return httpx.Response(500, json={"error": {"code": 2}})
        if name == "comments":
            return httpx.Response(200, json={"data": []})
        return httpx.Response(
            200, json={"data": [{"name": name, "values": [{"value": 0}]}]}
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as external:
        app.state.instagram.client = external
        assert (
            client.post(
                "/analytics/collect", data={"csrf": csrf, "media_id": "media-1"}
            ).status_code
            == 200
        )
    with connect(settings) as con:
        values = json.loads(
            con.execute("SELECT metrics FROM observations").fetchone()[0]
        )
    assert values["likes"] == {"value": 0, "status": "available"}
    assert values["comments"]["status"] == "pending"
    assert values["saved"]["status"] == "unsupported"
    assert values["shares"]["status"] == "error"
    assert (
        client.post(
            "/analytics/collect", data={"csrf": csrf, "media_id": "not-owned"}
        ).status_code
        == 404
    )


def test_public_image_token_expires_and_no_arbitrary_path(client, settings):
    path = settings.data_dir / "finished" / "test.jpg"
    path.write_bytes(b"test-jpeg")
    with connect(settings, write=True) as con:
        con.execute(
            "INSERT INTO media_links(token_hash, filename, expires_at) VALUES (?, 'test.jpg', ?)",
            (hashlib.sha256(b"capability").hexdigest(), int(time.time()) + 600),
        )
    assert client.get("/media/capability.jpg").content == b"test-jpeg"
    with connect(settings, write=True) as con:
        con.execute("UPDATE media_links SET expires_at=0")
    assert client.get("/media/capability.jpg").status_code == 404
