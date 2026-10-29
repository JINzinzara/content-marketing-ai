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
