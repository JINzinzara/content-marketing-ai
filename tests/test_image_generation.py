"""모의 이미지 API로 자동 편집·과거 작업 호환·후보 승인·예산·복구 검증"""

import base64
import io
import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import httpx
import pytest
from fastapi import HTTPException
from PIL import Image, ImageDraw

from app.costs import spent
from app.credits import finalize
from app.db import balance, connect, owned_post
from app.image_ai import SIZE, composite, feather_mask, mask_points, png_bytes, protected_canvas
from app.main import create_app

POINTS = [[0.25, 0.2], [0.75, 0.2], [0.75, 0.8], [0.25, 0.8]]


def enable_image(settings, budget="1"):
    settings.image_enabled, settings.openai_key = True, "not-a-real-key"
    settings.ai_budget_usd = Decimal(budget)
    settings.image_reserve_usd = Decimal("0.15")
    settings.image_text_rate, settings.image_input_rate, settings.image_output_rate = (
        Decimal("5"),
        Decimal("8"),
        Decimal("30"),
    )


def image_body(usage=True):
    body = {
        "data": [
            {
                "b64_json": base64.b64encode(
                    png_bytes(Image.new("RGB", SIZE, "orange"))
                ).decode()
            }
        ]
    }
    if usage:
        body["usage"] = {
            "input_tokens": 300,
            "input_tokens_details": {"image_tokens": 100, "text_tokens": 200},
            "output_tokens": 300,
        }
    return body


def image_form(csrf, revision=1):
    return {
        "csrf": csrf,
        "confirmed": "yes",
        "revision": revision,
    }


def test_feather_blends_only_the_inner_edge_and_preserves_product_core():
    original = Image.new("RGB", SIZE, "red")
    mask = Image.new("L", SIZE, 0)
    ImageDraw.Draw(mask).rectangle((256, 256, 768, 768), fill=255)
    alpha = feather_mask(mask)
    assert alpha.getpixel((512, 512)) == 255
    assert 0 < alpha.getpixel((256, 512)) < 255
    assert alpha.getpixel((255, 512)) == 0
    with Image.open(io.BytesIO(composite(original, mask, png_bytes(Image.new("RGB", SIZE, "blue"))))) as result:
        assert result.getpixel((512, 512)) == (255, 0, 0)
        assert result.getpixel((255, 512)) == (0, 0, 255)
        edge = result.getpixel((256, 512))
        assert 0 < edge[0] < 255 and 0 < edge[2] < 255


def test_automatic_edit_sends_full_original_without_mask_or_reference_products(
    app, client, csrf, new_post, settings
):
    identifier = new_post(preset_id="minimal")
    enable_image(settings)
    with connect(settings, write=True) as con:
        snapshot = json.loads(owned_post(con, "local-owner", identifier)["style_snapshot"])
        snapshot["reference_ids"] = ["local:past-menu"]
        con.execute("INSERT INTO style_references(id, owner_id, filename, caption, created_at) VALUES ('past-menu','local-owner','not-read.jpg','휘낭시에 사진',0)")
        con.execute("UPDATE posts SET style_snapshot=? WHERE id=?", (json.dumps(snapshot), identifier))

    def handler(request):
        payload = json.loads(request.content)
        assert len(payload["images"]) == 1
        assert "상품 하나만" in payload["prompt"] and "휘낭시에" in payload["prompt"]
        assert "mask" not in payload
        with connect(settings) as con:
            post = owned_post(con, "local-owner", identifier)
        original = (settings.data_dir / "originals" / post["original_file"]).read_bytes()
        assert base64.b64decode(payload["images"][0]["image_url"].split(",")[1]) == original
        return httpx.Response(200, json=image_body())

    with httpx.Client(transport=httpx.MockTransport(handler)) as external:
        app.state.image_ai.client = external
        assert client.post(f"/posts/{identifier}/images", data=image_form(csrf)).status_code == 200
    with connect(settings) as con:
        assert con.execute("SELECT state FROM image_generations").fetchone()[0] == "completed"


