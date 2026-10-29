"""이름 등록 → 공지 사진 선택, 과거 반응 자동 조회, 추천에서 새 제작을 시작하는 경로."""

import io
import json
from datetime import datetime, timedelta, timezone

import httpx
from PIL import Image

from backend import analytics
from backend.db import connect, ensure_account, owned_post
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


def test_old_posts_are_visible_without_purpose_or_observations(client, csrf, settings):
    with connect(settings, write=True) as con:
        for index in range(45):
            con.execute("INSERT INTO history(owner_id,media_id,caption,media_product_type,published_at) VALUES ('local-owner', ?, ?, 'FEED', ?)", (str(index), '지난 상품 '+str(index), (datetime(2025,1,1,tzinfo=timezone.utc)+timedelta(days=index)).isoformat()))
    page = client.get('/analytics')
    assert page.status_code == 200 and '지난 상품 44' in page.text
    assert '가져온 게시물의 목적 확인' not in page.text and 'name="promotion"' not in page.text
    assert '이전 게시물 더 보기' in page.text
    assert '지난 상품 0' in client.get('/analytics?p=3').text
    assert not analytics.report(settings, 'local-owner')['evidence']


def test_background_refresh_reads_unknown_purpose_without_ai(app, client, csrf, settings):
    attach(settings)
    calls = []
    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith('/media'):
            return httpx.Response(200, json={'data': [{'id': 'old', 'caption': '예전 게시물', 'media_type': 'IMAGE', 'media_product_type': 'FEED', 'timestamp': '2025-01-01T00:00:00Z'}]})
        name = request.url.params['metric']
        return httpx.Response(200, json={'data': [] if name == 'shares' else [{'name': name, 'values': [{'value': 0}]}]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as external:
        app.state.instagram.client = external
        response = client.post('/analytics/refresh', data={'csrf': csrf}, headers={'Accept': 'application/json'})
    assert response.status_code == 202
    assert len(calls) == 7
    state = client.get('/analytics/status').json()
    assert state['state'] == 'completed'
    page = client.get('/analytics')
    assert '예전 게시물' in page.text and 'data-auto="no"' in page.text
    with connect(settings) as con:
        assert con.execute('SELECT purpose FROM history').fetchone()[0] == ''
        metrics = json.loads(con.execute('SELECT metrics FROM observations').fetchone()[0])
        assert metrics['saved']['value'] == 0 and metrics['shares']['value'] is None
        assert not con.execute('SELECT 1 FROM ai_calls').fetchone()
    assert not analytics.report(settings, 'local-owner')['evidence']


def test_older_remote_pages_and_saved_cursor(app, client, csrf, settings, monkeypatch):
    settings.history_page_size = 2
    attach(settings)
    seen = []
    def sync(who, after='', **kwargs):
        seen.append(after)
        values = [('a', '2026-10-29'), ('b','2026-10-28')] if not after else [('c','2026-10-27')]
        with connect(settings, write=True) as con:
            for identifier, day in values:
                con.execute("INSERT OR IGNORE INTO history(owner_id,media_id,caption,media_product_type,published_at) VALUES (?, ?, ?, 'FEED', ?)", (who, identifier, '과거글 '+identifier, day+'T00:00:00Z'))
        return 'older-cursor' if not after else ''
    monkeypatch.setattr(app.state.instagram, 'sync_history', sync)
    monkeypatch.setattr(app.state.instagram, 'collect_metrics', lambda *args: {})
    assert client.post('/analytics/refresh', data={'csrf': csrf}).status_code == 200
    assert '이전 게시물 더 불러오기' in client.get('/analytics').text
    assert client.post('/analytics/refresh', data={'csrf': csrf, 'older': 'yes', 'page': 2}).status_code == 200
    assert seen == ['', 'older-cursor']
    assert '과거글 c' in client.get('/analytics?p=2').text
    app.state.analytics_refreshes.clear()
    client.post('/analytics/refresh', data={'csrf': csrf})
    with connect(settings) as con:
        assert con.execute('SELECT next_cursor FROM analytics_sync_state').fetchone()[0] == ''


def test_refresh_duplicates_share_one_background_task(app):
    class Tasks:
        def __init__(self): self.calls = []
        def add_task(self, *args): self.calls.append(args)
    tasks = Tasks()
    assert analytics.queue_refresh(app, 'owner', tasks, 1) == 1
    assert analytics.queue_refresh(app, 'owner', tasks, 2) == 1
    assert len(tasks.calls) == 1
    assert analytics.refresh_state(app, 'other')['state'] == 'idle'


def test_unsafe_permalink_is_not_linked(client, csrf, settings):
    with connect(settings, write=True) as con:
        con.execute("INSERT INTO history(owner_id,media_id,caption,permalink) VALUES ('local-owner','bad','과거글','javascript:alert(1)')")
    assert 'javascript:alert' not in client.get('/analytics').text


def test_briefing_only_shows_schedule_content_and_one_creation_button(client, csrf, settings):
    html = client.get('/briefing').text
    assert html.count('추천으로 새 게시물 만들기') == 1
    for obsolete in ['추천 없이 새 게시물', '이번 제작 초안', '추천 활용 기록', '비교 근거 · 게시물', 'AI 아이디어 요청']:
        assert obsolete not in html
    assert 'data-period="dates"' in html and 'type="date"' in html
    assert '기본 제작 제안' in html


def test_recommendation_starts_new_post_and_does_not_touch_existing_draft(client, csrf, settings, new_post, photo):
    existing = new_post()
    before = analytics.report(settings, 'local-owner')
    with connect(settings) as con:
        original = owned_post(con, 'local-owner', existing)
    response = client.post('/briefing/create-post', data={'csrf': csrf, 'evidence_hash': before['evidence_hash']}, follow_redirects=False)
    assert response.status_code == 303 and response.headers['location'].startswith('/posts/new?recommendation_id=')
    rec_id = int(response.headers['location'].split('=')[-1])
    assert 'name="recommendation_id"' in client.get(response.headers['location']).text
    response = client.post('/posts', data={'csrf': csrf, 'product_name': '새 쿠키', 'facts': '오늘의 메뉴', 'purpose': '신메뉴 안내', 'recommendation_id': rec_id}, files={'photo': ('cookie.png', photo, 'image/png')}, follow_redirects=False)
    assert response.status_code == 303
    identifier = response.headers['location'].split('/')[-1]
    assert identifier != existing
    with connect(settings) as con:
        post = owned_post(con, 'local-owner', identifier)
        assert post['recommendation_id'] == rec_id and post['facts'] == '오늘의 메뉴'
        assert owned_post(con, 'local-owner', existing) == original
        assert json.loads(con.execute('SELECT content FROM recommendations WHERE id=?',(rec_id,)).fetchone()[0])['source'] == 'starter'
        assert con.execute("SELECT COUNT(*) FROM recommendation_events WHERE action='applied' AND post_id=?",(identifier,)).fetchone()[0] == 1
        assert not con.execute('SELECT 1 FROM ai_calls').fetchone()


def test_recommendation_owner_and_changed_evidence_are_checked(client, csrf, settings, photo):
    with connect(settings, write=True) as con:
        ensure_account(con,'other')
        identifier=con.execute("INSERT INTO recommendations(owner_id,evidence_hash,content,created_at) VALUES ('other','hash','{}',1)").lastrowid
    assert client.get(f'/posts/new?recommendation_id={identifier}').status_code == 404
    assert client.post('/posts', data={'csrf':csrf,'product_name':'쿠키','purpose':'신메뉴 안내','recommendation_id':identifier},files={'photo':('photo.png',photo,'image/png')}).status_code == 404
    assert client.post('/briefing/create-post',data={'csrf':csrf,'evidence_hash':'changed'}).status_code == 409
    assert client.post('/analytics/refresh', data={}).status_code == 403


def test_data_based_recommendation_keeps_evidence_and_uses_no_extra_ai(client, csrf, settings):
    now = datetime.now(timezone.utc)
    with connect(settings, write=True) as con:
        for identifier, saved in [('a', 1), ('b', 4)]:
            published = (now-timedelta(hours=24)).isoformat()
            con.execute("INSERT INTO history(owner_id,media_id,caption,purpose,promotion,media_product_type,published_at) VALUES ('local-owner', ?, ?, '상품 소개','organic','FEED',?)", (identifier, '기존 메뉴 '+identifier, published))
            metrics = {'reach': {'value':100,'status':'available'},'saved':{'value':saved,'status':'available'}}
            con.execute("INSERT INTO observations(owner_id,media_id,published_at,observed_at,metrics) VALUES ('local-owner', ?, ?, ?, ?)",(identifier,published,now.isoformat(),json.dumps(metrics)))
    info = analytics.report(settings, 'local-owner')
    assert len(info['evidence']) == 2
    html = client.get('/briefing').text
    assert '기본 제작 제안' not in html and '기존 메뉴 b' in html
    response = client.post('/briefing/create-post', data={'csrf':csrf,'evidence_hash':info['evidence_hash']})
    assert response.status_code == 200 and '/posts/new?recommendation_id=' in str(response.url)
    with connect(settings) as con:
        row = con.execute('SELECT * FROM recommendations').fetchone()
        assert row['evidence_hash'] == 'rules:'+info['evidence_hash']
        assert json.loads(row['content'])['source'] == 'observed_rule'
        assert not con.execute('SELECT 1 FROM ai_calls').fetchone()


def test_refresh_failure_keeps_saved_data_and_does_not_loop(app, client, csrf, settings, monkeypatch):
    attach(settings)
    from backend.instagram import InstagramError
    with connect(settings, write=True) as con:
        con.execute("INSERT INTO history(owner_id,media_id,caption,media_product_type) VALUES ('local-owner','saved','기존 게시물','FEED')")
    def fail(*args, **kwargs):
        raise InstagramError('연결 오류', kind='auth')
    monkeypatch.setattr(app.state.instagram,'sync_history',fail)
    client.post('/analytics/refresh',data={'csrf':csrf})
    html = client.get('/analytics').text
    assert '기존 게시물' in html and 'data-auto="no"' in html
    assert client.get('/analytics/status').json()['state'] == 'error'
    with connect(settings) as con:
        assert not con.execute('SELECT 1 FROM observations').fetchone()


def test_expired_permission_stops_batch_instead_of_repeating_every_post(app, client, csrf, settings, monkeypatch):
    attach(settings)
    with connect(settings, write=True) as con:
        for key in ['first','second','third']:
            con.execute("INSERT INTO history(owner_id,media_id,caption,media_product_type) VALUES ('local-owner', ?, ?, 'FEED')", (key,key))
    calls=[]
    from backend.instagram import InstagramError
    monkeypatch.setattr(app.state.instagram,'sync_history',lambda *args, **kwargs:'')
    def fail(*args):
        calls.append(args)
        raise InstagramError('권한 확인',kind='permission')
    monkeypatch.setattr(app.state.instagram,'collect_metrics',fail)
    client.post('/analytics/refresh',data={'csrf':csrf})
    assert len(calls)==1 and client.get('/analytics/status').json()['state']=='error'
