"""당일 확정·예약 게시·품절 취소와 관측/브리핑/추천의 실제 연결을 모의 HTTP로 검증."""

import io
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi import HTTPException
from PIL import Image

from app import analytics, jobs, lineups
from app.db import connect, initialize, owned_post
from test_generation import enable
from test_instagram import attach, ready


def menu(client, csrf, photo, name="스푸키쿠키"):
    response = client.post("/menus", data={"csrf": csrf, "name": name}, files={"photo": ("menu.png", photo, "image/png")})
    assert response.status_code == 200, response.text
    import re
    return re.findall(r'name="menu_id" value="([a-f0-9]{32})"', response.text)[-1]


def confirmed_lineup(client, csrf, photo, settings):
    identifier = menu(client, csrf, photo)
    assert client.post("/lineups/save", data={"csrf": csrf, "revision": 1, "menu_id": identifier}).status_code == 200
    assert client.post("/lineups/confirm", data={"csrf": csrf, "revision": 2, "confirmed": "yes"}).status_code == 200
    return identifier


def notice(client, csrf, rev=2, **changes):
    response = client.post("/lineups/notice", data={"csrf": csrf, "revision": rev, "aspect": "story", **changes}, follow_redirects=False)
    assert response.status_code == 303, response.text
    return response.headers["location"].split("/")[-1]


def test_today_confirmation_copy_and_ownership(client, csrf, photo, settings):
    identifier = menu(client, csrf, photo)
    assert client.post("/lineups/save", data={"csrf": csrf, "revision": 1, "menu_id": identifier}).status_code == 200
    assert client.post("/lineups/notice", data={"csrf": csrf, "revision": 2}).status_code == 409
    assert client.post("/lineups/confirm", data={"csrf": csrf, "revision": 1, "confirmed": "yes"}).status_code == 409
    with connect(settings, write=True) as con:
        yesterday = (datetime.now(lineups.KST).date()-timedelta(days=1)).isoformat()
        con.execute("UPDATE lineups SET day=?, confirmed_at=1 WHERE owner_id='local-owner'", (yesterday,))
        con.execute("UPDATE lineup_items SET sold_out=1")
    assert client.post("/lineups/save", data={"csrf": csrf, "revision": 1, "copy": "yesterday"}).status_code == 200
    with connect(settings) as con:
        lineup = lineups.current(con, "local-owner")
        assert lineup["confirmed_at"] is None
        assert lineups.items(con, lineup["id"])[0]["sold_out"] == 0
    assert client.post("/lineups/save", data={"csrf": csrf, "revision": 2, "menu_id": "not-owned"}).status_code == 404
    assert client.post("/lineups/save", data={"revision": 2, "menu_id": identifier}).status_code == 403


def test_notice_story_render_publish_idempotency_and_no_ai(app, client, csrf, photo, settings):
    confirmed_lineup(client, csrf, photo, settings)
    identifier = notice(client, csrf)
    assert notice(client, csrf) == identifier
    raw = client.get(f"/posts/{identifier}/image").content
    assert Image.open(io.BytesIO(raw)).size == (1080, 1920)
    assert client.get(f"/posts/{identifier}").url.path == f"/notices/{identifier}"
    assert client.post(f"/posts/{identifier}/generate", data={"csrf": csrf}).status_code == 409
    assert client.post(f"/posts/{identifier}/images", data={"csrf": csrf, "confirmed": "yes", "revision": 1}).status_code == 409
    ready(settings)
    attach(settings)
    calls = []
    def handler(request):
        calls.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path.endswith("/media"):
            body = parse_qs(request.content.decode())
            assert body["media_type"] == ["STORIES"] and "caption" not in body
            return httpx.Response(200, json={"id": "container"})
        if request.url.path.endswith("/container"):
            return httpx.Response(200, json={"status_code": "FINISHED"})
        if request.url.path.endswith("/media_publish"):
            return httpx.Response(200, json={"id": "story"})
        raise AssertionError(request.url)
    with httpx.Client(transport=httpx.MockTransport(handler)) as external:
        app.state.instagram.client = external
        for _ in range(2):
            assert client.post(f"/posts/{identifier}/publish", data={"csrf": csrf, "confirmed": "yes", "revision": 1}).status_code == 200
    assert sum(path.endswith("/media_publish") for _, path in calls) == 1
    with connect(settings) as con:
        assert not con.execute("SELECT 1 FROM ai_calls").fetchone()
        assert con.execute("SELECT COUNT(*) FROM credits WHERE reference=?", ("post:"+identifier,)).fetchone()[0] == 1
        post = owned_post(con, "local-owner", identifier)
        assert post["published_at"] and json.loads(post["published_snapshot"])["content_kind"] == "lineup"