@pytest.mark.parametrize("stale_fields", [{}, {"mask_points": "broken", "mask_png": "invalid", "corrections": "invalid"}])
def test_json_submission_acknowledges_one_automatic_job_without_redirect(app, client, csrf, new_post, settings, stale_fields):
    identifier = new_post(preset_id="minimal")
    enable_image(settings)
    calls = []
    def handler(request):
        calls.append(request.url.path)
        assert "mask" not in json.loads(request.content)
        return httpx.Response(200, json=image_body())
    with httpx.Client(transport=httpx.MockTransport(handler)) as external:
        app.state.image_ai.client = external
        response = client.post(f"/posts/{identifier}/images", data={**image_form(csrf), **stale_fields}, headers={"Accept": "application/json"})
    assert response.status_code == 202 and not response.history
    job = client.get(f"/posts/{identifier}/image-progress").json()["job"]
    assert response.json()["job_id"] == job["id"] and job["state"] == "completed"
    assert calls == ["/v1/images/edits"]


def test_editor_no_longer_requires_region_or_loads_sam(app, client, new_post, csrf, settings):
    identifier = new_post()
    html = client.get(f"/posts/{identifier}").text
    assert 'id="mask-editor"' not in html and 'name="mask_points"' not in html
    assert 'id="mask-tool"' not in html and not hasattr(app.state, "segmenter")
    assert client.post(f"/posts/{identifier}/product-mask", data={"csrf": csrf}).status_code == 404
    with connect(settings) as con:
        assert con.execute("SELECT COUNT(*) FROM ai_calls").fetchone()[0] == 0


def test_automatic_edit_keeps_model_output_and_needs_explicit_selection(
    app, client, csrf, new_post, settings
):
    identifier = new_post(preset_id="minimal")
    enable_image(settings)
    calls = []

    def handler(request):
        assert str(request.url) == "https://api.openai.com/v1/images/edits"
        assert request.headers["authorization"] == "Bearer not-a-real-key"
        payload = json.loads(request.content)
        assert payload["model"] == "gpt-image-2" and payload["size"] == "1024x1024"
        assert (
            payload["n"] == 1
            and payload["quality"] == "low"
            and "input_fidelity" not in payload
        )
        assert "mask" not in payload
        calls.append(payload)
        return httpx.Response(200, json=image_body())

    with httpx.Client(transport=httpx.MockTransport(handler)) as external:
        app.state.image_ai.client = external
        assert (
            client.post(
                f"/posts/{identifier}/images", data=image_form(csrf)
            ).status_code
            == 200
        )
        with connect(settings) as con:
            post = owned_post(con, "local-owner", identifier)
            job = dict(con.execute("SELECT * FROM image_generations").fetchone())
            assert job["state"] == "completed" and post["selected_image_id"] is None
            assert balance(con, "local-owner") == 5
            assert spent(con) == 10800
        assert json.loads(job["parameters"])["edit_mode"] == "automatic" and job["mask_points"] == "[]"
        with Image.open(settings.data_dir / "generated" / job["filename"]) as candidate:
            assert candidate.getpixel((512, 512)) == (255, 165, 0)
            assert candidate.getpixel((0, 0)) == (255, 165, 0)
        assert (
            client.post(
                f"/posts/{identifier}/images/select",
                data={"csrf": csrf, "revision": 1, "image_id": job["id"]},
            ).status_code
            == 422
        )
        assert (
            client.post(
                f"/posts/{identifier}/images/select",
                data={
                    "csrf": csrf,
                    "revision": 1,
                    "confirmed": "yes",
                    "image_id": job["id"],
                },
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"/posts/{identifier}/images/select",
                data={
                    "csrf": csrf,
                    "revision": 1,
                    "confirmed": "yes",
                    "image_id": job["id"],
                },
            ).status_code
            == 409
        )
    assert len(calls) == 1
    finalize(settings, "local-owner", identifier, expected_revision=2)
    finalize(settings, "local-owner", identifier, expected_revision=2)
    with connect(settings) as con:
        assert balance(con, "local-owner") == 4
    assert client.get(f"/posts/{identifier}/download").status_code == 200


@pytest.mark.parametrize(
    "points",
    [
        "[]",
        "null",
        "broken",
        [[0, 0], [1, 0], [2, 1]],
        [[0, 0], [1, 1], [0.5, 0.5]],
        [[True, 0], [1, 0], [0, 1]],
    ],
)
def test_invalid_mask_cannot_start_paid_request(app, new_post, settings, points):
    identifier = new_post()
    enable_image(settings)
    with pytest.raises(HTTPException) as exc:
        app.state.image_ai.queue("local-owner", identifier, 1, points)
    assert exc.value.status_code == 422
    with connect(settings) as con:
        assert con.execute("SELECT COUNT(*) FROM ai_calls").fetchone()[0] == 0


