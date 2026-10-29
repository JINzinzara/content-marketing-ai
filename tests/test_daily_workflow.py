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

from app import analytics, briefings, jobs, lineups
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


def seed_metrics(settings, now, *, age=24):
    with connect(settings, write=True) as con:
        for identifier, purpose, fmt in [("a", "상품 소개", "FEED"), ("b", "상품 소개", "FEED"), ("lineup", "라인업", "FEED"), ("story", "상품 소개", "STORY"), ("unknown", "", "FEED")]:
            con.execute("INSERT INTO history(owner_id, media_id, caption, purpose, promotion, media_product_type, published_at) VALUES ('local-owner', ?, ?, ?, 'organic', ?, ?)", (identifier, identifier, purpose, fmt, (now-timedelta(hours=age)).isoformat()))


def test_scheduled_observation_and_briefing_no_new_data(app, client, csrf, settings, monkeypatch):
    now = datetime.now(timezone.utc)
    seed_metrics(settings, now)
    calls = []
    def collect(who, identifier):
        calls.append(identifier)
        with connect(settings, write=True) as con:
            published = con.execute("SELECT published_at FROM history WHERE media_id=?", (identifier,)).fetchone()[0]
            metrics = {"reach": {"status": "available", "value": 100}, "saved": {"status": "available", "value": 5 if identifier == "a" else 0}, "shares": {"status": "unsupported", "value": None}}
            con.execute("INSERT INTO observations(owner_id, media_id, observed_at, published_at, metrics) VALUES (?, ?, ?, ?, ?)", (who, identifier, now.isoformat(), published, json.dumps(metrics)))
    monkeypatch.setattr(app.state.instagram, "collect_metrics", collect)
    due = now.astimezone(lineups.KST).replace(second=0, microsecond=0)
    with connect(settings, write=True) as con:
        con.execute("INSERT INTO briefing_settings(owner_id, mode, local_time, enabled_at) VALUES ('local-owner', 'daily', ?, ?)", (due.strftime("%H:%M"), (due-timedelta(days=1)).isoformat()))
    jobs.tick(app, now)
    jobs.tick(app, now)
    assert sorted(calls) == ["a", "b"]
    with connect(settings) as con:
        runs = con.execute("SELECT * FROM briefing_runs").fetchall()
        assert len(runs) == 1 and runs[0]["state"] == "ready"
        before = json.loads(runs[0]["content"])
    jobs.tick(app, now+timedelta(days=1))
    with connect(settings) as con:
        last = con.execute("SELECT * FROM briefing_runs ORDER BY id DESC LIMIT 1").fetchone()
        assert last["state"] == "no_new_data" and json.loads(last["content"]) == before
    html = client.get("/briefing").text
    assert "새 반응이 없어" in html and "저장률 0.00%" in html and "저장률 5.00%" in html
    assert "story" not in {row["media_id"] for row in analytics.report(settings, "local-owner")["evidence"]}


def test_briefing_settings_weekdays_dates_and_no_retroactive_delivery(client, csrf, settings):
    assert client.post("/briefing/settings", data={"csrf": csrf, "mode": "weekdays", "local_time": "09:00"}).status_code == 422
    assert client.post("/briefing/settings", data={"csrf": csrf, "mode": "dates", "local_time": "09:00", "dates": "2026-02-30"}).status_code == 422
    setting = {"mode": "weekdays", "local_time": "09:00", "weekdays": "[3]", "dates": "[]", "enabled_at": "2026-10-28T10:00:00+09:00"}
    assert briefings.due_slot(setting, datetime(2026, 10, 29, 9, tzinfo=lineups.KST)) == "2026-10-29T09:00:00+09:00"
    assert briefings.due_slot(setting, datetime(2026, 10, 30, 9, tzinfo=lineups.KST)) is None
    setting.update(mode="dates", dates='["2026-10-30"]')
    assert briefings.due_slot(setting, datetime(2026, 10, 30, 9, tzinfo=lineups.KST))
    setting["enabled_at"] = "2026-10-30T10:00:00+09:00"
    assert briefings.due_slot(setting, datetime(2026, 10, 30, 11, tzinfo=lineups.KST)) is None


