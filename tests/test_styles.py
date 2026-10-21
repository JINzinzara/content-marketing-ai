"""스타일 선택·고정 규칙·버전·참고 사진의 소유권과 승인 경계 검증"""

import json

import httpx

from app.db import connect, owned_post
from app.styles import FIELDS, latest_profile, resolve


def profile_form(csrf, version=0, **changes):
    return {
        "csrf": csrf,
        "name": "오후의 커피",
        "style": "짧고 차분한 존댓말",
        "color": "#314e3e",
        "profile_version": version,
        **{"style_" + key: "" for key in FIELDS},
        **changes,
    }


def test_presets_combine_locked_fields_and_freeze_post_version(
    client, csrf, new_post, settings
):
    data = profile_form(
        csrf,
        style_palette="브랜드 베이지",
        style_voice="짧은 존댓말",
        locked_fields=["palette"],
    )
    assert client.post("/shop", data=data).status_code == 200
    identifier = new_post(preset_id="minimal")
    with connect(settings) as con:
        profile = latest_profile(con, "local-owner")
        snapshot = json.loads(
            owned_post(con, "local-owner", identifier)["style_snapshot"]
        )
        assert snapshot["profile_version"] == profile["version"] == 1
        assert snapshot["attributes"]["palette"] == "브랜드 베이지"
        assert snapshot["sources"]["palette"] == "가게에서 고정"
        assert snapshot["attributes"]["background"] == "단순한 배경, 소품 최소화"
        assert snapshot["attributes"]["voice"] == "짧은 존댓말"
        assert snapshot["conflicts"][0]["field"] == "색감"
        assert con.execute("SELECT COUNT(*) FROM ai_calls").fetchone()[0] == 0
    assert (
        client.post(
            "/shop", data=profile_form(csrf, 1, style_palette="새로운 기준")
        ).status_code
        == 200
    )
    assert client.post("/shop", data=data).status_code == 409
    with connect(settings) as con:
        assert (
            json.loads(owned_post(con, "local-owner", identifier)["style_snapshot"])
            == snapshot
        )
        assert latest_profile(con, "local-owner")["version"] == 2
        assert (
            resolve(con, "local-owner", "keep")["attributes"]["palette"]
            == "새로운 기준"
        )


def test_new_account_can_select_preset_without_claiming_history(
    client, csrf, new_post, settings
):
    identifier = new_post(preset_id="fresh")
    with connect(settings) as con:
        snapshot = json.loads(
            owned_post(con, "local-owner", identifier)["style_snapshot"]
        )
        assert snapshot["profile_version"] == 0 and snapshot["reference_ids"] == []
        assert snapshot["sources"]["background"] == "이번 스타일 선택"
    assert (
        client.post(
            f"/posts/{identifier}/style",
            data={
                "csrf": csrf,
                "preset_id": "modern",
                "confirmed": "yes",
                "revision": 1,
            },
        ).status_code
        == 200
    )
    with connect(settings) as con:
        assert latest_profile(con, "local-owner") is None
        assert con.execute("SELECT COUNT(*) FROM ai_calls").fetchone()[0] == 0
    assert (
        client.post(
            f"/posts/{identifier}/style",
            data={"csrf": csrf, "preset_id": "bad", "confirmed": "yes", "revision": 2},
        ).status_code
        == 422
    )


def test_uploaded_references_selected_for_analysis_require_confirmation(
    app, client, csrf, settings, photo
):
    client.post("/shop", data=profile_form(csrf))
    assert (
        client.post(
            "/shop/references",
            data={"csrf": csrf, "caption": "이전 메뉴 · 차분한 분위기"},
            files={"photo": ("ref.png", photo, "image/png")},
        ).status_code
        == 200
    )
    with connect(settings) as con:
        reference_id = (
            "local:" + con.execute("SELECT id FROM style_references").fetchone()[0]
        )
    from decimal import Decimal

    settings.openai_enabled, settings.openai_key = True, "not-a-real-key"
    settings.ai_budget_usd, settings.input_ceiling_rate, settings.output_rate = (
        Decimal("1"),
        Decimal("3"),
        Decimal("15"),
    )
    proposal = {
        "summary": "원목과 따뜻한 배경",
        "attributes": {
            key: ("원목 배경" if key == "background" else "") for key in FIELDS
        },
        "evidence": [reference_id + "의 배경 관찰"],
        "uncertainties": ["정확한 글꼴은 모름"],
    }

    def handler(request):
        body = json.loads(request.content)
        assert body["model"] == "gpt-5.4"
        assert body["input"][0]["content"][1]["image_url"].startswith(
            "data:image/jpeg;base64,"
        )
        if request.url.path.endswith("input_tokens"):
            return httpx.Response(200, json={"input_tokens": 100})
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "usage": {"input_tokens": 100, "output_tokens": 100},
                "output": [
                    {"content": [{"type": "output_text", "text": json.dumps(proposal)}]}
                ],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as external:
        app.state.ai.client = external
        assert (
            client.post(
                "/shop/analyze", data={"csrf": csrf, "reference_id": reference_id}
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/shop/analyze", data={"csrf": csrf, "reference_id": "local:not-owned"}
            ).status_code
            == 404
        )
    with connect(settings) as con:
        assert latest_profile(con, "local-owner")["version"] == 1
        pending = latest_profile(con, "local-owner", "proposal")
        assert pending["reference_ids"] == [reference_id]
        assert (
            latest_profile(con, "local-owner")["content"]["attributes"]["background"]
            == ""
        )
    accepted = profile_form(
        csrf, 1, style_background="원목 배경", proposal_version=pending["version"]
    )
    assert client.post("/shop", data=accepted).status_code == 200
    with connect(settings) as con:
        assert latest_profile(con, "local-owner")["reference_ids"] == [reference_id]
    assert "아직 확정하지 않은 AI 제안" not in client.get("/shop").text


def test_reference_and_logo_are_private_and_logo_is_frozen(
    client, csrf, settings, new_post, photo
):
    assert (
        client.post(
            "/shop/logo",
            data={"csrf": csrf},
            files={"photo": ("logo.png", photo, "image/png")},
        ).status_code
        == 200
    )
    identifier = new_post()
    with connect(settings) as con:
        before = json.loads(
            owned_post(con, "local-owner", identifier)["style_snapshot"]
        )["brand"]["logo_file"]
    client.post(
        "/shop/logo",
        data={"csrf": csrf},
        files={"photo": ("new.png", photo, "image/png")},
    )
    with connect(settings) as con:
        assert (
            json.loads(owned_post(con, "local-owner", identifier)["style_snapshot"])[
                "brand"
            ]["logo_file"]
            == before
        )
        assert con.execute("SELECT logo_file FROM accounts").fetchone()[0] != before
    assert client.get("/data/references/" + before).status_code == 404
    assert client.get("/posts/" + identifier + "/image").status_code == 200