def test_disabled_image_api_cannot_call_network(client, csrf, new_post):
    identifier = new_post()
    assert (
        client.post(f"/posts/{identifier}/images", data=image_form(csrf)).status_code
        == 503
    )


def test_provider_failure_is_actionable_without_leaking_provider_body(app, client, csrf, new_post, settings):
    identifier = new_post()
    enable_image(settings)
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500, json={"error": "private provider detail"}))) as external:
        app.state.image_ai.client = external
        client.post(f"/posts/{identifier}/images", data=image_form(csrf), headers={"Accept": "application/json"})
    job = client.get(f"/posts/{identifier}/image-progress").json()["job"]
    assert job["state"] == "failed" and "500" in job["message"]
    assert "private provider detail" not in job["message"]


def test_simultaneous_clicks_reserve_and_queue_only_once(app, new_post, settings):
    identifier = new_post()
    enable_image(settings)

    def start():
        try:
            return app.state.image_ai.queue("local-owner", identifier, 1, POINTS)
        except HTTPException as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: start(), range(2)))
    assert sum(isinstance(result, str) for result in results) == 1 and 409 in results
    with connect(settings) as con:
        assert con.execute("SELECT COUNT(*) FROM ai_calls").fetchone()[0] == 1
        assert spent(con) == 150000


def test_text_and_image_share_one_budget(app, client, csrf, new_post, settings):
    identifier = new_post()
    enable_image(settings, "0.20")
    app.state.image_ai.queue("local-owner", identifier, 1, POINTS)
    settings.openai_enabled = True
    settings.input_ceiling_rate, settings.output_rate = Decimal("12.5"), Decimal("50")
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
    assert calls == ["/v1/responses/input_tokens"]


def test_timeout_keeps_reservation_and_restart_never_replays(
    app, client, csrf, new_post, settings
):
    identifier = new_post()
    enable_image(settings)
    calls = []

    def handler(request):
        calls.append(request.url.path)
        raise httpx.ReadTimeout("simulated", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as external:
        app.state.image_ai.client = external
        assert (
            client.post(
                f"/posts/{identifier}/images", data=image_form(csrf)
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"/posts/{identifier}/images", data=image_form(csrf)
            ).status_code
            == 409
        )
        create_app(settings, external)
    with connect(settings) as con:
        assert (
            con.execute("SELECT state FROM image_generations").fetchone()[0]
            == "unknown"
        )
        assert spent(con) == 150000 and balance(con, "local-owner") == 5
    assert len(calls) == 1


def test_queued_job_is_unknown_after_restart_and_does_not_run(app, new_post, settings):
    identifier = new_post()
    enable_image(settings)
    job = app.state.image_ai.queue("local-owner", identifier, 1, POINTS)
    with httpx.Client(
        transport=httpx.MockTransport(lambda r: pytest.fail("재시작 후 외부 요청 금지"))
    ) as external:
        restarted = create_app(settings, external)
        restarted.state.image_ai.run(job)
    with connect(settings) as con:
        assert (
            con.execute("SELECT state FROM image_generations").fetchone()[0]
            == "unknown"
        )
        assert spent(con) == 150000


def test_failed_regeneration_preserves_selected_image_and_counts_attempt(
    app, client, csrf, new_post, settings
):
    identifier = new_post()
    enable_image(settings)
    responses = [image_body(), {"data": [{"b64_json": "not-base64"}]}]
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json=responses.pop(0))
        )
    ) as external:
        app.state.image_ai.client = external
        client.post(f"/posts/{identifier}/images", data=image_form(csrf))
        with connect(settings) as con:
            image_id = con.execute("SELECT id FROM image_generations").fetchone()[0]
        client.post(
            f"/posts/{identifier}/images/select",
            data={
                "csrf": csrf,
                "revision": 1,
                "confirmed": "yes",
                "image_id": image_id,
            },
        )
        assert (
            client.post(
                f"/posts/{identifier}/images", data=image_form(csrf, 2)
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"/posts/{identifier}/images", data=image_form(csrf, 2)
            ).status_code
            == 429
        )
    with connect(settings) as con:
        post = owned_post(con, "local-owner", identifier)
        assert post["selected_image_id"] == image_id and post["revision"] == 2
        assert (
            con.execute(
                "SELECT COUNT(*) FROM image_generations WHERE state='failed'"
            ).fetchone()[0]
            == 1
        )
        assert balance(con, "local-owner") == 5


