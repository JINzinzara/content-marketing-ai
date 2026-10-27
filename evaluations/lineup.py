"""공지 구성 후보의 CLI 평가용 함수"""

import io
from datetime import date

from fastapi import HTTPException
from PIL import Image, ImageDraw

from app.rendering import draw_text

VERSION = "lineup-1.0.0"


def compose(menus, selected, sold_out, day, shop):
    date.fromisoformat(day)
    if len(selected) != len(set(selected)) or not set(selected) <= set(menus):
        raise ValueError("등록된 메뉴를 중복 없이 선택해주세요")
    if not selected or not set(sold_out) <= set(selected):
        raise ValueError("판매 메뉴와 품절 메뉴를 확인해주세요")
    lines = [shop, day + " 오늘의 라인업", ""]
    lines += [menus[key] + (" · 품절" if key in sold_out else "") for key in selected]
    return "\n".join(lines)


def render_notice(text, output_format):
    if output_format not in {"STORY", "FEED"} or not text.strip() or len(text) > 2200:
        raise HTTPException(422, "공지 형식과 내용을 확인해주세요")
    size = (1080, 1920) if output_format == "STORY" else (1080, 1350)
    image = Image.new("RGB", size, "#fffaf3")
    draw_text(
        ImageDraw.Draw(image),
        text,
        (90, 190, 900, size[1] - 400),
        size=52,
        minimum=30,
        fill="#2c3027",
    )
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=92)
    return buffer.getvalue()
