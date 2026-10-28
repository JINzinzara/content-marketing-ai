"""리뷰의 다섯 재현 사례를 실제 저장 경로와 모의 HTTP로 검증한다."""
import json
from decimal import Decimal

import httpx
import pytest
from fastapi import HTTPException

import model_compare as mc
from app.costs import reconcile, reserve, spent
from app.db import connect, initialize, owned_post
from app.text_ai import Draft, validate_draft
from test_generation import enable
from test_model_compare import experiment, handler


@pytest.mark.parametrize('during', ['unchanged', 'edited', 'publishing', 'finalized', 'deleted'])
def test_initial_draft_obeys_revision_and_publication_guards(app, client, csrf, photo, settings, monkeypatch, during):
    enable(settings, '5')
    observed = []
    def delayed(owner_id, post_id, shop, post):
        observed.append(post_id)
        if during == 'edited':
            response = client.post(f'/posts/{post_id}/edit', data={
                'csrf': csrf, 'revision': 1, 'title': '사람 제목', 'caption': '사람이 저장한 최신 문구',
                'hashtags': '#쿠키', 'layout': 'split', 'aspect': 'portrait'}, follow_redirects=False)
            assert response.status_code == 303
        elif during == 'publishing':
            with connect(settings, write=True) as con:
                con.execute("UPDATE posts SET publish_state='creating' WHERE id=?", (post_id,))
        elif during == 'deleted':
            response = client.post(f'/posts/{post_id}/delete', data={
                'csrf': csrf, 'revision': 1, 'confirmed': 'yes'}, follow_redirects=False)
            assert response.status_code == 303
        elif during == 'finalized':
            response = client.post(f'/posts/{post_id}/finalize', data={
                'csrf': csrf, 'revision': 1, 'confirmed': 'yes'}, follow_redirects=False)
            assert response.status_code == 303
        return Draft(title='AI 제목', caption='새 AI 문구', hashtags='#쿠키', layout='frame')
    monkeypatch.setattr(app.state.ai, 'draft', delayed)
    response = client.post('/posts', data={'csrf': csrf, 'product_name': '스푸키쿠키', 'purpose': '신메뉴 안내'},
                           files={'photo': ('cookie.png', photo, 'image/png')}, follow_redirects=False)
    assert response.status_code == 303
    with connect(settings) as con:
        post = dict(con.execute('SELECT * FROM posts WHERE id=?', (observed[0],)).fetchone())
    if during == 'deleted':
        assert post['deleted_at'] is not None and post['caption'] == '스푸키쿠키' and post['revision'] == 1
    elif during == 'edited':
        assert post['caption'] == '사람이 저장한 최신 문구' and post['revision'] == 2
    elif during == 'publishing':
        assert post['caption'] == '스푸키쿠키' and post['revision'] == 1
    else:
        assert post['caption'] == '새 AI 문구' and post['revision'] == 2
    if during == 'finalized':
        assert post['rendered_revision'] == 1
        assert client.get(f'/posts/{post["id"]}/download').status_code == 409


@pytest.mark.parametrize('date', ['10/26~11/6', '10/26~11/06', '10.26~11.06', '10.26~11.06.', '10월 26일~11월 06일'])
def test_equivalent_sale_dates_pass(date):
    product = {'period': '10월 26일부터 11월 6일까지 할로윈 한정 판매'}
    validate_draft(Draft(title='스푸키쿠키', caption=f'스푸키쿠키\n\n{date} 한정 판매해요.', hashtags='', layout='frame'), product)


@pytest.mark.parametrize('new_fact', ['10/27~11/6', '가격 10.26원', '$11.06', 'USD 10.26', '가격 11.06', '가격 10.26~11.06', '달러 11.06', '중량 11.06 g', '중량 11.060g'])
def test_date_normalization_does_not_allow_other_dates_or_amounts(new_fact):
    with pytest.raises(HTTPException):
        validate_draft(Draft(title='스푸키쿠키', caption=new_fact, hashtags='', layout='frame'),
                       {'period': '10월 26일부터 11월 6일까지'})


def test_padded_date_is_a_completed_comparison_output(experiment):
    _, _, cfg, config = experiment
    out = config.parent / 'padded-date'
    manifest = mc.prepare(config, out, 'explore', 'gpt-5.4', 'original')
    def padded(request):
        response = handler(request)
        if request.url.path.endswith('/responses'):
            body = response.json()
            piece = body['output'][0]['content'][0]
            draft = json.loads(piece['text'])
            draft['caption'] = draft['caption'].replace('10월 26일~11월 6일', '10.26~11.06')
            piece['text'] = json.dumps(draft)
            return httpx.Response(200, json=body)
        return response
    with httpx.Client(transport=httpx.MockTransport(padded)) as client:
        mc.run(out, cfg, client)
    assert mc.read_json(mc.record_path(out, manifest['samples'][0]))['state'] == 'completed'


