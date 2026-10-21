"""기본 가격 비표시·명시적 표시·AI 입력/출력과 기존 본문 보존 검증"""

import json

import httpx
import pytest

from app.db import connect, owned_post
from app.rendering import render
from test_generation import enable, response_body


def test_stored_price_is_not_added_to_default_caption_or_image(new_post, settings):
    identifier = new_post()
    with connect(settings) as con:
        post = owned_post(con, "local-owner", identifier)
    assert post["price"] == "6,500원" and post["show_price"] == 0
    assert "6,500" not in post["caption"]
    assert render(settings, post, "가게") == render(
        settings, {**post, "price": ""}, "가게"
    )
    assert render(settings, {**post, "show_price": 1}, "가게") != render(
        settings, post, "가게"
    )


def test_explicit_price_setting_is_saved_and_edit_does_not_erase_authored_caption(
    client, csrf, new_post, settings
):
    identifier = new_post(show_price="true")
    with connect(settings) as con:
        post = owned_post(con, "local-owner", identifier)
    assert post["show_price"] == 1 and "6,500원" in post["caption"]
    fields = {
        k: post[k]
        for k in (
            "title",
            "caption",
            "hashtags",
            "price",
            "layout",
            "aspect",
            "revision",
        )
    }
    assert (
        client.post(
            f"/posts/{identifier}/edit", data={**fields, "csrf": csrf}
        ).status_code
        == 200
    )
    with connect(settings) as con:
        edited = owned_post(con, "local-owner", identifier)
    assert edited["show_price"] == 0 and edited["price"] == post["price"]
    assert edited["caption"] == post["caption"]


@pytest.mark.parametrize("show_price", [False, True])
def test_ai_receives_price_only_when_enabled(
    app, client, csrf, new_post, settings, show_price
):
    identifier = new_post(show_price=str(show_price).lower())
    enable(settings)

    def handler(request):
        payload = json.loads(request.content)
        data = json.loads(payload["input"][0]["content"][0]["text"])
        assert data["show_price"] is show_price
        assert ("price" in data["confirmed_product"]) is show_price
        if request.url.path.endswith("input_tokens"):
            return httpx.Response(200, json={"input_tokens": 100})
        return httpx.Response(200, json=response_body())

    with httpx.Client(transport=httpx.MockTransport(handler)) as external:
        app.state.ai.client = external
        assert (
            client.post(
                f"/posts/{identifier}/generate", data={"csrf": csrf}
            ).status_code
            == 200
        )


def test_hidden_price_in_ai_output_is_rejected_even_if_number_is_in_product_facts(
    app, client, csrf, new_post, settings
):
    identifier = new_post(facts="가격 6,500원", show_price="false")
    enable(settings)

    def handler(request):
        if request.url.path.endswith("input_tokens"):
            return httpx.Response(200, json={"input_tokens": 100})
        return httpx.Response(
            200,
            json=response_body(
                {
                    "title": "밤 크림 라테",
                    "caption": "6,500원입니다.",
                    "hashtags": "",
                    "layout": "frame",
                }
            ),
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as external:
        app.state.ai.client = external
        assert (
            client.post(
                f"/posts/{identifier}/generate", data={"csrf": csrf}
            ).status_code
            == 422
        )
    with connect(settings) as con:
        assert owned_post(con, "local-owner", identifier)["source"] == "manual"
