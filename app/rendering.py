"""업로드된 이미지를 검증하고 승인한 이미지·원본 상품·한글·브랜드를 게시 규격에 맞게 합성"""

import io
import os
import tempfile
import warnings
from pathlib import Path

from fastapi import HTTPException
from PIL import Image, ImageCms, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

from .db import connect
from .styles import post_style

FONT = Path(__file__).parent / "assets/fonts/NotoSansKR.ttf"
UPLOAD_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 20_000_000
ASPECTS = {"portrait": (1080, 1350), "square": (1080, 1080)}
LAYOUTS = {"frame", "split"}


def normalize_photo(raw):
    """업로드 사진 bytes -> 검증·회전 보정·정규화한 JPEG bytes환"""
    if not raw or len(raw) > UPLOAD_BYTES:
        raise HTTPException(422, "이미지는 8MB 이하의 JPEG·PNG·WebP 파일로 올려주세요")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as src:
                if (
                    src.format not in {"JPEG", "PNG", "WEBP"}
                    or src.width * src.height > MAX_PIXELS
                ):
                    raise ValueError("unsupported image")
                src.load()
                fixed = ImageOps.exif_transpose(src)
                if src.info.get("icc_profile") and fixed.mode in {"RGB", "CMYK"}:
                    fixed = ImageCms.profileToProfile(
                        fixed,
                        ImageCms.ImageCmsProfile(io.BytesIO(src.info["icc_profile"])),
                        ImageCms.createProfile("sRGB"),
                        outputMode="RGB",
                    )
                if fixed.mode in {"RGBA", "LA"} or "transparency" in fixed.info:
                    base = Image.new("RGBA", fixed.size, "white")
                    base.alpha_composite(fixed.convert("RGBA"))
                    fixed = base.convert("RGB")
                else:
                    fixed = fixed.convert("RGB")
                # 원본 비율을 유지하며 웹 제작에 필요한 크기로만 줄인다.
                fixed.thumbnail((2400, 2400), Image.Resampling.LANCZOS)
                out = io.BytesIO()
                fixed.save(out, "JPEG", quality=95)
                return out.getvalue()
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        ImageCms.PyCMSError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        raise HTTPException(
            422, "잘못된 형태의 이미지입니다. JPEG·PNG·WebP의 형태인지 확인해주세요"
        ) from None


def atomic_write(path, content):
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix=".writing-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def font(size):
    if not FONT.exists():
        raise HTTPException(
            503, "한글 글꼴 파일을 설치해주세요 (README의 글꼴 안내 참고)"
        )
    return ImageFont.truetype(str(FONT), size)


def wrapped(draw, text, face, width):
    lines = []
    for paragraph in text.splitlines() or [""]:
        line = ""
        for ch in paragraph:
            if line and draw.textlength(line + ch, font=face) > width:
                lines.append(line.rstrip())
                line = "" if ch.isspace() else ch
            else:
                line += ch
        lines.append(line.rstrip())
    return lines


def draw_text(draw, text, box, *, size, fill, minimum=24):
    x, y, width, height = box
    for point in range(size, minimum - 1, -2):
        face = font(point)
        lines = wrapped(draw, text, face, width)
        line_height = point * 1.4
        if len(lines) * line_height <= height:
            for index, line in enumerate(lines):
                draw.text(
                    (x, y + index * line_height),
                    line,
                    font=face,
                    fill=fill,
                    anchor="lt",
                )
            return
    raise HTTPException(422, "제목이나 가격 표기의 글자 수를 줄여주세요")


def render(settings, post, shop_name, *, preview=False):
    """원본 또는 사용자가 승인한 후보로 이미지를 합성해 JPEG bytes로 반환"""
    width, height = ASPECTS[post["aspect"]]
    image = Image.new("RGB", (width, height), "#f5f1e8")
    draw = ImageDraw.Draw(image)
    accent = post["color"]
    photo = settings.data_dir / "originals" / post["original_file"]
    if post.get("selected_image_id"):
        with connect(settings) as con:
            candidate = con.execute(
                "SELECT * FROM image_generations WHERE id=? AND post_id=? AND owner_id=? AND state='completed'",
                (post["selected_image_id"], post["id"], post["owner_id"]),
            ).fetchone()
        if not candidate or candidate["style_snapshot"] != post["style_snapshot"]:
            raise HTTPException(409, "현재 스타일에 맞는 이미지를 다시 선택해주세요")
        photo = settings.data_dir / "generated" / candidate["filename"]
    brand = post_style(post).get("brand", {})
    logo = brand.get("logo_file", "")
    with Image.open(photo) as src:
        if post["layout"] == "frame":
            photo_box = (48, 210, width - 96, height - 450)
            title_box = (56, 90, width - 112, 115)
            price_box = (56, height - 145, width - 112, 85)
        else:
            photo_box = (0, 0, width, int(height * 0.67))
            title_box = (56, int(height * 0.70), width - 112, int(height * 0.17))
            price_box = (56, int(height * 0.89), width - 112, int(height * 0.08))
        x, y, w, h = photo_box
        scaled = ImageOps.contain(src, (w, h), Image.Resampling.LANCZOS)
        draw.rectangle((x, y, x + w, y + h), fill="white")
        image.paste(scaled, (x + (w - scaled.width) // 2, y + (h - scaled.height) // 2))
    if post["layout"] == "frame":
        draw_text(
            draw,
            brand.get("name") or shop_name or "우리 가게",
            (56, 32, width - (240 if logo else 112), 45),
            size=28,
            fill=accent,
        )
    if logo:
        with Image.open(settings.data_dir / "references" / logo) as mark:
            mark = ImageOps.contain(mark.convert("RGB"), (104, 60))
            image.paste(mark, (width - 136, 24))
    draw_text(draw, post["title"], title_box, size=58, fill=accent)
    if post.get("show_price") and post["price"]:
        draw_text(draw, post["price"], price_box, size=42, fill=accent)
    if preview:
        draw.rectangle((width - 224, height - 48, width, height), fill="#ffffff")
        draw.text(
            (width - 206, height - 38),
            "미리보기",
            font=font(24),
            fill="#777777",
            anchor="lt",
        )
    buf = io.BytesIO()
    image.save(buf, "JPEG", quality=92, optimize=True)
    return buf.getvalue()
