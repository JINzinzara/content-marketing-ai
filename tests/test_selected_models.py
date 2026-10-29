"""확정 모델 요청·단가를 검증한다."""
import json
from decimal import Decimal
import httpx
from app.db import connect
from test_generation import response_body


def test_selected_models_use_separate_rates_and_requests(app, client, settings):
    client.get('/studio')
    settings.openai_enabled, settings.openai_key = True, 'fake-only'
    settings.ai_budget_usd = Decimal('5')
    settings.input_ceiling_rate, settings.output_rate = Decimal('2.50'), Decimal('15')
    seen = []
    def handler(request):
        payload = json.loads(request.content)
        seen.append((request.url.path, payload))
        if request.url.path.endswith('input_tokens'):
            return httpx.Response(200, json={'input_tokens':100})
        return httpx.Response(200, json=response_body())
    from app.text_ai import Draft
    with httpx.Client(transport=httpx.MockTransport(handler)) as external:
        app.state.ai.client = external
        for purpose in ('style','draft'):
            app.state.ai.generate('local-owner', purpose, {}, Draft)
    generations = [p for path, p in seen if path.endswith('/responses')]
    assert [p['model'] for p in generations] == ['gpt-5.4-mini','gpt-5.4-mini']
    assert all(p['reasoning']=={'effort':'none'} and p['service_tier']=='default' for p in generations[:2])
    with connect(settings) as con:
        assert [r[0] for r in con.execute('SELECT cost_microusd FROM ai_calls ORDER BY rowid')] == [525,525]
    settings.input_ceiling_rate = settings.output_rate = Decimal('0')
    assert settings.ai_ready
