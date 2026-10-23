"""로컬 CPU SlimSAM으로 선택한 상품 마스크를 만들고 검증 (외부 추론 API 없음)"""

import base64
import binascii
import hashlib
import importlib.util
import io
import json
import math
import os
from pathlib import Path
from threading import Lock
import urllib.request

from fastapi import HTTPException
from PIL import Image, ImageDraw, ImageFilter

MODEL_DIR = Path(__file__).resolve().parent.parent / "models/slimsam"
REVISION = "5850ab45f587c112167512ffef949107115e26a0"
# 공개 SlimSAM ONNX 배포의 고정 버전·LFS SHA-256. 양자화 전 float 모델을 사용
MODELS = {
    "vision_encoder.onnx": (
        23276014,
        "9f8433273a6750b587779baa0cf5508111001bf7e7acfcf585d370139fd366d0",
    ),
    "prompt_encoder_mask_decoder.onnx": (
        16557892,
        "f4514391764fbd56e08e119060d874ecd7d52994bfb1968af159e12d4943b5bb",
    ),
}
SIZE = (1024, 1024)


def correction_points(value):
    """원본 사진의 정규화 포함(1)·제외(0) 점을 최대 20개 검증한다."""
    try:
        points = json.loads(value)
        if not isinstance(points, list) or len(points) > 20:
            raise ValueError
        for point in points:
            if not isinstance(point, list) or len(point) != 3:
                raise ValueError
            if any(
                type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1
                for v in point[:2]
            ):
                raise ValueError
            if type(point[2]) is not int or point[2] not in (0, 1):
                raise ValueError
        return points
    except (ValueError, TypeError):
        raise HTTPException(422, "보정점을 다시 표시하세요") from None


def refine_edges(canvas, mask):
    """사진 명암을 가이드로 안쪽 경계의 알파만 보정하고 내부·구멍을 보존한다."""
    import numpy as np

    # Guided Image Filtering(He et al., 2010)의 회색조 식. 반경 4·eps .001은 프로젝트 초기값.
    radius = 4

    def mean(values):
        padded = np.pad(values, radius, mode="edge")
        sums = np.pad(padded, ((1, 0), (1, 0))).cumsum(0, dtype=np.float64).cumsum(1)
        width = radius * 2 + 1
        return (
            sums[width:, width:] - sums[:-width, width:]
            - sums[width:, :-width] + sums[:-width, :-width]
        ) / width ** 2

    guide = np.asarray(canvas.convert("L"), dtype=np.float64) / 255
    alpha = np.asarray(mask, dtype=np.float64) / 255
    mi, mp = mean(guide), mean(alpha)
    variance = np.maximum(0, mean(guide * guide) - mi * mi)
    a = (mean(guide * alpha) - mi * mp) / (variance + .001)
    q = np.clip(mean(a) * guide + mean(mp - a * mi), 0, 1)
    # 바깥 배경을 다시 살리지 않고, 안쪽 3px만 혼합한다. 투명 유리 매팅은 아니다.
    inside = np.asarray(mask.filter(ImageFilter.MinFilter(7))) == 255
    refined = np.where(alpha == 0, 0, np.where(inside, 255, np.rint(q * 255)))
    return Image.fromarray(refined.astype(np.uint8))


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


def download_models(directory=MODEL_DIR):
    """고정 공개 모델을 내려받고 크기·SHA-256을 확인한 뒤 설치"""
    directory.mkdir(parents=True, exist_ok=True)
    for filename, (size, digest) in MODELS.items():
        target = directory / filename
        if (
            target.is_file()
            and hashlib.sha256(target.read_bytes()).hexdigest() == digest
        ):
            continue
        temporary = target.with_suffix(".download")
        url = f"https://huggingface.co/Xenova/slimsam-77-uniform/resolve/{REVISION}/onnx/{filename}"
        try:
            checksum, downloaded = hashlib.sha256(), 0
            with (
                urllib.request.urlopen(url, timeout=60) as response,
                temporary.open("wb") as output,
            ):
                while chunk := response.read(1024 * 1024):
                    downloaded += len(chunk)
                    if downloaded > size:
                        raise ValueError("모델 파일 크기가 다릅니다")
                    checksum.update(chunk)
                    output.write(chunk)
            if downloaded != size or checksum.hexdigest() != digest:
                raise ValueError("모델 파일의 크기 또는 SHA-256이 다릅니다")
            os.replace(temporary, target)
            print(f"검증 완료: {filename} ({size:,} bytes)")
        finally:
            temporary.unlink(missing_ok=True)


