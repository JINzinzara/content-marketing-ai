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
        assert snapshot["sources"]["palette"] == "사용자 고정"
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
        assert snapshot["sources"]["background"] == "새로운 스타일 선택"
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
        assert body["model"] == "gpt-5.4-mini"
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


def test_logo_upload_is_removed_and_new_posts_ignore_legacy_account_logo(
    client, csrf, settings, new_post, photo
):
    assert client.post("/shop/logo", data={"csrf": csrf}, files={"photo": ("logo.png", photo, "image/png")}).status_code == 404
    with connect(settings, write=True) as con:
        con.execute("ALTER TABLE accounts ADD COLUMN logo_file TEXT NOT NULL DEFAULT ''")
        con.execute("UPDATE accounts SET logo_file='legacy-logo.jpg' WHERE id='local-owner'")
    identifier = new_post()
    with connect(settings) as con:
        brand = json.loads(owned_post(con, "local-owner", identifier)["style_snapshot"])["brand"]
        assert "logo_file" not in brand
        assert con.execute("SELECT logo_file FROM accounts").fetchone()[0] == "legacy-logo.jpg"
    assert 'action="/shop/logo"' not in client.get("/shop").text
    assert client.get("/data/references/legacy-logo.jpg").status_code == 404
    assert client.get("/posts/" + identifier + "/image").status_code == 200


# 앱에 가져온 자료 수가 기존 표시·선택 한도를 넘는 경우를 함께 검증한다.
def seed_many_references(settings, photo):
    import io
    from PIL import Image

    out = io.BytesIO()
    with Image.open(io.BytesIO(photo)) as image:
        image.convert("RGB").save(out, "JPEG")
    ids = []
    with connect(settings, write=True) as con:
        for n in range(55):
            identifier = f"history-{n}"
            ids.append("ig:" + identifier)
            con.execute(
                "INSERT INTO history(owner_id, media_id, caption, media_type, media_url) VALUES ('local-owner', ?, ?, ?, ?)",
                (identifier, f"이전 게시물 {n}", "IMAGE" if n % 2 == 0 else "VIDEO", f"https://cdn.example/{n}.jpg"),
            )
        for n in range(35):
            identifier = f"upload-{n}"
            ids.append("local:" + identifier)
            (settings.data_dir / "references" / (identifier + ".jpg")).write_bytes(out.getvalue())
            con.execute(
                "INSERT INTO style_references(id, owner_id, filename, caption, created_at) VALUES (?, 'local-owner', ?, ?, ?)",
                (identifier, identifier + ".jpg", f"참고 자료 {n}", n),
            )
    return ids


def style_response():
    return {
        "summary": "차분한 원목 분위기",
        "attributes": {key: "" for key in FIELDS},
        "evidence": ["가져온 자료의 배경과 말투"],
        "uncertainties": ["서로 다른 행사 게시물은 구분 필요"],
    }


def test_all_imported_references_are_visible_and_can_be_selected(client, csrf, settings, photo):
    from app.styles import reference_list
    ids = seed_many_references(settings, photo)
    with connect(settings) as con:
        assert {r["id"] for r in reference_list(con, "local-owner")} == set(ids)
    html = client.get("/shop").text
    assert all(f'value="{identifier}"' in html for identifier in ids)
    assert 'id="references-all"' in html and 'id="references-count"' in html
    assert "최대 3개" not in html


def test_analysis_accepts_more_than_three_selected_references(app, client, csrf, settings, photo):
    from test_generation import enable, response_body
    enable(settings)
    ids = seed_many_references(settings, photo)[:4]
    def handler(request):
        payload = json.loads(request.content)
        examples = json.loads(payload["input"][0]["content"][0]["text"])["past_post_examples"]
        assert [item["id"] for item in examples] == ids
        assert [item["image_index"] for item in examples] == [1, None, 2, None]
        if request.url.path.endswith("input_tokens"):
            return httpx.Response(200, json={"input_tokens": 100})
        return httpx.Response(200, json=response_body(style_response()))
    with httpx.Client(transport=httpx.MockTransport(handler)) as external:
        app.state.ai.client = external
        assert client.post("/shop/analyze", data={"csrf": csrf, "reference_id": ids}).status_code == 200
    with connect(settings) as con:
        assert latest_profile(con, "local-owner", "proposal")["reference_ids"] == ids


def test_large_selection_uses_one_json_field_and_sends_every_reference(app, client, csrf, settings, photo):
    from test_generation import enable, response_body
    enable(settings)
    ids = seed_many_references(settings, photo)
    calls = []
    def handler(request):
        calls.append(request.url.path)
        content = json.loads(request.content)["input"][0]["content"]
        examples = json.loads(content[0]["text"])["past_post_examples"]
        assert [item["id"] for item in examples] == ids
        # 영상의 본문은 포함하지만 이미지가 없는 자료와 사진 순서를 혼동하지 않는다.
        assert len(content) == 1 + 28 + 35
        assert [r["image_index"] for r in examples if r["image_index"] is not None] == list(range(1, 64))
        if request.url.path.endswith("input_tokens"):
            return httpx.Response(200, json={"input_tokens": 100})
        return httpx.Response(200, json=response_body(style_response()))
    with httpx.Client(transport=httpx.MockTransport(handler)) as external:
        app.state.ai.client = external
        response = client.post("/shop/analyze", data={"csrf": csrf, "reference_ids": json.dumps(ids + [ids[0]])})
    assert response.status_code == 200
    assert calls == ["/v1/responses/input_tokens", "/v1/responses"]
    with connect(settings) as con:
        assert latest_profile(con, "local-owner", "proposal")["reference_ids"] == ids