def test_soldout_undo_invalidates_notice_and_pending_reservation(client, csrf, photo, settings):
    menu_id = confirmed_lineup(client, csrf, photo, settings)
    identifier = notice(client, csrf, aspect="portrait")
    assert client.post(f"/posts/{identifier}/finalize", data={"csrf": csrf, "confirmed": "yes", "revision": 1}).status_code == 200
    with connect(settings, write=True) as con:
        con.execute("INSERT INTO publish_schedules(post_id, owner_id, revision, due_at, state) VALUES (?, 'local-owner', 1, ?, 'pending')", (identifier, datetime.now(lineups.KST).isoformat()))
    assert client.post("/lineups/status", data={"csrf": csrf, "revision": 2, "menu_id": menu_id, "action": "soldout", "confirmed": "yes"}).status_code == 200
    with connect(settings) as con:
        assert con.execute("SELECT state FROM publish_schedules").fetchone()[0] == "cancelled"
    assert client.post(f"/posts/{identifier}/finalize", data={"csrf": csrf, "confirmed": "yes", "revision": 1}).status_code == 409
    soldout_id = notice(client, csrf, 3, menu_id=menu_id)
    with connect(settings) as con:
        post = owned_post(con, "local-owner", soldout_id)
        assert post["content_kind"] == "soldout" and post["media_product_type"] == "STORY"
    assert client.post("/lineups/status", data={"csrf": csrf, "revision": 3, "menu_id": menu_id, "action": "undo", "confirmed": "yes"}).status_code == 200
    assert client.post(f"/posts/{soldout_id}/finalize", data={"csrf": csrf, "confirmed": "yes", "revision": 1}).status_code == 409
    with connect(settings) as con:
        assert con.execute("SELECT COUNT(*) FROM notices").fetchone()[0] == 2


def test_scheduler_date_and_revision_guards_and_recovery(app, client, csrf, photo, settings, monkeypatch):
    confirmed_lineup(client, csrf, photo, settings)
    identifier = notice(client, csrf)
    with connect(settings, write=True) as con:
        con.execute("UPDATE lineups SET day='2000-01-01'")
        con.execute("INSERT INTO publish_schedules(post_id, owner_id, revision, due_at, state) VALUES (?, 'local-owner', 1, '2000-01-01T08:00:00+09:00', 'pending')", (identifier,))
    monkeypatch.setattr(app.state.instagram, "publish", lambda *a, **kw: pytest.fail("오래된 공지를 게시하면 안 된다"))
    jobs.tick(app)
    with connect(settings) as con:
        assert con.execute("SELECT state FROM publish_schedules").fetchone()[0] == "expired"
    with connect(settings, write=True) as con:
        con.execute("UPDATE publish_schedules SET state='processing'")
    jobs.recover(settings)
    jobs.tick(app)
    with connect(settings) as con:
        assert con.execute("SELECT state FROM publish_schedules").fetchone()[0] == "unknown"


def test_due_schedule_publishes_once_and_polls_existing_container(app, client, csrf, photo, settings, monkeypatch):
    confirmed_lineup(client, csrf, photo, settings)
    identifier = notice(client, csrf)
    assert client.post(f"/posts/{identifier}/finalize", data={"csrf": csrf, "confirmed": "yes", "revision": 1}).status_code == 200
    now = datetime.now(lineups.KST)
    with connect(settings, write=True) as con:
        con.execute("INSERT INTO publish_schedules(post_id, owner_id, revision, due_at, state) VALUES (?, 'local-owner', 1, ?, 'pending')", (identifier, (now-timedelta(seconds=1)).isoformat()))
    seen = []
    def publish(who, post_id, **kwargs):
        seen.append(kwargs["check_only"])
        with connect(settings, write=True) as con:
            con.execute("UPDATE posts SET publish_state=? WHERE id=?", ("published" if kwargs["check_only"] else "container_ready", post_id))
    monkeypatch.setattr(app.state.instagram, "publish", publish)
    jobs.tick(app, now)
    with pytest.raises(HTTPException):
        lineups.save_selection(settings, "local-owner", ["invalid"], 2)
    jobs.tick(app, now)
    jobs.tick(app, now)
    assert seen == [False, True]
    with connect(settings) as con:
        assert con.execute("SELECT state FROM publish_schedules").fetchone()[0] == "published"


def test_schedule_requires_final_current_revision_and_today(client, csrf, photo, settings):
    confirmed_lineup(client, csrf, photo, settings)
    identifier = notice(client, csrf)
    tomorrow = (datetime.now(lineups.KST)+timedelta(days=1)).replace(tzinfo=None).isoformat()
    with pytest.raises(HTTPException) as exc:
        lineups.schedule(settings, "local-owner", identifier, 1, tomorrow)
    assert exc.value.status_code == 422
    due = datetime.now(lineups.KST)+timedelta(seconds=10)
    if due.date().isoformat() != lineups.today():
        pytest.skip("자정 경계")
    local_due = due.replace(tzinfo=None).isoformat()
    with pytest.raises(HTTPException) as exc:
        lineups.schedule(settings, "local-owner", identifier, 1, local_due)
    assert exc.value.status_code == 409
    assert client.post(f"/posts/{identifier}/finalize", data={"csrf": csrf, "confirmed": "yes", "revision": 1}).status_code == 200
    lineups.schedule(settings, "local-owner", identifier, 1, local_due)
    lineups.schedule(settings, "local-owner", identifier, 1, local_due)
    with connect(settings) as con:
        assert con.execute("SELECT COUNT(*) FROM publish_schedules").fetchone()[0] == 1
    assert client.post(f"/notices/{identifier}/cancel", data={"csrf": csrf}).status_code == 200


def test_notice_renders_multiple_menus_and_rejects_unreadable_density(client, csrf, photo, settings):
    confirmed_lineup(client, csrf, photo, settings)
    with connect(settings) as con:
        lineup = lineups.current(con, "local-owner")
        item = lineups.items(con, lineup["id"])[0]
        shop = dict(con.execute("SELECT * FROM accounts WHERE id='local-owner'").fetchone())
    raw = lineups.render_notice(settings, shop, lineup, [item]*6, "portrait", "lineup")
    assert Image.open(io.BytesIO(raw)).size == (1080, 1350)
    with pytest.raises(HTTPException) as exc:
        lineups.render_notice(settings, shop, lineup, [item]*100, "portrait", "lineup")
    assert exc.value.status_code == 422
