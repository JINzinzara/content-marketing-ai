"""상품 선택의 소유권·무과금·마스크 보존·유료 요청 전 입력 검증을 시험한다."""

import base64
import io
import json
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from PIL import Image, ImageDraw

from app.db import balance, connect, ensure_account
from app.image_ai import SIZE, feather_mask, png_bytes
from app.segmentation import ProductSegmenter, correction_points, decode_mask, encode_mask, refine_edges

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


def test_local_selection_is_owned_versioned_and_has_no_ai_cost(app, client, csrf, new_post, settings):
    identifier = new_post(preset_id="natural")
    observed = []
    def extract(canvas, box, corrections):
        observed.append((canvas.size, box, corrections))
        return product_mask()
    app.state.segmenter = SimpleNamespace(ready=True, extract=extract)
    form = {"csrf": csrf, "revision": 1, "mask_points": json.dumps(POINTS), "corrections": "[[0,0,1],[1,1,0]]", "refine_edges": "no"}
    result = client.post(f"/posts/{identifier}/product-mask", data=form)
    assert result.status_code == 200
    assert observed[0][0] == SIZE
    assert observed[0][2] == [(0, 128, 1), (1023, 895, 0)]  # 가로 사진의 위아래 여백 반영.
    mask = decode_mask(result.json()["mask_png"])
    assert mask.getpixel((400, 500)) == 255 and mask.getpixel((512, 512)) == 0
    for extra in ({"corrections": "[[0,0,2]]"}, {"refine_edges": "invalid"}):
        assert client.post(f"/posts/{identifier}/product-mask", data={**form, **extra}).status_code == 422
    assert len(observed) == 1  # 검증 실패 시 모델도 호출하지 않는다.
    with Image.open(io.BytesIO(base64.b64decode(result.json()["preview_png"]))) as preview:
        assert preview.mode == "RGBA" and preview.getpixel((512, 512))[3] == 0
    assert client.post(f"/posts/{identifier}/product-mask", data={**form, "revision": 2}).status_code == 409
    assert client.post(f"/posts/{identifier}/product-mask", data={**form, "csrf": "wrong"}).status_code == 403
    with connect(settings, write=True) as con:
        ensure_account(con, "another-owner")
        con.execute("UPDATE posts SET owner_id='another-owner' WHERE id=?", (identifier,))
        assert con.execute("SELECT COUNT(*) FROM ai_calls").fetchone()[0] == 0
        assert balance(con, "local-owner") == 5
    assert client.post(f"/posts/{identifier}/product-mask", data=form).status_code == 404
    assert len(observed) == 1


def test_missing_local_models_do_not_trigger_download(tmp_path):
    model = ProductSegmenter(tmp_path)
    assert not model.ready
    with pytest.raises(HTTPException) as exc:
        model.extract(Image.new("RGB", SIZE), (10, 10, 20, 20))
    assert exc.value.status_code == 503
    assert list(tmp_path.iterdir()) == []


def test_sam_point_contract_selects_foreground_and_removes_disconnected_objects(tmp_path, monkeypatch):
    np = pytest.importorskip("numpy")
    pytest.importorskip("onnxruntime")
    model = ProductSegmenter(tmp_path)
    monkeypatch.setattr(ProductSegmenter, "ready", property(lambda self: True))
    class Encoder:
        def run(self, _, values):
            assert values["pixel_values"].shape == (1, 3, 1024, 1024)
            return [np.zeros((1, 256, 64, 64), np.float32)] * 2
    class Decoder:
        def run(self, _, values):
            assert values["input_labels"].tolist() == [[[1, 0, 0, 0, 0]]]
            assert values["input_points"][0, 0, 0].tolist() == [512, 512]
            masks = np.full((1, 1, 3, 256, 256), -10, np.float32)
            masks[0, 0, 0, :20, :20] = 10  # 높은 점수여도 상품 중앙을 포함하지 않는 후보는 제외.
            masks[0, 0, 1, 90:170, 90:170] = 10
            masks[0, 0, 1, :20, :20] = 10  # 중앙 상품과 떨어진 다른 물체도 제외.
            return [np.array([[[.99, .9, .1]]], np.float32), masks]
        def get_outputs(self):
            return [SimpleNamespace(name=name) for name in ("iou_scores", "pred_masks")]
    model._encoder, model._decoder = Encoder(), Decoder()
    mask = model.extract(Image.new("RGB", SIZE, "white"), (256, 256, 768, 768))
    assert mask.getpixel((512, 512)) == 255
    assert mask.getpixel((10, 10)) == 0
    assert mask.getbbox() is not None


