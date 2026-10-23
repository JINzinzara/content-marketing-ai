"""사진을 검증하고 원본 또는 승인한 후보를 게시 규격에 가득 채워 출력"""

import io
import os
import re
import tempfile
import warnings
from pathlib import Path

from fastapi import HTTPException
from PIL import Image, ImageCms, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

from .db import connect

FONT = Path(__file__).parent / "assets/fonts/NotoSansKR.ttf"
EMOJI_FONT = FONT.with_name("NotoEmoji.ttf")
EMOJI_CHARACTER = re.compile(
    r"[\U0001f000-\U0001faff\u2600-\u27bf\u200d\ufe0e\ufe0f\u20e3]"
    r"|[0-9#*](?=\ufe0f?\u20e3)"
)
UPLOAD_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 20_000_000
ASPECTS = {"portrait": (1080, 1350), "square": (1080, 1080)}
LAYOUTS = {"frame", "split"}


def normalize_photo(raw):
    """업로드 사진 bytes -> 검증·회전 보정·정규화한 JPEG bytes 반환"""
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


def font(size, *, emoji=False):
    path = EMOJI_FONT if emoji else FONT
    if not path.exists():
        raise HTTPException(
            503, "한글·이모지 글꼴 파일을 설치해주세요 (README의 글꼴 안내 참고)"
        )
    return ImageFont.truetype(str(path), size)


def text_runs(text):
    """연속한 문자·이모지를 같은 글꼴로 측정·그리기 위한 구간으로 나눈다."""
    runs = []
    for index, character in enumerate(text):
        emoji = bool(EMOJI_CHARACTER.match(text, index))
        if runs and runs[-1][1] == emoji:
            runs[-1] = (runs[-1][0] + character, emoji)
        else:
            runs.append((character, emoji))
    return runs


def text_width(draw, text, faces):
    return sum(draw.textlength(run, font=faces[emoji]) for run, emoji in text_runs(text))


def wrapped(draw, text, faces, width):
    lines = []
    for paragraph in text.splitlines() or [""]:
        line = ""
        for ch in paragraph:
            if line and text_width(draw, line + ch, faces) > width:
                lines.append(line.rstrip())
                line = "" if ch.isspace() else ch
            else:
                line += ch
        lines.append(line.rstrip())
    return lines


def draw_text(draw, text, box, *, size, fill, minimum=24):
    x, y, width, height = box
    for point in range(size, minimum - 1, -2):
        faces = (font(point), font(point, emoji=True))
        lines = wrapped(draw, text, faces, width)
        line_height = point * 1.4
        if len(lines) * line_height <= height:
            for index, line in enumerate(lines):
                runs = text_runs(line)
                top = min((faces[emoji].getbbox(run, anchor="ls")[1] for run, emoji in runs), default=0)
                cursor = x
                for run, emoji in runs:
                    draw.text((cursor, y + index * line_height - top), run,
                              font=faces[emoji], fill=fill, anchor="ls")
                    cursor += draw.textlength(run, font=faces[emoji])
            return
    raise HTTPException(422, "제목이나 가격 표기의 글자 수를 줄여주세요")


def render(settings, post, shop_name, *, preview=False):
    """원본 또는 승인한 후보를 비율에 맞춰 채우고 JPEG bytes로 반환"""
    width, height = ASPECTS[post["aspect"]]
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
    with Image.open(photo) as src:
        image = ImageOps.fit(src.convert("RGB"), (width, height), Image.Resampling.LANCZOS)
    if post.get("show_price") and post["price"]:
        draw_text(ImageDraw.Draw(image), post["price"],
                  (56, height - 145, width - 112, 85), size=42, fill=post["color"])
    buf = io.BytesIO()
    image.save(buf, "JPEG", quality=92, optimize=True)
    return buf.getvalue()