def test_large_selection_keeps_budget_guard_before_generation(app, client, csrf, settings, photo):
    from test_generation import enable
    enable(settings)
    ids = seed_many_references(settings, photo)
    calls = []
    def handler(request):
        calls.append(request.url.path)
        assert request.url.path.endswith("input_tokens")
        return httpx.Response(200, json={"input_tokens": 1_000_000})
    with httpx.Client(transport=httpx.MockTransport(handler)) as external:
        app.state.ai.client = external
        response = client.post("/shop/analyze", data={"csrf": csrf, "reference_ids": json.dumps(ids)})
    assert response.status_code == 402 and calls == ["/v1/responses/input_tokens"]
    with connect(settings) as con:
        assert latest_profile(con, "local-owner", "proposal") is None
        assert con.execute("SELECT COUNT(*) FROM ai_calls").fetchone()[0] == 0


def test_selection_json_above_old_field_size_is_not_truncated(app, client, csrf, settings):
    from test_generation import enable, response_body
    enable(settings)
    ids = ["ig:" + str(n) + "x" * 180 for n in range(100)]
    with connect(settings, write=True) as con:
        for identifier in ids:
            con.execute("INSERT INTO history(owner_id, media_id, caption) VALUES ('local-owner', ?, '')", (identifier[3:],))
    encoded = json.dumps(ids)
    assert len(encoded) > 16_384
    def handler(request):
        content = json.loads(request.content)["input"][0]["content"]
        assert [r["id"] for r in json.loads(content[0]["text"])["past_post_examples"]] == ids
        if request.url.path.endswith("input_tokens"):
            return httpx.Response(200, json={"input_tokens": 100})
        return httpx.Response(200, json=response_body(style_response()))
    with httpx.Client(transport=httpx.MockTransport(handler)) as external:
        app.state.ai.client = external
        assert client.post("/shop/analyze", data={"csrf": csrf, "reference_ids": encoded}).status_code == 200


def test_invalid_selection_and_foreign_reference_are_rejected_before_ai(app, client, csrf, settings):
    from app.db import ensure_account
    with connect(settings, write=True) as con:
        ensure_account(con, "other")
        con.execute("INSERT INTO history(owner_id, media_id, caption) VALUES ('other','private','private-caption')")
    for value in ("broken", "null", '"ig:private"', '[{}]', '[1]', "[" * 1100):
        assert client.post("/shop/analyze", data={"csrf": csrf, "reference_ids": value}).status_code == 422
    assert client.post("/shop/analyze", data={"csrf": csrf, "reference_ids": '["ig:private"]'}).status_code == 404
    assert client.post("/shop/analyze", data={"csrf": "wrong", "reference_ids": '[]'}).status_code == 403
    with connect(settings) as con:
        assert con.execute("SELECT COUNT(*) FROM ai_calls").fetchone()[0] == 0


def test_disabled_analysis_explains_missing_settings_without_exposing_values(app, client, csrf, settings):
    from test_generation import enable
    enable(settings)
    secret = settings.openai_key = "private-value-never-render"
    settings.openai_enabled = False
    settings.style_output_rate = 0
    assert settings.ai_missing == ["OPENAI_ENABLED=true", "OPENAI_STYLE_OUTPUT_USD_PER_MILLION > 0"]
    html = client.get("/shop").text
    assert 'id="style-setup-info"' in html and 'id="style-setup-message"' in html
    assert "텍스트 AI 설정 필요" in html and "OPENAI_ENABLED=true" in html
    assert secret not in html
    with connect(settings, write=True) as con:
        con.execute("UPDATE accounts SET description='가게 소개' WHERE id='local-owner'")
    response = client.post("/shop/analyze", data={"csrf": csrf})
    assert response.status_code == 503 and "OPENAI_ENABLED=true" in response.text and secret not in response.text
    settings.openai_enabled, settings.style_output_rate = True, 50
    assert settings.ai_ready
    html = client.get("/shop").text
    assert 'id="style-setup-info"' not in html and secret not in html


def test_connections_explain_both_ai_settings_without_calling_or_exposing_secrets(client, csrf, settings):
    from test_generation import enable
    from test_image_generation import enable_image
    enable(settings)
    enable_image(settings)
    settings.openai_key = "private-key-never-render"
    settings.style_input_ceiling_rate = 0
    settings.image_reserve_usd = 0
    settings.image_output_rate = 0
    assert settings.image_missing == ["IMAGE_CALL_RESERVE_USD > 0", "IMAGE_OUTPUT_USD_PER_MILLION > 0"]
    assert not settings.image_ready
    html = client.get('/connections').text
    assert 'data-ai-setup="text-setup-message"' in html
    assert 'data-ai-setup="image-setup-message"' in html
    assert "OPENAI_STYLE_INPUT_CEILING_USD_PER_MILLION" in html
    assert "IMAGE_CALL_RESERVE_USD" in html and "IMAGE_OUTPUT_USD_PER_MILLION" in html
    assert settings.openai_key not in html
    settings.style_input_ceiling_rate = 5
    settings.image_reserve_usd = 1
    settings.image_output_rate = 15
    assert settings.ai_ready and settings.image_ready
    html = client.get('/connections').text
    assert 'data-ai-setup=' not in html and html.count('호출 설정됨') == 2
    with connect(settings) as con:
        assert con.execute("SELECT COUNT(*) FROM ai_calls").fetchone()[0] == 0