def test_optional_recommendation_changes_only_target_draft(client, csrf, new_post, settings):
    first, other = new_post(), new_post()
    idea = {"observation": "관찰", "next_content": "대표 사진의 여백을 바꿔보세요", "time_hypothesis": "가설", "limitations": "매출 효과 미확인"}
    with connect(settings, write=True) as con:
        identifier = con.execute("INSERT INTO recommendations(owner_id, evidence_hash, content, created_at) VALUES ('local-owner', 'evidence', ?, 1)", (json.dumps(idea),)).lastrowid
        before = owned_post(con, "local-owner", first)
        shop = dict(con.execute("SELECT * FROM accounts WHERE id='local-owner'").fetchone())
    assert client.get(f"/posts/new?recommendation_id={identifier}").status_code == 200
    assert client.post(f"/recommendations/{identifier}/choose", data={"csrf": csrf, "target": f"{first}:1", "action": "applied"}).status_code == 200
    with connect(settings) as con:
        post = owned_post(con, "local-owner", first)
        assert post["recommendation_id"] == identifier and post["revision"] == 2
        assert post["style_snapshot"] == before["style_snapshot"] and post["facts"] == before["facts"]
        assert briefings.guidance(con, post) == idea
        assert owned_post(con, "local-owner", other)["recommendation_id"] is None
        assert dict(con.execute("SELECT * FROM accounts WHERE id='local-owner'").fetchone()) == shop
        assert not con.execute("SELECT 1 FROM publish_schedules").fetchone()
    assert client.post(f"/recommendations/{identifier}/choose", data={"csrf": csrf, "target": f"{first}:1", "action": "ignored"}).status_code == 409
    assert client.post(f"/recommendations/{identifier}/choose", data={"csrf": csrf, "target": f"{first}:2", "action": "ignored"}).status_code == 200
    with connect(settings) as con:
        assert owned_post(con, "local-owner", first)["recommendation_id"] is None
        assert {row[0] for row in con.execute("SELECT action FROM recommendation_events")} == {"shown", "applied", "ignored"}


def test_additive_schema_initialization_preserves_existing_data(client, settings, new_post):
    identifier = new_post()
    with connect(settings) as con:
        before = owned_post(con, "local-owner", identifier)
    initialize(settings)
    initialize(settings)
    with connect(settings) as con:
        assert owned_post(con, "local-owner", identifier) == before


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


def test_api_error_does_not_become_a_zero_observation_or_a_new_insight(app, client, csrf, settings, monkeypatch):
    now = datetime.now(timezone.utc)
    seed_metrics(settings, now)
    from app.instagram import InstagramError
    monkeypatch.setattr(app.state.instagram, "collect_metrics", lambda *args: (_ for _ in ()).throw(InstagramError("권한 확인", kind="permission")))
    _, errors = briefings.collect(settings, app.state.instagram, "local-owner", now)
    with connect(settings, write=True) as con:
        con.execute("INSERT INTO briefing_runs(owner_id, scheduled_for, created_at, state) VALUES ('local-owner', 'manual-test', ?, 'running')", (now.isoformat(),))
    briefings.prepare(settings, "local-owner", "manual-test", errors)
    assert "아직 모이고" in client.get("/briefing").text
    with connect(settings) as con:
        assert "권한 확인" in con.execute("SELECT message FROM briefing_runs ORDER BY id DESC LIMIT 1").fetchone()[0]
    with connect(settings) as con:
        assert not con.execute("SELECT 1 FROM observations").fetchone()


def test_published_lineup_and_story_snapshot_excluded_even_if_current_fields_look_promotional(client, new_post, settings):
    for index, (kind, fmt) in enumerate([("lineup", "FEED"), ("promotion", "STORY")]):
        identifier = new_post()
        now = datetime.now(timezone.utc)
        with connect(settings, write=True) as con:
            con.execute("UPDATE posts SET ig_media_id=?, promotion='organic', published_snapshot=? WHERE id=?", (f"media-{index}", json.dumps({"content_kind": kind, "media_product_type": fmt}), identifier))
            con.execute("INSERT INTO observations(owner_id, media_id, observed_at, published_at, metrics) VALUES ('local-owner', ?, ?, ?, ?)", (f"media-{index}", now.isoformat(), (now-timedelta(hours=24)).isoformat(), json.dumps({"reach":{"status":"available","value":100},"saved":{"status":"available","value":5}})))
    assert not analytics.report(settings, "local-owner")["evidence"]


