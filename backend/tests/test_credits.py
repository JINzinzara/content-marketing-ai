"""무료 5건·동시 확정·중복 차감·파일 저장 실패와 버전 검증"""

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException

from backend.credits import finalize
from backend.db import balance, connect, owned_post


def test_five_completed_posts_sixth_blocked_and_repeat_free(
    client, csrf, new_post, settings
):
    ids = [new_post() for _ in range(6)]
    for post_id in ids[:5]:
        result = client.post(
            f"/posts/{post_id}/finalize",
            data={"csrf": csrf, "confirmed": "yes", "revision": 1},
            follow_redirects=False,
        )
        assert result.status_code == 303
    assert (
        client.post(
            f"/posts/{ids[5]}/finalize",
            data={"csrf": csrf, "confirmed": "yes", "revision": 1},
        ).status_code
        == 402
    )
    for _ in range(2):
        assert client.get(f"/posts/{ids[0]}/download").status_code == 200
        assert (
            client.post(
                f"/posts/{ids[0]}/finalize",
                data={"csrf": csrf, "confirmed": "yes", "revision": 1},
            ).status_code
            == 200
        )
    with connect(settings) as con:
        assert balance(con, "local-owner") == 0
        assert (
            con.execute("SELECT COUNT(*) FROM credits WHERE amount=-1").fetchone()[0]
            == 5
        )


def test_same_post_concurrent_requests_charge_once(new_post, settings):
    post_id = new_post()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(lambda _: finalize(settings, "local-owner", post_id), range(8))
        )
    assert len({p["finished_file"] for p in results}) == 1
    with connect(settings) as con:
        assert balance(con, "local-owner") == 4


def test_two_posts_compete_for_last_credit(new_post, settings):
    ids = [new_post() for _ in range(6)]
    for identifier in ids[:4]:
        finalize(settings, "local-owner", identifier)

    def attempt(identifier):
        try:
            finalize(settings, "local-owner", identifier)
            return "ok"
        except HTTPException as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ids[4:]))
    assert sorted(results, key=str) == sorted(["ok", 402], key=str)
    with connect(settings) as con:
        assert balance(con, "local-owner") == 0


def test_file_write_failure_does_not_consume_credit(new_post, settings, monkeypatch):
    post_id = new_post()

    def fail(*args):
        raise OSError("test disk failure")

    monkeypatch.setattr("backend.credits.atomic_write", fail)
    with pytest.raises(OSError):
        finalize(settings, "local-owner", post_id)
    with connect(settings) as con:
        assert balance(con, "local-owner") == 5
        assert owned_post(con, "local-owner", post_id)["finished_file"] is None


def test_restart_and_cookie_clear_do_not_regrant(settings, client, new_post, csrf):
    from backend.main import create_app
    from fastapi.testclient import TestClient

    post_id = new_post()
    finalize(settings, "local-owner", post_id)
    with TestClient(create_app(settings)) as second:
        response = second.get("/studio")
        assert response.status_code == 200 and post_id in response.text
        assert second.get(f"/posts/{post_id}/download").status_code == 200
    with connect(settings) as con:
        assert balance(con, "local-owner") == 4
        assert (
            con.execute(
                "SELECT COUNT(*) FROM credits WHERE reference='trial'"
            ).fetchone()[0]
            == 1
        )


def test_confirmation_required(client, csrf, new_post, settings):
    post_id = new_post()
    assert (
        client.post(f"/posts/{post_id}/finalize", data={"csrf": csrf}).status_code
        == 422
    )
    with connect(settings) as con:
        assert balance(con, "local-owner") == 5


def test_stale_confirmation_cannot_charge_unreviewed_revision(
    client, csrf, new_post, settings
):
    post_id = new_post()
    with connect(settings, write=True) as con:
        con.execute("UPDATE posts SET revision=2 WHERE id=?", (post_id,))
    response = client.post(
        f"/posts/{post_id}/finalize",
        data={"csrf": csrf, "confirmed": "yes", "revision": 1},
    )
    assert response.status_code == 409
    with connect(settings) as con:
        assert balance(con, "local-owner") == 5
