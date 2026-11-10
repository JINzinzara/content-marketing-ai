"""보관된 비교 실험 이미지를 정량 지표로 평가한다. API 호출은 하지 않는다.

지표는 비슷한 과제의 선행 연구를 따른다(근거는 docs/metrics.md의 "지표 선정 근거").
- 상품 보존: 입력과 결과의 메인 쿠키 영역(`comparison/product_boxes.csv`)만 잘라 비교한다.
  DINO(ViT-S/16)와 CLIP-I(ViT-B/32)의 상품 영역 코사인 유사도(DreamBooth, Malhi et al. 2025의
  segmented DINO/CLIP-I), LPIPS, 그리고 화소 지표 PSNR·SSIM.
- 스타일 일치: VGG-16 Gram 행렬 스타일 거리(Gatys et al.)로 결과와 기존 Instagram 게시물 8장을 비교한다.
  CLIP 전역 유사도는 내용(쿠키·음료)과 스타일이 섞여 스타일 지표로 쓰지 않는다.
- 피드 일관성: 같은 조건 결과 5장끼리의 Gram 스타일 거리 평균.

생성 편집은 상품도 새로 그리므로 PSNR·SSIM은 낮게 나오는 것이 정상이다. 절대값보다 조건 간 비교를 본다.

설치: python -m pip install -e ".[eval]"
실행: python -m backend.eval_metrics [저장소 루트]
"""

import csv
import itertools
import statistics
import sys
from pathlib import Path

import numpy as np
from PIL import Image

from .config import ROOT
from .model_compare import read_json, write_csv

SIZE = 256
RUNS = {"final-fixed-01": "", "instagram-style-pipeline-01": "content/"}
CASES = ("original", "angle-higher", "angle-left", "light-bright-cool", "light-dim-warm")
FIELDS = ["run", "sample_id", "condition", "case", "product_dino", "product_clip", "lpips", "psnr", "ssim",
          "style_gram", "H01", "H02", "H03", "H04"]


def load_boxes(path):
    with open(path, encoding="utf-8") as f:
        return {r["image"]: tuple(int(r[k]) for k in ("x0", "y0", "x1", "y1")) for r in csv.DictReader(f)}


def human_grades(path):
    """두 실험의 판정표는 열 이름이 달라 위치로 읽는다: id, 영역, 그룹, 사례, T01~T09, H01~H04, 이유."""
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))[1:]
    return {r[0]: dict(zip(("H01", "H02", "H03", "H04"), r[13:17])) for r in rows}


def image_samples(runs):
    for run, sub in RUNS.items():
        manifest = read_json(runs / run / sub / "manifest.json")
        grades = human_grades(runs / run / "reviews.csv")
        for s in manifest["samples"]:
            if s["kind"] != "image":
                continue
            # final: 이미지 모델 비교, 스타일 경로: 분석 모델 비교(그룹)
            condition = s["model"] if run == "final-fixed-01" else "분석 " + {"A": "gpt-5.4-mini", "B": "gpt-5.4"}[s["group"]]
            yield {"run": run, "sample_id": s["id"], "condition": condition, "case": s["case"],
                   "path": runs / run / sub / "samples" / s["id"] / "generated-original.png",
                   "input": runs / "final-fixed-01" / "inputs" / f"{s['case']}.jpg", **grades.get(s["id"], {})}


def load(path, box=None):
    image = Image.open(path).convert("RGB")
    return image.crop(box) if box else image