def test_selected_idea_reaches_caption_and_image_generation(app, client, csrf, new_post, settings, monkeypatch):
    identifier = new_post()
    idea = {"observation": "근거", "next_content": "여백을 살린 구도", "time_hypothesis": "가설", "limitations": "한계"}
    with connect(settings, write=True) as con:
        rec_id = con.execute("INSERT INTO recommendations(owner_id, evidence_hash, content, created_at) VALUES ('local-owner', 'selected', ?, 1)", (json.dumps(idea),)).lastrowid
    briefings.apply(settings, "local-owner", rec_id, identifier, 1, "applied")
    from app.text_ai import Draft
    captured = []
    def generate(owner_id, purpose, payload, *args, **kwargs):
        captured.append(payload)
        return Draft(title="가을 밤 라테", caption="가을 밤 라테\n\n직접 제조한 밤 크림", hashtags="#카페", layout="split")
    monkeypatch.setattr(app.state.ai, "generate", generate)
    with connect(settings) as con:
        post = owned_post(con, "local-owner", identifier)
        shop = dict(con.execute("SELECT * FROM accounts WHERE id='local-owner'").fetchone())
    app.state.ai.draft("local-owner", identifier, shop, post)
    assert captured[0]["optional_idea_for_this_draft"] == idea
    assert captured[0]["confirmed_product"]["facts"] == post["facts"]
    from test_image_generation import enable_image
    enable_image(settings)
    job = app.state.image_ai.queue("local-owner", identifier, post["revision"])
    with connect(settings) as con:
        prompt = con.execute("SELECT prompt FROM image_generations WHERE id=?", (job,)).fetchone()[0]
    assert idea["next_content"] in prompt and "상품의 색과 디테일을 재해석하지 말고" in prompt


def test_collection_failure_is_not_retried_every_worker_tick(app, client, settings, monkeypatch):
    client.get("/studio")
    now = datetime.now(timezone.utc)
    seed_metrics(settings, now)
    from app.instagram import InstagramError
    calls = []
    def fail(*args):
        calls.append(args)
        raise InstagramError("일시 오류")
    monkeypatch.setattr(app.state.instagram, "collect_metrics", fail)
    for _ in range(2):
        _, errors = briefings.collect(settings, app.state.instagram, "local-owner", now)
        assert errors
    assert len(calls) == 2  # 두 게시물을 각각 한 번, 매 틱마다 재호출하지 않는다.
    with connect(settings) as con:
        assert not con.execute("SELECT 1 FROM observations").fetchone()
        assert con.execute("SELECT COUNT(*) FROM metric_collection_jobs WHERE state='error'").fetchone()[0] == 2


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


def test_publication_worker_does_not_wait_for_metric_collection(app, client, csrf, photo, settings, monkeypatch):
    confirmed_lineup(client, csrf, photo, settings)
    identifier = notice(client, csrf)
    now = datetime.now(lineups.KST)
    with connect(settings, write=True) as con:
        con.execute("INSERT INTO publish_schedules(post_id, owner_id, revision, due_at, state) VALUES (?, 'local-owner', 1, ?, 'pending')", (identifier, (now-timedelta(seconds=1)).isoformat()))
        con.execute("INSERT INTO briefing_settings(owner_id, mode, local_time, enabled_at) VALUES ('local-owner', 'daily', '00:00', ?)", ((now-timedelta(days=1)).isoformat(),))
    monkeypatch.setattr(briefings, "collect", lambda *args: pytest.fail("반응 수집은 예약 게시 고리를 막으면 안 된다"))
    published = []
    def publish(*args, **kwargs):
        published.append(args)
        with connect(settings, write=True) as con:
            con.execute("UPDATE posts SET publish_state='published' WHERE id=?", (identifier,))
    monkeypatch.setattr(app.state.instagram, "publish", publish)
    jobs.tick(app, now, kind="publishing")
    assert len(published) == 1


def test_analytics_can_refresh_format_without_ai_or_restoring_deleted_references(app, client, csrf, settings):
    attach(settings)
    with connect(settings, write=True) as con:
        con.execute("INSERT INTO history(owner_id, media_id, deleted_at) VALUES ('local-owner', 'hidden', 1)")
    def handler(request):
        assert request.method == "GET" and request.url.path.endswith("/media")
        assert "media_product_type" in request.url.params["fields"]
        return httpx.Response(200, json={"data": [{"id": "feed", "media_type": "IMAGE", "media_product_type": "FEED"}, {"id": "hidden", "media_product_type": "FEED"}]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as external:
        app.state.instagram.client = external
        response = client.post("/instagram/sync", data={"csrf": csrf, "return_to": "analytics"})
    assert response.status_code == 200 and response.url.path == "/analytics"
    with connect(settings) as con:
        assert con.execute("SELECT media_product_type FROM history WHERE media_id='feed'").fetchone()[0] == "FEED"
        assert con.execute("SELECT deleted_at FROM history WHERE media_id='hidden'").fetchone()[0] == 1
        assert not con.execute("SELECT 1 FROM ai_calls").fetchone()
