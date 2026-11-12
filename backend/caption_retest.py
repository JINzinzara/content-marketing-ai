"""문구 말투 개선 전후를 같은 상품으로 다시 생성해 비교한다.

- 대상: 측정 계정(local-owner)의 반복 사용 실측 게시물 5건과 같은 사진·상품 정보·스타일
- 조건: before(이전 지시문, 말투 규칙 없음) / after(현재 지시문 + shop_voice_rules), 각 2회
- 지표: 마침표로 끝나는 줄, 격식체 끝맺음, 이모지 줄 비율, 입력에 없는 감각 표현 수
- 게시물은 바꾸지 않는다. 호출은 별도 계정(caption-retest)의 비용 원장에 기록한다.
- 결과가 저장된 조합은 다시 호출하지 않는다.

실행: python -m backend.caption_retest [저장소 루트] [--execute]
"""

import argparse
import json
import random
import statistics
import sys
import time
from pathlib import Path

import httpx

from . import text_ai
from .config import ROOT
from .db import connect
from .eval_judge import judge_settings
from .model_compare import read_json, save_json
from .text_ai import Draft, TextAI, voice_stats

OWNER, SOURCE = "caption-retest", "local-owner"
VARIANTS, REPS = ("before", "after"), 2
BEFORE_INSTRUCTIONS = text_ai.INSTRUCTIONS.replace(text_ai.VOICE_INSTRUCTIONS, "")
# ponytail: 감각 표현은 고정 목록으로 센다. 목록에 없는 표현은 놓친다(한계로 보고).
SENSORY = ("부드러", "부드럽", "진한", "진하", "따뜻", "따듯", "차가", "시원", "고소", "달콤", "달달", "상큼", "촉촉",
           "바삭", "쫀득", "풍미", "깊은", "향긋", "쌉싸", "진득", "크리미")


def unprovided_sensory(caption, facts):
    given = json.dumps(facts, ensure_ascii=False)
    return [w for w in SENSORY if w in caption and w not in given]


def targets(settings):
    with connect(settings) as con:
        shop = dict(con.execute("SELECT * FROM accounts WHERE id=?", (SOURCE,)).fetchone())
        posts = [dict(r) for r in con.execute(
            "SELECT * FROM posts WHERE owner_id=? AND deleted_at IS NULL AND finalized_at IS NOT NULL ORDER BY created_at",
            (SOURCE,))]
    return shop, posts


def execute(settings, out):
    shop, posts = targets(settings)
    with connect(settings, write=True) as con:
        con.execute("INSERT OR IGNORE INTO accounts(id, created_at) VALUES (?, ?)", (OWNER, int(time.time())))
        # 게시물을 확정하지 않으므로 이 제작권은 차감되지 않는다(AI 호출 전 잔액 검사용).
        con.execute("INSERT OR IGNORE INTO credits(owner_id, reference, amount, created_at) VALUES (?, 'grant:caption-retest', 1, ?)",
                    (OWNER, int(time.time())))
    ai = TextAI(settings, httpx.Client(timeout=120))
    for rep in range(1, REPS + 1):
        for post in posts:
            for variant in VARIANTS:
                path = out / f"{post['id'][:8]}-{variant}-{rep}.json"
                if path.exists():
                    continue
                data, image, facts = ai.draft_request(SOURCE, shop, post)
                if variant == "before":
                    data.pop("shop_voice_rules")
                text_ai.INSTRUCTIONS = BEFORE_INSTRUCTIONS if variant == "before" else BEFORE_INSTRUCTIONS + text_ai.VOICE_INSTRUCTIONS
                started = time.monotonic()
                try:
                    draft = ai.checked_draft(ai.generate(OWNER, "draft", data, Draft, images=[image]), facts)
                except Exception as exc:  # 결과를 모르면 멈춘다. 자동 재호출하지 않는다.
                    print(f"{post['product_name']} {variant} #{rep}: 실패, 중단 ({exc})")
                    return
                finally:
                    text_ai.INSTRUCTIONS = BEFORE_INSTRUCTIONS + text_ai.VOICE_INSTRUCTIONS
                save_json(path, {"post_id": post["id"], "product": post["product_name"], "variant": variant, "rep": rep,
                                 "facts": facts, "seconds": time.monotonic() - started, **draft.model_dump()})
                print(f"{post['product_name']} {variant} #{rep}: 완료", flush=True)


def report(settings, out):
    rows = [read_json(p) for p in sorted(out.glob("*.json")) if p.name != "blind-key.json"]
    with connect(settings) as con:
        feed = [r[0] for r in con.execute("SELECT caption FROM history WHERE owner_id=? AND deleted_at IS NULL", (SOURCE,))]
        cost = con.execute("SELECT COALESCE(SUM(cost_microusd), 0) FROM ai_calls WHERE owner_id=?", (OWNER,)).fetchone()[0]
    print("| 조건 | 문구 수 | 마침표 끝 줄 | 격식체 끝 줄 | 이모지 줄 | 입력에 없는 감각 표현(건) |")
    print("|---|---|---|---|---|---|")
    f = voice_stats(feed)
    print(f"| 기존 피드 | {len(feed)} | {f['period']:.0%} | {f['formal']:.0%} | {f['emoji']:.0%} | - |")
    for variant in VARIANTS:
        g = [r for r in rows if r["variant"] == variant]
        if not g:
            continue
        s = voice_stats([r["caption"] for r in g])
        hits = sum(len(unprovided_sensory(r["caption"], r["facts"])) for r in g)
        print(f"| {variant} | {len(g)} | {s['period']:.0%} | {s['formal']:.0%} | {s['emoji']:.0%} | {hits} |")
    print(f"\n문구 생성 비용(사용량 추정): ${cost / 1_000_000:.4f}, 평균 생성 시간 "
          f"{statistics.mean(r['seconds'] for r in rows):.1f}초" if rows else "")
    blind(out, [r for r in rows if r["rep"] == 1])


def blind(out, rows):
    """상품별 before/after 1회차를 순서를 섞어 blind.md로, 정답은 blind-key.json으로 남긴다."""
    if not rows or (out / "blind-key.json").exists():
        return
    rng, key, lines = random.Random(20261112), {}, ["# 문구 블라인드 비교\n", "상품마다 더 나은 문구(1 또는 2)와 이유를 적어 주세요.\n"]
    for product in dict.fromkeys(r["product"] for r in rows):
        pair = [r for r in rows if r["product"] == product]
        rng.shuffle(pair)
        key[product] = [r["variant"] for r in pair]
        lines.append(f"\n## {product}\n")
        for i, r in enumerate(pair, 1):
            lines.append(f"\n**문구 {i}**\n\n{r['caption']}\n\n{r['hashtags']}\n")
        lines.append("\n선택: \n이유: \n")
    (out / "blind.md").write_text("".join(lines), encoding="utf-8")
    save_json(out / "blind-key.json", key)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", type=Path, default=ROOT)
    parser.add_argument("--execute", action="store_true", help="API를 호출해 빠진 조합을 생성한다")
    args = parser.parse_args(argv)
    settings = judge_settings(args.root)
    out = args.root / "backend" / "comparison" / "runs" / "caption-retest-01"
    out.mkdir(parents=True, exist_ok=True)
    if args.execute:
        execute(settings, out)
    report(settings, out)


if __name__ == "__main__":
    main(sys.argv[1:])