@pytest.mark.parametrize("edge_alpha", [255, 128])
def test_approved_sam_mask_survives_queue_and_composite(app, client, csrf, new_post, settings, edge_alpha):
    identifier = new_post(preset_id="natural")
    settings.image_enabled, settings.openai_key = True, "not-a-real-key"
    settings.ai_budget_usd, settings.image_reserve_usd = Decimal("1"), Decimal("0.1")
    settings.image_text_rate, settings.image_input_rate, settings.image_output_rate = Decimal("5"), Decimal("8"), Decimal("30")
    form = {"csrf": csrf, "confirmed": "yes", "revision": 1, "mask_points": json.dumps(POINTS)}
    invalid = client.post(f"/posts/{identifier}/images", data={**form, "mask_png": "invalid"})
    assert invalid.status_code == 422
    with connect(settings) as con:
        assert con.execute("SELECT COUNT(*) FROM ai_calls").fetchone()[0] == 0
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
        response = client.post(f"/posts/{identifier}/images", data={**form, "mask_png": encode_mask(approved)}, headers={"Accept": "application/json"})
    assert response.status_code == 202 and calls == ["/v1/images/edits"]
    with connect(settings) as con:
        job = con.execute("SELECT filename,parameters FROM image_generations").fetchone()
        assert decode_mask(json.loads(job["parameters"])["product_mask_png"]).getpixel((512, 512)) == 0
    with Image.open(settings.data_dir / "generated" / job["filename"]) as result:
        assert result.getpixel((400, 500)) == ((32, 80, 120) if edge_alpha == 255 else (143, 122, 60))
        assert result.getpixel((512, 512)) == (255, 165, 0)


@pytest.mark.parametrize("value", ["null", "{}", "[[0,0,2]]", "[[true,0,1]]", "[[0,0,true]]", "[[NaN,0,1]]", "[[1.1,0,1]]", "[[0,0]]", json.dumps([[0, 0, 1]] * 21)])
def test_invalid_correction_points_are_rejected(value):
    with pytest.raises(HTTPException) as exc:
        correction_points(value)
    assert exc.value.status_code == 422


def test_include_exclude_prompts_retain_requested_components_and_reuse_embedding(tmp_path, monkeypatch):
    np = pytest.importorskip("numpy")
    pytest.importorskip("onnxruntime")
    model = ProductSegmenter(tmp_path)
    monkeypatch.setattr(ProductSegmenter, "ready", property(lambda self: True))
    encoder_calls = []
    class Encoder:
        def run(self, _, values):
            encoder_calls.append(True)
            return [np.zeros((1,)), np.zeros((1,))]
    class Decoder:
        def run(self, _, values):
            points = values["input_points"][0, 0].tolist()
            labels = values["input_labels"][0, 0].tolist()
            assert labels[points.index([580, 580])] == 0
            assert labels[points.index([500, 500])] == 1
            masks = np.full((1, 1, 3, 256, 256), -10, np.float32)
            masks[0, 0, :2, 100:160, 100:160] = 10
            masks[0, 0, :2, :20, :20] = 10
            masks[0, 0, 1, 140:150, 140:150] = -10
            return [np.array([[[.99, .8, .1]]]), masks]
        def get_outputs(self):
            return [SimpleNamespace(name=name) for name in ("iou_scores", "pred_masks")]
    model._encoder, model._decoder = Encoder(), Decoder()
    image = Image.new("RGB", SIZE, "white")
    args = (image, (256, 256, 768, 768))
    corrections = [(500, 500, 1), (580, 580, 0), (10, 10, 1)]
    mask = model.extract(*args, corrections)
    assert mask.getpixel((500, 500)) == 255 and mask.getpixel((580, 580)) == 0
    assert mask.getpixel((10, 10)) == 255  # 명시적으로 포함한 떨어진 장식은 유지.
    mask = model.extract(*args, corrections[:2])
    assert mask.getpixel((10, 10)) == 0
    assert len(encoder_calls) == 1
    model.extract(Image.new("RGB", SIZE, "black"), args[1], corrections[:2])
    assert len(encoder_calls) == 2


def test_edge_refinement_preserves_interior_holes_and_confirmed_alpha():
    np = pytest.importorskip("numpy")
    mask = product_mask()
    image = Image.new("RGB", SIZE, "white")
    refined = refine_edges(image, mask)
    values = np.asarray(refined)
    assert np.all(values[np.asarray(mask) == 0] == 0)
    assert refined.getpixel((400, 500)) == 255 and refined.getpixel((512, 512)) == 0
    assert np.any((values > 0) & (values < 255))
    assert feather_mask(refined).tobytes() == refined.tobytes()
    # 명암 경계와 일치한 선택은 균일 배경의 선택보다 선명하게 남는다.
    half = Image.new("L", SIZE, 0)
    ImageDraw.Draw(half).rectangle((0, 0, 511, 1023), fill=255)
    guided = refine_edges(half.convert("RGB"), half)
    flat = refine_edges(image, half)
    assert guided.getpixel((511, 512)) > flat.getpixel((511, 512))