def test_existing_app_call_can_be_reconciled_with_audit_and_budget_still_holds(app, client, settings):
    client.get('/studio')
    settings.ai_budget_usd = Decimal('0.13')
    with connect(settings, write=True) as con:
        con.execute("INSERT INTO ai_calls(id, owner_id, purpose, model, state, reserved_microusd, cost_microusd, created_at) VALUES ('old-app-call','local-owner','image','gpt-image-2','completed',100000,120000,0)")
    with pytest.raises(HTTPException):
        with connect(settings, write=True) as con:
            reserve(con, settings, 'local-owner', 'draft', 'gpt-5.4', 1000)
    with connect(settings, write=True) as con:
        reconcile(con, 'old-app-call', 120000, '제공사 청구 내역 확인')
    with connect(settings) as con:
        row = con.execute("SELECT * FROM ai_cost_reconciliations").fetchone()
        assert (row['reserved_before_microusd'], row['cost_before_microusd'], row['cost_after_microusd']) == (100000, 120000, 120000)
        assert row['note'] == '제공사 청구 내역 확인' and spent(con) == 120000
    with pytest.raises(HTTPException):
        with connect(settings, write=True) as con:
            reserve(con, settings, 'local-owner', 'draft', 'gpt-5.4', 20000)
    with connect(settings, write=True) as con:
        assert reserve(con, settings, 'local-owner', 'draft', 'gpt-5.4', 1000)


@pytest.mark.parametrize('amount,note', [(-1, '확인'), (True, '확인'), (1, '')])
def test_invalid_reconciliation_does_not_change_ledger(app, settings, amount, note):
    with pytest.raises(ValueError):
        with connect(settings, write=True) as con:
            reconcile(con, 'missing', amount, note)
    with connect(settings) as con:
        assert con.execute('SELECT COUNT(*) FROM ai_cost_reconciliations').fetchone()[0] == 0


def test_legacy_database_adds_reconciliation_table_without_losing_calls(app, client, settings):
    client.get('/studio')
    with connect(settings, write=True) as con:
        con.execute('DROP TABLE ai_cost_reconciliations')
        con.execute("INSERT INTO ai_calls(id,owner_id,purpose,model,state,reserved_microusd,created_at) VALUES ('legacy','local-owner','draft','gpt-5.4','pending',123,0)")
    initialize(settings)
    initialize(settings)
    with connect(settings) as con:
        assert con.execute('SELECT COUNT(*) FROM ai_calls WHERE id="legacy"').fetchone()[0] == 1
        assert con.execute('SELECT COUNT(*) FROM ai_cost_reconciliations').fetchone()[0] == 0


def test_budget_and_app_settlement_cli_never_construct_http_client(app, client, settings, monkeypatch, capsys):
    client.get('/studio')
    with connect(settings, write=True) as con:
        con.execute("INSERT INTO ai_calls(id,owner_id,purpose,model,state,reserved_microusd,cost_microusd,created_at) VALUES ('app-overrun','local-owner','image','gpt-image-2','completed',100000,120000,0)")
    monkeypatch.setattr(mc.Settings, 'from_env', lambda: settings)
    monkeypatch.setattr(mc.httpx, 'Client', lambda *a, **k: pytest.fail('정산/원장 조회는 네트워크 금지'))
    mc.main(['budget'])
    assert 'app-overrun' in capsys.readouterr().out
    mc.main(['settle-call', '--call-id', 'app-overrun', '--cost-usd', '0.12', '--note', '청구 확인'])
    with connect(settings) as con:
        assert con.execute("SELECT reserved_microusd FROM ai_calls WHERE id='app-overrun'").fetchone()[0] == 120000


def test_direct_app_settlement_rejects_comparison_call(experiment, monkeypatch):
    out, _, cfg, _ = experiment
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        mc.run(out, cfg, client, limit=1)
    with connect(cfg) as con:
        call_id = con.execute('SELECT id FROM ai_calls').fetchone()[0]
    monkeypatch.setattr(mc.Settings, 'from_env', lambda: cfg)
    with pytest.raises(SystemExit) as raised:
        mc.main(['settle-call', '--call-id', call_id, '--cost-usd', '0.01', '--note', '확인'])
    assert raised.value.code == 1


def test_scope_reflects_exploration_and_actual_completion(experiment):
    final, _, cfg, config = experiment
    out = config.parent / 'one-input'
    mc.prepare(config, out, 'explore', 'gpt-image-2', 'original')
    before = mc.read_json(out/'results.json')['evidence_scope']
    assert '입력 1장(원본 1장·합성 0장)' in before and '완료 0건' in before
    assert '입력 1장(원본 1장·합성 0장)' in (out/'review.html').read_text()
    assert '전체 계획 20건' in mc.read_json(final/'results.json')['evidence_scope']
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        mc.run(out, cfg, client)
    assert '완료 1건' in mc.read_json(out/'results.json')['evidence_scope']


def test_summary_distinguishes_estimate_mixed_and_reconciled_cost(experiment):
    _, _, cfg, config = experiment
    out = config.parent / 'basis'
    manifest = mc.prepare(config, out, 'explore', 'gpt-image-2')
    assert mc.report(out)[0]['cost_basis'] == 'not_measured'
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        mc.run(out, cfg, client, limit=2)
    assert mc.report(out)[0]['cost_basis'] == 'usage_uncached_upper_estimate'
    mc.settle(out, manifest['samples'][0]['id'], '0.01', '청구 확인 1', cfg)
    assert mc.report(out)[0]['cost_basis'] == 'mixed'
    mc.settle(out, manifest['samples'][1]['id'], '0.01', '청구 확인 2', cfg)
    row = mc.report(out)[0]
    assert row['cost_basis'] == 'manual_billing_reconciliation' and row['known_cost_usd'] == '0.02'
    with connect(cfg) as con:
        assert con.execute('SELECT COUNT(*) FROM ai_cost_reconciliations').fetchone()[0] == 2
