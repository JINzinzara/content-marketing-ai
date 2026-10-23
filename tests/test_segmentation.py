"""이전에 저장된 마스크의 읽기·출력 호환성과 형식 검증을 시험한다."""

import base64
import io
import json
from decimal import Decimal

import httpx
import pytest
from fastapi import HTTPException
from PIL import Image, ImageDraw

from app.db import connect
from app.image_ai import SIZE, png_bytes
from app.segmentation import decode_mask, encode_mask

POINTS = [[0.2, 0.1], [0.8, 0.1], [0.8, 0.9], [0.2, 0.9]]


def product_mask():
    mask = Image.new("L", SIZE, 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse((350, 300, 650, 700), fill=255)
    draw.ellipse((450, 450, 550, 550), fill=0)
    return mask


@pytest.mark.parametrize("value", [None, "not-base64!", "", "A" * (2 * 1024 * 1024 + 1)])
def test_invalid_mask_is_rejected(value):
    with pytest.raises(HTTPException) as exc:
        decode_mask(value)
    assert exc.value.status_code == 422


def test_mask_validates_format_dimensions_and_nonempty_selection():
    for image in (Image.new("L", (2, 2), 255), Image.new("RGB", SIZE, "white"), Image.new("L", SIZE, 0)):
        with pytest.raises(HTTPException):
            decode_mask(encode_mask(image))
    mask = decode_mask(encode_mask(product_mask()))
    assert mask.getpixel((512, 512)) == 0  # 컵 손잡이 등의 구멍을 채우지 않는다.
    assert mask.getpixel((400, 500)) == 255


@pytest.mark.parametrize("edge_alpha", [255, 128])
def test_legacy_mask_jobs_still_preserve_confirmed_alpha(app, new_post, settings, edge_alpha):
    identifier = new_post(preset_id="natural")
    settings.image_enabled, settings.openai_key = True, "not-a-real-key"
    settings.ai_budget_usd, settings.image_reserve_usd = Decimal("1"), Decimal("0.1")
    settings.image_text_rate, settings.image_input_rate, settings.image_output_rate = Decimal("5"), Decimal("8"), Decimal("30")
    calls = []
    def handler(request):
        calls.append(request.url.path)
        payload = json.loads(request.content)
        assert "배경을 빈 벽·단색 면으로 제한하지 말고" in payload["prompt"]
        assert "목재·린넨·창가 빛" in payload["prompt"] and "휘낭시에" in payload["prompt"]
        with Image.open(io.BytesIO(base64.b64decode(payload["mask"]["image_url"].split(",")[1]))) as mask:
            assert mask.getpixel((400, 500))[3] == edge_alpha
            assert mask.getpixel((512, 512))[3] == 0
        with Image.open(io.BytesIO(base64.b64decode(payload["images"][0]["image_url"].split(",")[1]))) as image:
            assert image.getpixel((400, 500)) == (32, 80, 120)  # 경계 픽셀을 흰색과 미리 섞지 않는다.
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(png_bytes(Image.new("RGB", SIZE, "orange"))).decode()}]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as external:
        app.state.image_ai.client = external
        approved = product_mask()
        approved.putpixel((400, 500), edge_alpha)
        job = app.state.image_ai.queue("local-owner", identifier, 1)
        with connect(settings, write=True) as con:
            parameters = json.loads(con.execute("SELECT parameters FROM image_generations WHERE id=?", (job,)).fetchone()[0])
            parameters.pop("edit_mode")  # 이전 버전에는 편집 방식 표시가 없다.
            parameters["product_mask_png"] = encode_mask(approved)
            con.execute("UPDATE image_generations SET parameters=?, mask_points=? WHERE id=?", (json.dumps(parameters), json.dumps(POINTS), job))
        app.state.image_ai.run(job)
    assert calls == ["/v1/images/edits"]
    with connect(settings) as con:
        job = con.execute("SELECT filename,parameters FROM image_generations").fetchone()
        assert decode_mask(json.loads(job["parameters"])["product_mask_png"]).getpixel((512, 512)) == 0
    with Image.open(settings.data_dir / "generated" / job["filename"]) as result:
        assert result.getpixel((400, 500)) == ((32, 80, 120) if edge_alpha == 255 else (143, 122, 60))
        assert result.getpixel((512, 512)) == (255, 165, 0)
