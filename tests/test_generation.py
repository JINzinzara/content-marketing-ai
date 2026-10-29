"""구조화 응답·상품 사실·텍스트 원가·호출 한도와 실패 검증."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import httpx
import pytest

from app.db import connect, owned_post
from app.config import Settings
from fastapi import HTTPException


def enable(settings, budget="1"):
    settings.openai_enabled = True
    settings.openai_key = "not-a-real-key"
    settings.openai_model = "test-model"
    settings.draft_model = "test-model"
    settings.ai_budget_usd = Decimal(budget)
    settings.input_ceiling_rate = Decimal("12.5")
    settings.output_rate = Decimal("50")
    settings.draft_input_ceiling_rate = settings.input_ceiling_rate
    settings.draft_output_rate = settings.output_rate
    settings.style_input_ceiling_rate = settings.input_ceiling_rate
    settings.style_output_rate = settings.output_rate


def response_body(text=None, status="completed"):
    text = text or {
        "title": "밤 크림 라테",
        "caption": "밤 크림 라테를 소개해요.",
        "hashtags": "#라테",
        "layout": "frame",
    }
    return {
        "id": "response-test",
        "status": status,
        "usage": {"input_tokens": 100, "output_tokens": 100},
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps(text, ensure_ascii=False),
                    }
                ],
            }
        ],
    }


def test_without_enabled_api_no_external_call(app, client, csrf, new_post):
    identifier = new_post()
    assert (
        client.post(f"/posts/{identifier}/generate", data={"csrf": csrf}).status_code
        == 503
    )


def test_draft_failure_keeps_post_and_shows_warning_once(app, client, new_post, settings, monkeypatch):
    enable(settings)
    def failed(*args):
        raise HTTPException(503, "test failure")
    monkeypatch.setattr(app.state.ai, "draft", failed)
    identifier = new_post()
    html = client.get(f"/posts/{identifier}").text
    assert 'id="page-warning"' in html and "초안은 저장했지만" in html
    assert 'id="page-warning"' not in client.get(f"/posts/{identifier}").text
    with connect(settings) as con:
        assert owned_post(con, "local-owner", identifier)["source"] != "ai"


def test_structured_output_token_preflight_and_usage_record(
    app, client, csrf, new_post, settings
):
    identifier = new_post()
    enable(settings)
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        calls.append(request.url.path)
        assert request.headers["authorization"] == "Bearer not-a-real-key"
        assert payload["model"] == "test-model"
        assert payload["text"]["format"]["type"] == "json_schema"
        assert payload["input"][0]["content"][1]["image_url"].startswith(
            "data:image/jpeg;base64,"
        )
        if request.url.path.endswith("input_tokens"):
            assert "max_output_tokens" not in payload
            return httpx.Response(200, json={"input_tokens": 100})
        assert payload["max_output_tokens"] == settings.max_output_tokens
        assert payload["store"] is False and "tools" not in payload
        return httpx.Response(200, json=response_body())

    with httpx.Client(transport=httpx.MockTransport(handler)) as external:
        app.state.ai.client = external
        assert (
            client.post(
                f"/posts/{identifier}/generate", data={"csrf": csrf}
            ).status_code
            == 200
        )
    assert calls == ["/v1/responses/input_tokens", "/v1/responses"]
    with connect(settings) as con:
        post = owned_post(con, "local-owner", identifier)
        assert post["source"] == "ai" and post["revision"] == 2
        usage = con.execute("SELECT * FROM ai_calls").fetchone()
        assert usage["state"] == "completed" and usage["output_tokens"] == 100
        assert usage["cost_microusd"] == 6250


@pytest.mark.parametrize("budget,status", [("1", 200), ("0.001", 402)])
def test_default_mini_draft_uses_its_own_model_rates_and_budget(
    app, client, csrf, new_post, settings, budget, status
):
    identifier = new_post()
    enable(settings, budget)
    defaults = Settings()
    settings.draft_model = defaults.draft_model
    settings.draft_input_ceiling_rate = defaults.draft_input_ceiling_rate
    settings.draft_output_rate = defaults.draft_output_rate
    original = (settings.openai_model, settings.input_ceiling_rate, settings.output_rate)
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        calls.append(request.url.path)
        assert payload["model"] == "gpt-5.4-mini"
        assert payload["reasoning"] == {"effort": "none"}
        if request.url.path.endswith("input_tokens"):
            assert "service_tier" not in payload
            return httpx.Response(200, json={"input_tokens": 100})
        assert payload["service_tier"] == "default" and payload["store"] is False
        return httpx.Response(200, json=response_body())

    with httpx.Client(transport=httpx.MockTransport(handler)) as external:
        app.state.ai.client = external
        assert client.post(f"/posts/{identifier}/generate", data={"csrf": csrf}).status_code == status
    assert (settings.openai_model, settings.input_ceiling_rate, settings.output_rate) == original
    with connect(settings) as con:
        rows = con.execute("SELECT * FROM ai_calls").fetchall()
        if status == 200:
            assert len(rows) == 1 and rows[0]["model"] == "gpt-5.4-mini"
            assert rows[0]["reserved_microusd"] == 9291
            assert rows[0]["cost_microusd"] == 525
        else:
            assert not rows and calls == ["/v1/responses/input_tokens"]


def test_draft_env_defaults_and_overrides_do_not_change_analysis_model(monkeypatch):
    monkeypatch.setattr("app.config.load_dotenv", lambda *a, **kw: None)
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.4")
    for name in ("OPENAI_DRAFT_MODEL", "OPENAI_DRAFT_INPUT_CEILING_USD_PER_MILLION",
                 "OPENAI_DRAFT_OUTPUT_USD_PER_MILLION"):
        monkeypatch.delenv(name, raising=False)
    cfg = Settings.from_env()
    assert (cfg.draft_model, cfg.draft_input_ceiling_rate, cfg.draft_output_rate) == (
        "gpt-5.4-mini", Decimal("0.75"), Decimal("4.50"))
    monkeypatch.setenv("OPENAI_DRAFT_MODEL", "custom-draft")
    monkeypatch.setenv("OPENAI_DRAFT_INPUT_CEILING_USD_PER_MILLION", "1.25")
    monkeypatch.setenv("OPENAI_DRAFT_OUTPUT_USD_PER_MILLION", "8")
    cfg = Settings.from_env()
    assert (cfg.draft_model, cfg.draft_input_ceiling_rate, cfg.draft_output_rate) == (
        "custom-draft", Decimal("1.25"), Decimal("8"))
    assert cfg.openai_model == "gpt-5.4"
    cfg.draft_output_rate = Decimal("0")
    assert not cfg.ai_ready and "OPENAI_DRAFT_OUTPUT_USD_PER_MILLION > 0" in cfg.ai_missing


def test_budget_stops_model_request_before_spend(app, client, csrf, new_post, settings):
    identifier = new_post()
    enable(settings, "0.001")
    calls = []

    def handler(request):
        calls.append(request.url.path)
        assert request.url.path.endswith("input_tokens")
        return httpx.Response(200, json={"input_tokens": 100})

    with httpx.Client(transport=httpx.MockTransport(handler)) as external:
        app.state.ai.client = external
        assert (
            client.post(
                f"/posts/{identifier}/generate", data={"csrf": csrf}
            ).status_code
            == 402
        )
    assert len(calls) == 1
    with connect(settings) as con:
        assert con.execute("SELECT COUNT(*) FROM ai_calls").fetchone()[0] == 0


@pytest.mark.parametrize(
    "bad_result,code",
    [
        (
            response_body(
                {
                    "title": "밤 라테",
                    "caption": "3,000원 할인해요",
                    "hashtags": "",
                    "layout": "frame",
                }
            ),
            422,
        ),
        (
            response_body(
                {
                    "title": "무설탕 밤 라테",
                    "caption": "소개합니다",
                    "hashtags": "",
                    "layout": "frame",
                }
            ),
            422,
        ),
        (
            response_body(
                {
                    "title": "밤 라테",
                    "caption": "소개합니다",
                    "hashtags": "",
                    "layout": "unapproved",
                }
            ),
            502,
        ),
        (response_body(status="incomplete"), 502),
    ],
)
def test_bad_or_incomplete_output_preserves_existing_content(
    app, client, csrf, new_post, settings, bad_result, code
):
    identifier = new_post()
    enable(settings)

    def handler(request):
        return httpx.Response(
            200,
            json=(
                {"input_tokens": 100}
                if request.url.path.endswith("input_tokens")
                else bad_result
            ),
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as external:
        app.state.ai.client = external
        assert (
            client.post(
                f"/posts/{identifier}/generate", data={"csrf": csrf}
            ).status_code
            == code
        )
    with connect(settings) as con:
        post = owned_post(con, "local-owner", identifier)
        assert post["revision"] == 1 and post["source"] == "manual"
        assert con.execute("SELECT cost_microusd FROM ai_calls").fetchone()[0] > 0


def test_timeout_keeps_reserved_cost_and_does_not_retry(
    app, client, csrf, new_post, settings
):
    identifier = new_post()
    enable(settings, "0.15")
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("input_tokens"):
            return httpx.Response(200, json={"input_tokens": 100})
        raise httpx.ReadTimeout("simulated", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as external:
        app.state.ai.client = external
        assert (
            client.post(
                f"/posts/{identifier}/generate", data={"csrf": csrf}
            ).status_code
            == 502
        )
        assert (
            client.post(
                f"/posts/{identifier}/generate", data={"csrf": csrf}
            ).status_code
            == 409
        )
    assert calls.count("/v1/responses") == 1
    with connect(settings) as con:
        usage = con.execute("SELECT * FROM ai_calls").fetchone()
        assert usage["state"] == "pending" and usage["cost_microusd"] is None
        assert usage["reserved_microusd"] == 103650


def test_per_post_generation_limit_counts_failed_and_successful_attempts(
    app, client, csrf, new_post, settings
):
    identifier = new_post()
    enable(settings)
    settings.ai_attempts_per_post = 1

    def handler(request):
        return httpx.Response(
            200,
            json=(
                {"input_tokens": 100}
                if request.url.path.endswith("input_tokens")
                else response_body()
            ),
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as external:
        app.state.ai.client = external
        assert (
            client.post(
                f"/posts/{identifier}/generate", data={"csrf": csrf}
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"/posts/{identifier}/generate", data={"csrf": csrf}
            ).status_code
            == 429
        )


def test_style_proposal_requires_owner_confirmation(app, client, csrf, settings):
    client.post(
        "/shop",
        data={
            "csrf": csrf,
            "name": "우리 카페",
            "description": "동네의 차분한 카페",
            "style": "기존에 확정한 스타일",
            "color": "#314e3e",
        },
    )
    enable(settings)
    from app.styles import FIELDS

    proposal = {
        "summary": "짧고 차분한 존댓말",
        "attributes": {key: "" for key in FIELDS},
        "evidence": ["직접 제공한 가게 소개"],
        "uncertainties": ["게시물 사진은 아직 없음"],
    }

    def handler(request):
        return httpx.Response(
            200,
            json=(
                {"input_tokens": 100}
                if request.url.path.endswith("input_tokens")
                else response_body(proposal)
            ),
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as external:
        app.state.ai.client = external
        assert client.post("/shop/analyze", data={"csrf": csrf}).status_code == 200
    with connect(settings) as con:
        row = con.execute("SELECT style, style_proposal FROM accounts").fetchone()
        assert row["style"] == "기존에 확정한 스타일"
        assert "짧고 차분한 존댓말" in row["style_proposal"]


def test_concurrent_calls_share_global_budget_reservation(app, new_post, settings):
    identifiers = [new_post(), new_post()]
    enable(settings, "0.15")
    started, release = threading.Event(), threading.Event()

    def handler(request):
        if request.url.path.endswith("input_tokens"):
            return httpx.Response(200, json={"input_tokens": 100})
        started.set()
        assert release.wait(5)
        return httpx.Response(200, json=response_body())

    with connect(settings) as con:
        posts = [
            owned_post(con, "local-owner", identifier) for identifier in identifiers
        ]
    from app.db import account

    shop = account(settings, "local-owner")
    with httpx.Client(transport=httpx.MockTransport(handler)) as external:
        app.state.ai.client = external
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(
                app.state.ai.draft, "local-owner", identifiers[0], shop, posts[0]
            )
            assert started.wait(5)
            try:
                with pytest.raises(HTTPException) as raised:
                    app.state.ai.draft("local-owner", identifiers[1], shop, posts[1])
                assert raised.value.status_code == 402
            finally:
                release.set()
            assert first.result().title == "밤 크림 라테"
