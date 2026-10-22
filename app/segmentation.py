"""로컬 CPU SlimSAM으로 선택한 상품 마스크를 만들고 검증 (외부 추론 API 없음)"""

import base64
import binascii
import hashlib
import importlib.util
import io
import os
from pathlib import Path
from threading import Lock
import urllib.request

from fastapi import HTTPException
from PIL import Image, ImageDraw

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
        self._lock = Lock()

    @property
    def ready(self):
        return importlib.util.find_spec("onnxruntime") is not None and all(
            (self.directory / filename).is_file() for filename in MODELS
        )

    def extract(self, canvas, box):
        """1024 정사각 사진과 xyxy 상자에서 상품 마스크(L)를 반환"""
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
            # SamProcessor의 고정 전처리. 이미 1024 정사각 캔버스라 추가 resize/pad는 없다.
            pixels = np.asarray(canvas.convert("RGB"), dtype=np.float32) / 255.0
            pixels = (
                pixels - np.array([0.485, 0.456, 0.406], dtype=np.float32)
            ) / np.array([0.229, 0.224, 0.225], dtype=np.float32)
            pixels = pixels.transpose(2, 0, 1)[None]
            x0, y0, x1, y1 = box
            seed = (
                min(SIZE[0] - 1, round((x0 + x1) / 2)),
                min(SIZE[1] - 1, round((y0 + y1) / 2)),
            )
            # 이 ONNX 배포는 box 입력이 없고 점 라벨 0/1만 지원한다.
            # 상자 중앙을 상품, 모서리를 배경으로 전달한다. 전체 자동 인식이 아니다.
            coordinates = [seed, (x0, y0), (x1 - 1, y1 - 1), (x0, y1 - 1), (x1 - 1, y0)]
            try:
                embeddings = self._encoder.run(None, {"pixel_values": pixels})
                outputs = self._decoder.run(
                    None,
                    {
                        "image_embeddings": embeddings[0],
                        "image_positional_embeddings": embeddings[1],
                        "input_points": np.asarray(coordinates, dtype=np.float32)[
                            None, None
                        ],
                        "input_labels": np.asarray([[[1, 0, 0, 0, 0]]], dtype=np.int64),
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
                if mask.getpixel(seed):
                    candidates.append((float(score), mask))
            if not candidates:
                raise HTTPException(
                    422,
                    "상품을 찾지 못했습니다. 상품 전체가 들어오도록 영역을 다시 지정해주세요",
                )
            _, mask = max(candidates, key=lambda candidate: candidate[0])
            # 중앙 상품과 떨어진 음식·배경 조각은 보존하지 않는다. 내부 구멍은 유지한다.
            mask = (
                mask.copy()
            )  # fromarray의 읽기 전용 버퍼를 floodfill이 수정할 수 있게 한다.
            ImageDraw.floodfill(mask, seed, 128)
            return mask.point(lambda value: 255 if value == 128 else 0)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="SlimSAM 로컬 모델 설치")
    parser.add_argument("--download", action="store_true", required=True)
    download_models()
