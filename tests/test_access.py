"""소유권·CSRF·로컬 접근과 비공개 파일 노출 차단 검증"""

import base64
import json
from dataclasses import replace

import httpx
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

from app.db import connect, ensure_account
from app.main import create_app


def test_csrf_missing_and_invalid(client, new_post):
    identifier = new_post()
    for value in ({}, {"csrf": "incorrect"}):
        assert (
            client.post(
                f"/posts/{identifier}/finalize", data={**value, "confirmed": "yes"}
            ).status_code
            == 403
        )


def test_another_account_cannot_read_or_write(client, settings, new_post):
    identifier = new_post()
    with connect(settings, write=True) as con:
        ensure_account(con, "another")
    production = replace(
        settings,
        environment="production",
        public_url="https://cafe.example",
        instagram_app_id="test-id",
        instagram_app_secret="test-secret",
    )
    with httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(500))
    ) as external:
        app = create_app(production, external)
        with TestClient(app, base_url="https://cafe.example") as other:
            data = base64.b64encode(
                json.dumps({"owner_id": "another", "csrf": "another-csrf"}).encode()
            )
            other.cookies.set(
                "session", TimestampSigner(settings.session_secret).sign(data).decode()
            )
            for suffix in ("", "/image", "/download"):
                assert other.get(f"/posts/{identifier}{suffix}").status_code == 404
            assert (
                other.post(
                    f"/posts/{identifier}/finalize",
                    data={"csrf": "another-csrf", "confirmed": "yes", "revision": 1},
                ).status_code
                == 404
            )
            other.cookies.clear()
            assert other.get(f"/posts/{identifier}").status_code == 401


def test_local_mode_not_exposed_through_arbitrary_host(client):
    assert (
        client.get("/studio", headers={"host": "attacker.example"}).status_code == 400
    )


def test_secrets_and_private_files_not_served(client):
    assert client.get("/data/app.sqlite3").status_code == 404
    assert client.get("/.env").status_code == 404
    assert client.get("/media/not-a-real-token.jpg").status_code == 404
    assert (
        client.post(
            "/posts", headers={"content-length": str(11 * 1024 * 1024)}
        ).status_code
        == 413
    )