class Metrics:
    """무거운 모델은 한 번만 불러온다."""

    def __init__(self):
        import lpips
        import open_clip
        import timm
        import torch
        import torchvision
        from skimage.metrics import peak_signal_noise_ratio, structural_similarity

        self.torch, self.psnr_fn, self.ssim_fn = torch, peak_signal_noise_ratio, structural_similarity
        self.lpips = lpips.LPIPS(net="alex", verbose=False).eval()
        self.clip, _, self.clip_pre = open_clip.create_model_and_transforms("ViT-B-32", pretrained="laion2b_s34b_b79k")
        self.dino = timm.create_model("vit_small_patch16_224.dino", pretrained=True, num_classes=0).eval()
        self.dino_pre = timm.data.create_transform(**timm.data.resolve_data_config({}, model=self.dino))
        vgg = torchvision.models.vgg16(weights=torchvision.models.VGG16_Weights.IMAGENET1K_V1).features.eval()
        self.vgg_blocks = [vgg[:4], vgg[4:9], vgg[9:16], vgg[16:23]]  # relu1_2, relu2_2, relu3_3, relu4_3
        self.vgg_pre = torchvision.transforms.Compose([
            torchvision.transforms.Resize((SIZE, SIZE)), torchvision.transforms.ToTensor(),
            torchvision.transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])])
        self.clip.eval()

    def _cos(self, model, pre, a, b, encode=None):
        with self.torch.no_grad():
            f = encode or model
            va, vb = f(pre(a)[None])[0], f(pre(b)[None])[0]
        return float(va @ vb / (va.norm() * vb.norm()))

    def product(self, a, b):
        """상품 영역 두 장(PIL)의 보존 지표."""
        x = np.asarray(a.resize((SIZE, SIZE), Image.LANCZOS))
        y = np.asarray(b.resize((SIZE, SIZE), Image.LANCZOS))
        t = lambda v: self.torch.from_numpy(v.copy()).permute(2, 0, 1)[None].float() / 127.5 - 1
        with self.torch.no_grad():
            perceptual = float(self.lpips(t(x), t(y)))
        return {"product_dino": self._cos(self.dino, self.dino_pre, a, b),
                "product_clip": self._cos(self.clip, self.clip_pre, a, b, self.clip.encode_image),
                "lpips": perceptual,
                "psnr": float(self.psnr_fn(x, y, data_range=255)),
                "ssim": float(self.ssim_fn(x, y, channel_axis=2, data_range=255))}

    def grams(self, image):
        h = self.vgg_pre(image)[None]
        out = []
        with self.torch.no_grad():
            for block in self.vgg_blocks:
                h = block(h)
                f = h[0].flatten(1)
                out.append(f @ f.T / f.shape[1])
        return out

    @staticmethod
    def gram_distance(g1, g2):
        """층별 Gram 행렬의 상대 오차 평균. 0이면 같은 스타일."""
        return float(statistics.mean(float((a - b).norm() / b.norm()) for a, b in zip(g1, g2)))


def main(root):
    runs = root / "backend" / "comparison" / "runs"
    boxes = load_boxes(Path(__file__).parent / "comparison" / "product_boxes.csv")
    m = Metrics()

    probe = load(runs / "final-fixed-01/inputs/original.jpg", boxes["input:original"])
    same = m.product(probe, probe)
    assert same["ssim"] > 0.999 and same["lpips"] < 1e-4 and same["product_dino"] > 0.999, "자기 자신 비교 검사 실패"

    refs = [m.grams(load(p)) for p in sorted((runs / "instagram-style-pipeline-01/references").glob("*.jpg"))]
    style = lambda g: statistics.mean(m.gram_distance(g, r) for r in refs)

    rows, grams = [], {}
    for s in image_samples(runs):
        g = m.grams(load(s["path"]))
        grams.setdefault((s["run"], s["condition"]), []).append(g)
        rows.append({k: s.get(k, "") for k in FIELDS}
                    | m.product(load(s["input"], boxes["input:" + s["case"]]), load(s["path"], boxes[s["sample_id"]]))
                    | {"style_gram": style(g)})
    baseline = statistics.mean(style(m.grams(load(runs / f"final-fixed-01/inputs/{c}.jpg"))) for c in CASES)

    out = runs / "image-metrics.csv"
    write_csv(out, [{k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()} for r in rows], FIELDS)

    print("| 실험 | 조건 | n | 상품 DINO↑ | 상품 CLIP-I↑ | LPIPS↓ | PSNR(dB)↑ | SSIM↑ | 피드 스타일 거리↓ | 결과 간 스타일 거리↓ |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for (run, cond), gs in grams.items():
        g = [r for r in rows if r["run"] == run and r["condition"] == cond]
        avg = lambda k: statistics.mean(r[k] for r in g)
        within = statistics.mean(m.gram_distance(a, b) for a, b in itertools.permutations(gs, 2))
        print(f"| {run} | {cond} | {len(g)} | {avg('product_dino'):.3f} | {avg('product_clip'):.3f} | {avg('lpips'):.3f} | "
              f"{avg('psnr'):.2f} | {avg('ssim'):.3f} | {avg('style_gram'):.3f} | {within:.3f} |")
    print(f"\n입력 사진 5장의 피드 스타일 거리(기준선): {baseline:.3f}")
    print(f"저장: {out}")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT)
