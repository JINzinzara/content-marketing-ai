"""기존에 저장된 상품 마스크의 PNG 직렬화·검증만 유지한다. 새 추론은 하지 않는다."""

import base64
import binascii
import io
from fastapi import HTTPException
from PIL import Image

SIZE = (1024, 1024)


def encode_mask(mask):
    out = io.BytesIO()
    mask.save(out, "PNG")
    return base64.b64encode(out.getvalue()).decode("ascii")


def decode_mask(value):
    """사용자 마스크의 크기·형식·비어 있지 않은 선택 영역 확인"""
    try:
        if not isinstance(value, str) or len(value) > 2 * 1024 * 1024:
            raise ValueError
        raw = base64.b64decode(value, validate=True)
        with Image.open(io.BytesIO(raw)) as image:
            if (
                image.format != "PNG"
                or image.size != SIZE
                or image.mode not in {"1", "L"}
            ):
                raise ValueError
            mask = image.convert("L")
        if not mask.getbbox():
            raise ValueError
        return mask
    except (ValueError, binascii.Error, OSError, Image.DecompressionBombError):
        raise HTTPException(
            422, "상품 선택 결과가 올바르지 않습니다. 다시 선택해주세요"
        ) from None
