"""최종 리뷰에서 확인한 만료 공지 발행과 새 게시물 목록의 누락을 검증한다."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import lineups
from app.db import connect
from test_daily_workflow import confirmed_lineup, notice
from test_instagram import attach, ready


@pytest.mark.parametrize("status", ["FINISHED", "PUBLISHED"])
def test_expired_notice_check_can_read_but_cannot_publish(app, client, csrf, settings, photo, status):
    confirmed_lineup(client, csrf, photo, settings)
    identifier = notice(client, csrf)
    ready(settings)
    attach(settings)
    with connect(settings, write=True) as con:
        con.execute("UPDATE posts SET publish_state='container_ready', container_id='old-container' WHERE id=?", (identifier,))
        con.execute("UPDATE lineups SET day=?", ((datetime.now(lineups.KST).date()-timedelta(days=1)).isoformat(),))
    calls = []
    def handler(request):
        calls.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json={"status_code": status})
        return httpx.Response(200, json={"id": "wrong-old-story"})
    with httpx.Client(transport=httpx.MockTransport(handler)) as external:
        app.state.instagram.client = external
        response = client.post(f"/posts/{identifier}/check", data={"csrf": csrf})
    assert response.status_code == (409 if status == "FINISHED" else 200)
    assert calls == ["GET"]


def test_head_refresh_keeps_new_gap_reachable_after_old_tail_was_exhausted(app, client, csrf, settings, monkeypatch):
    settings.history_page_size = 2
    phase, afters = [0], []
    values = [("a", 1), ("b", 2), ("c", 3), ("d", 4), ("e", 5)]
    def sync(who, after="", **kwargs):
        afters.append(after)
        page = values[:2] if phase[0] == 0 else values[-2:] if not after else values[1:3]
        with connect(settings, write=True) as con:
            for identifier, day in page:
                con.execute("INSERT OR IGNORE INTO history(owner_id,media_id,caption,media_product_type,published_at) VALUES (?, ?, ?, 'FEED', ?)", (who, identifier, identifier, datetime(2026, 1, day, tzinfo=timezone.utc).isoformat()))
        return "gap-cursor" if phase[0] and not after else ""
    monkeypatch.setattr(app.state.instagram, "sync_history", sync)
    monkeypatch.setattr(app.state.instagram, "collect_metrics", lambda *args: {})
    client.post("/analytics/refresh", data={"csrf": csrf})
    phase[0] = 1
    client.post("/analytics/refresh", data={"csrf": csrf})
    with connect(settings) as con:
        assert con.execute("SELECT next_cursor FROM analytics_sync_state").fetchone()[0] == "gap-cursor"
    client.post("/analytics/refresh", data={"csrf": csrf, "older": "yes", "page": 3})
    assert afters[-1] == "gap-cursor"
    with connect(settings) as con:
        assert con.execute("SELECT 1 FROM history WHERE media_id='c'").fetchone()
