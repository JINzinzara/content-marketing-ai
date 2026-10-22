"""사진 검증·회전·한글 합성·규격·상품 색과 저장 실패 검증"""

import io

import pytest
from fastapi import HTTPException
from PIL import Image, ImageChops, ImageDraw

from app.db import connect, owned_post
from app.rendering import draw_text, normalize_photo, render, text_runs


def test_emoji_is_rendered_as_a_glyph_instead_of_missing_font_box():
    images = []
    for title in ("☕", "\u0378"):
        image = Image.new("RGB", (200, 120), "white")
        draw_text(ImageDraw.Draw(image), title, (10, 10, 180, 100), size=58, fill="black")
        images.append(image)
    assert ImageChops.difference(*images).getbbox() is not None
    assert text_runs("밤 라테 ☕🌰") == [("밤 라테 ", False), ("☕🌰", True)]
    assert text_runs("1️⃣") == [("1️⃣", True)]


def test_feed_preview_keeps_caption_emoji_and_matches_export(client, csrf, new_post, settings):
    from app.credits import finalize

    identifier = new_post()
    fields = {"csrf": csrf, "revision": 1, "title": "몽블랑 라테 ☕🌰",
              "caption": "오늘의 라테 ☕\n<script>안전한 본문</script>", "hashtags": "#밤라테🌰",
              "layout": "frame", "aspect": "portrait"}
    assert client.post(f"/posts/{identifier}/edit", data=fields).status_code == 200
    html = client.get(f"/posts/{identifier}").text
    assert 'aria-label="Instagram 피드 미리보기"' in html and 'id="feed-caption"' in html
    assert "오늘의 라테 ☕" in html and "#밤라테🌰" in html
    assert "&lt;script&gt;안전한 본문&lt;/script&gt;" in html
    preview = client.get(f"/posts/{identifier}/image")
    assert preview.status_code == 200
    finalize(settings, "local-owner", identifier, expected_revision=2)
    assert client.get(f"/posts/{identifier}/download").content == preview.content


def test_invalid_upload_rejected(client, csrf):
    data = {"csrf": csrf, "product_name": "상품", "purpose": "신메뉴 안내"}
    response = client.post(
        "/posts",
        data=data,
        files={"photo": ("a.jpg", b"<script>bad</script>", "image/jpeg")},
    )
    assert response.status_code == 422
    with pytest.raises(HTTPException):
        normalize_photo(b"x" * (8 * 1024 * 1024 + 1))


@pytest.mark.parametrize(
    "aspect,size", [("portrait", (1080, 1350)), ("square", (1080, 1080))]
)
@pytest.mark.parametrize("layout", ["frame", "split"])
def test_export_dimensions_long_korean_and_original_color(
    new_post, settings, aspect, size, layout
):
    identifier = new_post(
        product_name="가을의 따뜻한 밤 크림 라테와 함께하는 조금 더 긴 우리 가게의 신메뉴 소개",
        layout=layout,
        aspect=aspect,
    )
    with connect(settings) as con:
        post = owned_post(con, "local-owner", identifier)
    raw = render(settings, post, "조금 더 긴 우리 가게 이름")
    with Image.open(io.BytesIO(raw)) as result:
        assert result.format == "JPEG" and result.mode == "RGB"
        assert result.size == size
        pixel = result.getpixel((540, 500 if aspect == "portrait" else 400))
        assert all(abs(a - b) < 5 for a, b in zip(pixel, (32, 80, 120)))


def test_exif_orientation_and_metadata_removed():
    image = Image.new("RGB", (50, 100), "red")
    exif = image.getexif()
    exif[274] = 6
    out = io.BytesIO()
    image.save(out, "JPEG", exif=exif)
    with Image.open(io.BytesIO(normalize_photo(out.getvalue()))) as normalized:
        assert normalized.size == (100, 50)
        assert not normalized.getexif()


def test_manual_edit_free_and_old_form_cannot_overwrite(
    client, csrf, new_post, settings
):
    from app.credits import finalize
    from app.db import balance

    identifier = new_post()
    finalize(settings, "local-owner", identifier)
    fields = {
        "csrf": csrf,
        "title": "더 짧은 제목",
        "caption": "사장님이 수정한 문구",
        "hashtags": "#카페",
        "price": "6,500원",
        "layout": "split",
        "aspect": "square",
        "promotion": "organic",
        "revision": 1,
    }
    assert client.post(f"/posts/{identifier}/edit", data=fields).status_code == 200
    assert client.post(f"/posts/{identifier}/edit", data=fields).status_code == 409
    assert client.get(f"/posts/{identifier}/download").status_code == 409
    finalize(settings, "local-owner", identifier)
    with connect(settings) as con:
        assert balance(con, "local-owner") == 4
    response = client.get(f"/posts/{identifier}/download")
    with Image.open(io.BytesIO(response.content)) as image:
        assert image.size == (1080, 1080)