def test_style_change_invalidates_candidate_without_resetting_usage(
    app, client, csrf, new_post, settings
):
    identifier = new_post()
    enable_image(settings)
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json=image_body(False))
        )
    ) as external:
        app.state.image_ai.client = external
        client.post(f"/posts/{identifier}/images", data=image_form(csrf))
    with connect(settings) as con:
        image_id = con.execute("SELECT id FROM image_generations").fetchone()[0]
        assert spent(con) == 150000
    assert (
        client.post(
            f"/posts/{identifier}/style",
            data={
                "csrf": csrf,
                "revision": 1,
                "preset_id": "modern",
                "confirmed": "yes",
            },
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/posts/{identifier}/images/select",
            data={
                "csrf": csrf,
                "revision": 2,
                "image_id": image_id,
                "confirmed": "yes",
            },
        ).status_code
        == 409
    )
    with connect(settings) as con:
        assert owned_post(con, "local-owner", identifier)["selected_image_id"] is None
        assert con.execute("SELECT COUNT(*) FROM image_generations").fetchone()[0] == 1


def test_actual_cost_over_reserve_stops_future_calls(
    app, client, csrf, new_post, settings
):
    first, second = new_post(), new_post()
    enable_image(settings)
    body = image_body()
    body["usage"]["output_tokens"] = 10000
    with httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))
    ) as external:
        app.state.image_ai.client = external
        assert (
            client.post(f"/posts/{first}/images", data=image_form(csrf)).status_code
            == 200
        )
        assert (
            client.post(f"/posts/{second}/images", data=image_form(csrf)).status_code
            == 402
        )


def test_other_post_and_other_account_cannot_use_private_images(
    app, client, csrf, new_post, settings, photo
):
    from dataclasses import replace
    from fastapi.testclient import TestClient
    from itsdangerous import TimestampSigner
    from app.db import ensure_account

    first, second = new_post(), new_post()
    enable_image(settings)
    with httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=image_body()))
    ) as external:
        app.state.image_ai.client = external
        client.post(f"/posts/{first}/images", data=image_form(csrf))
    client.post(
        "/shop/references",
        data={"csrf": csrf, "caption": "비공개"},
        files={"photo": ("ref.png", photo, "image/png")},
    )
    with connect(settings, write=True) as con:
        ensure_account(con, "another")
        image_id = con.execute("SELECT id FROM image_generations").fetchone()[0]
        ref_id = con.execute("SELECT id FROM style_references").fetchone()[0]
    assert (
        client.post(
            f"/posts/{second}/images/select",
            data={
                "csrf": csrf,
                "revision": 1,
                "image_id": image_id,
                "confirmed": "yes",
            },
        ).status_code
        == 404
    )
    production = replace(
        settings,
        environment="production",
        public_url="https://cafe.example",
        instagram_app_id="test-id",
        instagram_app_secret="test-secret",
    )
    with httpx.Client(
        transport=httpx.MockTransport(lambda r: pytest.fail("네트워크 호출 금지"))
    ) as external:
        other_app = create_app(production, external)
        with TestClient(other_app, base_url="https://cafe.example") as other:
            raw = base64.b64encode(
                json.dumps({"owner_id": "another", "csrf": "another-csrf"}).encode()
            )
            other.cookies.set(
                "session", TimestampSigner(settings.session_secret).sign(raw).decode()
            )
            for url in (
                f"/posts/{first}/original",
                f"/posts/{first}/images/{image_id}",
                f"/shop/references/{ref_id}",
            ):
                assert other.get(url).status_code == 404
            assert (
                other.post(
                    f"/posts/{first}/images", data=image_form("another-csrf")
                ).status_code
                == 404
            )
            assert (
                other.post(
                    f"/posts/{first}/images/select",
                    data={
                        "csrf": "another-csrf",
                        "revision": 1,
                        "confirmed": "yes",
                        "image_id": image_id,
                    },
                ).status_code
                == 404
            )