class ProductSegmenter:
    """이미지마다 상품 하나를 지정하는 SlimSAM. 세션은 재사용하고 추론은 직렬화"""

    def __init__(self, directory=MODEL_DIR):
        self.directory = directory
        self._encoder = self._decoder = None
        self._image_key = self._embeddings = None
        self._lock = Lock()

    @property
    def ready(self):
        return importlib.util.find_spec("onnxruntime") is not None and all(
            (self.directory / filename).is_file() for filename in MODELS
        )

    def extract(self, canvas, box, corrections=()):
        """상자와 픽셀 좌표 포함·제외 점으로 상품 마스크(L)를 반환한다."""
        if not self.ready:
            raise HTTPException(
                503,
                "자동 상품 선택 모델을 설치해주세요. 수동 경계도 사용할 수 있습니다",
            )
        import numpy as np
        import onnxruntime as ort

        ort.disable_telemetry_events()

        with self._lock:
            if self._encoder is None:
                for filename, (_, digest) in MODELS.items():
                    if (
                        hashlib.sha256(
                            (self.directory / filename).read_bytes()
                        ).hexdigest()
                        != digest
                    ):
                        raise HTTPException(503, "상품 선택 모델을 다시 설치해주세요")
                try:
                    self._encoder = ort.InferenceSession(
                        str(self.directory / "vision_encoder.onnx"),
                        providers=["CPUExecutionProvider"],
                    )
                    self._decoder = ort.InferenceSession(
                        str(self.directory / "prompt_encoder_mask_decoder.onnx"),
                        providers=["CPUExecutionProvider"],
                    )
                except Exception:
                    self._encoder = self._decoder = None
                    raise HTTPException(
                        503, "자동 상품 선택 모델을 불러오지 못했습니다"
                    ) from None
            x0, y0, x1, y1 = box
            seed = (
                min(SIZE[0] - 1, round((x0 + x1) / 2)),
                min(SIZE[1] - 1, round((y0 + y1) / 2)),
            )
            # 이 ONNX 배포는 box 입력이 없고 점 라벨 0/1만 지원한다.
            # 상자 중앙을 상품, 모서리를 배경으로 전달한다. 전체 자동 인식이 아니다.
            positives = [(x, y) for x, y, label in corrections if label == 1]
            # 사용자가 포함점을 찍으면 상자 중앙 대신 그 점을 상품 기준으로 삼는다.
            if positives:
                seed = positives[-1]
            prompts = {seed: 1, (x0, y0): 0, (x1 - 1, y1 - 1): 0, (x0, y1 - 1): 0, (x1 - 1, y0): 0}
            explicit = {(x, y): label for x, y, label in corrections}
            prompts.update(explicit)
            anchors = [point for point, label in prompts.items() if label == 1]
            if not anchors:
                raise HTTPException(422, "상품 포함점을 표시하세요")
            try:
                # 직전 사진 한 장만 메모리에 보관한다. 보정 시 이미지 encoder를 재실행하지 않는다.
                image_key = hashlib.sha256(canvas.tobytes()).digest()
                if self._image_key != image_key:
                    pixels = np.asarray(canvas.convert("RGB"), dtype=np.float32) / 255.0
                    pixels = (
                        pixels - np.array([.485, .456, .406], dtype=np.float32)
                    ) / np.array([.229, .224, .225], dtype=np.float32)
                    embeddings = self._encoder.run(
                        None, {"pixel_values": pixels.transpose(2, 0, 1)[None]}
                    )
                    self._image_key, self._embeddings = image_key, embeddings
                embeddings = self._embeddings
                outputs = self._decoder.run(
                    None,
                    {
                        "image_embeddings": embeddings[0],
                        "image_positional_embeddings": embeddings[1],
                        "input_points": np.asarray(list(prompts), dtype=np.float32)[
                            None, None
                        ],
                        "input_labels": np.asarray(list(prompts.values()), dtype=np.int64)[None, None],
                    },
                )
            except Exception:
                raise HTTPException(
                    503,
                    "상품 자동 선택에 실패했습니다. 영역을 다시 지정하거나 수동 경계를 사용해주세요",
                ) from None
            mapped = dict(
                zip((item.name for item in self._decoder.get_outputs()), outputs)
            )
            scores = mapped["iou_scores"].reshape(-1)
            masks = mapped["pred_masks"].reshape(-1, 256, 256)
            candidates = []
            for score, logits in zip(scores, masks):
                full = Image.fromarray(logits).resize(SIZE, Image.Resampling.BILINEAR)
                mask = Image.fromarray((np.asarray(full) > 0).astype(np.uint8) * 255)
                if all(mask.getpixel(point) for point in anchors) and all(
                    bool(mask.getpixel((x, y))) == bool(label)
                    for (x, y), label in explicit.items()
                ):
                    candidates.append((float(score), mask))
            if not candidates:
                raise HTTPException(
                    422,
                    "포함·제외점을 조정하세요",
                )
            _, mask = max(candidates, key=lambda candidate: candidate[0])
            # 포함점을 찍은 성분만 보존한다. 내부 구멍과 명시한 제외점은 유지한다.
            mask = (
                mask.copy()
            )  # fromarray의 읽기 전용 버퍼를 floodfill이 수정할 수 있게 한다.
            for anchor in anchors:
                if mask.getpixel(anchor) == 255:
                    ImageDraw.floodfill(mask, anchor, 128)
            return mask.point(lambda value: 255 if value == 128 else 0)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="SlimSAM 로컬 모델 설치")
    parser.add_argument("--download", action="store_true", required=True)
    download_models()
