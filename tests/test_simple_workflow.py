"""이름 등록 → 공지 사진 선택, 과거 반응 자동 조회, 추천에서 새 제작을 시작하는 경로."""

import io
import json
from datetime import datetime, timedelta, timezone

import httpx
from PIL import Image

from app.db import connect, ensure_account, owned_post
from test_daily_workflow import confirmed_lineup, notice
from test_instagram import attach


def test_register_menu_name_without_photo_and_choose_photo_for_notice(client, csrf, settings, photo):
    response = client.post('/menus', data={'csrf': csrf, 'name': '스푸키쿠키'})
    assert response.status_code == 200 and 'id="menu-photo"' not in response.text
    with connect(settings) as con:
        item = dict(con.execute('SELECT * FROM menus').fetchone())
    assert item['name'] == '스푸키쿠키' and item['filename'] == ''
    assert client.get(f'/menus/{item["id"]}/image').status_code == 404
    client.post('/lineups/save', data={'csrf': csrf, 'revision': 1, 'menu_id': item['id']})
    client.post('/lineups/confirm', data={'csrf': csrf, 'revision': 2, 'confirmed': 'yes'})
    identifier = notice(client, csrf)
    before = client.get(f'/posts/{identifier}/image').content
    assert Image.open(io.BytesIO(before)).size == (1080, 1920)
    response = client.post(f'/notices/{identifier}/photo', data={'csrf': csrf, 'revision': 1}, files={'photo': ('image.png', photo, 'image/png')})
    assert response.status_code == 200 and '사진 바꾸기' in response.text
    after = client.get(f'/posts/{identifier}/image').content
    assert after != before
    with connect(settings) as con:
        post = owned_post(con, 'local-owner', identifier)
        assert post['revision'] == 2 and json.loads(post['notice_snapshot'])['photo_file']
        assert con.execute('SELECT filename FROM menus').fetchone()[0] == ''
        assert not con.execute('SELECT 1 FROM ai_calls').fetchone()
    assert client.post(f'/notices/{identifier}/photo', data={'csrf': csrf, 'revision': 2, 'remove': 'yes'}).status_code == 200
    assert client.get(f'/posts/{identifier}/image').content == before


def test_notice_photo_keeps_old_file_and_cancels_reservation(client, csrf, settings, photo):
    confirmed_lineup(client, csrf, photo, settings)
    identifier = notice(client, csrf)
    client.post(f'/posts/{identifier}/finalize', data={'csrf': csrf, 'revision': 1, 'confirmed': 'yes'})
    with connect(settings, write=True) as con:
        post = owned_post(con, 'local-owner', identifier)
        old_file = settings.data_dir/'finished'/post['finished_file']
        old_bytes = old_file.read_bytes()
        con.execute("INSERT INTO publish_schedules(post_id,owner_id,revision,due_at,state) VALUES (?, 'local-owner', 1, ?, 'pending')", (identifier, datetime.now(timezone.utc).isoformat()))
    assert client.post(f'/notices/{identifier}/photo', data={'csrf': csrf, 'revision': 1}, files={'photo': ('photo.png', photo, 'image/png')}).status_code == 200
    assert old_file.read_bytes() == old_bytes
    with connect(settings) as con:
        assert con.execute('SELECT state FROM publish_schedules').fetchone()[0] == 'cancelled'
    assert client.get(f'/posts/{identifier}/download').status_code == 409
    assert client.post(f'/notices/{identifier}/photo', data={'csrf': csrf, 'revision': 1}, files={'photo': ('photo.png', photo, 'image/png')}).status_code == 409
    assert client.post(f'/notices/{identifier}/photo', data={'csrf': csrf, 'revision': 2}, files={'photo': ('bad.png', b'bad', 'image/png')}).status_code == 422
