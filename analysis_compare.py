"""스타일 분석·추천의 고정 입력 A/B 비교. run --execute만 유료 호출한다."""

import argparse
import base64
import fcntl
import html
import importlib.metadata
import json
import re
import secrets
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
from fastapi import HTTPException

import model_compare as mc
from app import analytics
from app.config import ROOT, Settings
from app.rendering import atomic_write, normalize_photo
from app.styles import FIELDS as STYLE_FIELDS

TASKS = {"style": "스타일 분석", "recommendation": "추천"}
CRITERIA = ("자료 일치", "근거 연결", "단정 억제", "활용 가능")
REVIEW_FIELDS = ("sample_id", "영역", "그룹", "사례", *CRITERIA, "reason")
PREFERENCES = {"", "A 우세", "비슷함", "B 우세"}
STYLE_SCOPE_NOTE = "이 실험의 스타일 입력은 기존 인스타 피드가 아닌 상품 사진과 합성 캡션입니다. 실제 가게 스타일 분석 모델의 선정 근거에서 제외합니다. 추천 결과는 합성 지표에 대한 별도 탐색으로만 해석하세요."


def recommendation_input(case):
    rows = []
    for spec in case["rows"]:
        published = datetime(2026, 10, 19, spec.get("utc_hour", 0), tzinfo=timezone.utc)
        values = {"reach": spec["reach"], "saved": spec["saved"], "shares": spec["shares"]}
        rows.append({
            "media_id": spec["id"], "layout": "split", "purpose": spec.get("purpose", "상품 소개"),
            "promotion": "organic", "product_name": "합성 평가용 상품", "media_product_type": "FEED",
            "published_at": published.isoformat(), "observed_at": (published + timedelta(hours=24)).isoformat(),
            "metrics": {k: {"value": v, "status": "available" if v is not None else "pending"}
                        for k, v in values.items()},
        })
    info = analytics.analyze(rows, Settings())
    if not info["evidence"]:
        raise ValueError("앱이 추천 요청을 보내지 않는 자료 부족 사례입니다: " + case["id"])
    return {"observed_comparisons": info["evidence"], "limitations": info["limitations"]}


def prepare(config_path, out):
    config_path, out = Path(config_path).resolve(), Path(out).resolve()
    cfg = mc.read_json(config_path)
    if set(cfg["models"]) != {"gpt-5.4-mini", "gpt-5.4"}:
        raise ValueError("스타일·추천 비교 후보는 gpt-5.4-mini와 gpt-5.4입니다")
    if len(cfg["style_cases"]) != 5 or len(cfg["recommendation_cases"]) != 5:
        raise ValueError("기능마다 입력 5개가 필요합니다")
    for settings in cfg["models"].values():
        rates = settings["rates_per_million_usd"]
        if set(rates) != {"input", "cached_input", "output"} or any(mc.dollars(v) <= 0 for v in rates.values()):
            raise ValueError("모든 텍스트 단가를 양수로 입력하세요")
        if type(settings["max_output_tokens"]) is not int or not 256 <= settings["max_output_tokens"] <= 8192:
            raise ValueError("출력 한도는 256~8192 정수여야 합니다")
        if settings["reasoning_effort"] != "none":
            raise ValueError("이번 비교의 공통 추론 설정은 none입니다")
    if len({s["max_output_tokens"] for s in cfg["models"].values()}) != 1:
        raise ValueError("두 후보의 출력 한도를 동일하게 맞추세요")
    cases = []
    for case in cfg["style_cases"]:
        photo = normalize_photo((config_path.parent / case["file"]).read_bytes())
        data = {"task": "style_analysis", "shop_description": cfg["shop_description"], "reference_notes": "",
                "past_post_examples": [{"id": "local:" + case["id"], "caption": cfg["example_caption"][:2200], "image_index": 1}]}
        cases.append({**case, "task": "style", "data": data, "photo": photo})
    for case in cfg["recommendation_cases"]:
        cases.append({**case, "task": "recommendation", "data": recommendation_input(case),
                      "photo": None, "provenance": "통제된 합성 반응 지표. 실제 관측·매출 자료 아님"})
    if len({c["id"] for c in cases}) != 10 or any(not re.fullmatch(r"[a-z0-9-]+", c["id"]) for c in cases):
        raise ValueError("서로 다른 영문 소문자·숫자·하이픈 입력 ID 10개가 필요합니다")
    out.mkdir(parents=True, exist_ok=False, mode=0o700)
    for name in ("inputs", "requests", "samples"):
        (out / name).mkdir()
    atomic_write(out / "protocol.md", (ROOT / "README.md").read_bytes())
    manifest = {"version": 1, "suite": "analysis", "phase": "final", "run_id": secrets.token_hex(8),
                "created_at": time.time(), "config": cfg, "source_sha256": mc.source_hashes(),
                "files_sha256": {"protocol.md": mc.digest((out / "protocol.md").read_bytes())},
                "cases": [], "samples": [], "regeneration": "first_attempt_only", "python_version": sys.version,
                "dependencies": {n: importlib.metadata.version(n) for n in ("httpx", "Pillow", "pydantic")}}
    groups = {}
    for task in TASKS:
        names = list(cfg["models"])
        secrets.SystemRandom().shuffle(names)
        groups[task] = {m: chr(65 + i) for i, m in enumerate(names)}
    secrets.SystemRandom().shuffle(cases)
    for case in cases:
        image_path = "inputs/" + case["id"] + ".jpg" if case["photo"] else None
        images = []
        if image_path:
            atomic_write(out / image_path, case["photo"])
            manifest["files_sha256"][image_path] = mc.digest(case["photo"])
            images = ["data:image/jpeg;base64," + base64.b64encode(case["photo"]).decode()]
        human_input = "inputs/" + case["id"] + ".json"
        mc.save_json(out / human_input, {"provenance": case["provenance"], "model_input": case["data"]})
        manifest["files_sha256"][human_input] = mc.digest((out / human_input).read_bytes())
        manifest["cases"].append({"id": case["id"], "label": case["label"], "task": case["task"],
                                  "input": human_input, "image": image_path, "provenance": case["provenance"]})
        schedule = list(cfg["models"])
        secrets.SystemRandom().shuffle(schedule)
        for model in schedule:
            settings = cfg["models"][model]
            payload = mc.response_payload(model, case["data"], mc.RESULT_TYPES[case["task"]], images)
            payload.update(max_output_tokens=settings["max_output_tokens"], store=False,
                           service_tier="default", reasoning={"effort": "none"})
            sid = secrets.token_hex(6)
            request = "requests/" + sid + ".json"
            mc.save_json(out / request, payload)
            manifest["files_sha256"][request] = mc.digest((out / request).read_bytes())
            manifest["samples"].append({"id": sid, "kind": "text", "task": case["task"], "case": case["id"],
                                        "model": model, "group": groups[case["task"]][model], "request": request})
    mc.save_json(out / "manifest.json", manifest)
    atomic_write(out / "manifest.sha256", (mc.digest((out / "manifest.json").read_bytes()) + "\n").encode())
    labels = {c["id"]: c["label"] for c in manifest["cases"]}
    order = {c["id"]: i for i, c in enumerate(manifest["cases"])}
    review_order = sorted(manifest["samples"], key=lambda s: (list(TASKS).index(s["task"]), order[s["case"]], s["group"]))
    rows = [{**dict.fromkeys(REVIEW_FIELDS, ""), "sample_id": s["id"], "영역": TASKS[s["task"]],
             "그룹": s["group"], "사례": labels[s["case"]]} for s in review_order]
    mc.write_csv(out / "reviews.csv", rows, REVIEW_FIELDS)
    mc.write_csv(out / "preferences.csv", [{"영역": task, "preference": "", "reason": ""} for task in TASKS.values()],
                 ("영역", "preference", "reason"))
    atomic_write(out / "evaluator.txt", "본인\n".encode())
    report(out)
    return manifest


def display_output(task, value):
    if task == "style":
        return (value["summary"] + "\n\n" + "\n".join(label + ": " + value["attributes"][key] for key, label in STYLE_FIELDS.items())
                + "\n\n근거:\n" + "\n".join(value["evidence"]) + "\n\n미확인:\n" + "\n".join(value["uncertainties"]))
    labels = {"observation": "관찰", "next_content": "다음 제작 제안", "time_hypothesis": "시각 가설", "limitations": "한계"}
    return "\n\n".join(label + ":\n" + value[key] for key, label in labels.items())


def report(out):
    manifest = mc.frozen_manifest(out)
    if manifest.get("suite") != "analysis":
        raise ValueError("스타일·추천 비교 폴더를 지정하세요")
    samples = manifest["samples"]
    evaluator = (out / "evaluator.txt").read_text().strip()
    if not evaluator:
        raise ValueError("evaluator.txt의 사람 평가자 이름이 필요합니다")
    reviews = mc.reviewed_rows(out / "reviews.csv", ("sample_id",), {(s["id"],) for s in samples},
                               evaluator=evaluator, criteria={(s["id"],): CRITERIA for s in samples}, fields=REVIEW_FIELDS)
    preferences = mc.reviewed_rows(out / "preferences.csv", ("영역",), {(t,) for t in TASKS.values()},
                                   PREFERENCES, "preference", evaluator=evaluator)
    cases = {c["id"]: c for c in manifest["cases"]}
    states, verdicts = {}, {}
    for s in samples:
        row = reviews[(s["id"],)]
        if (row["영역"], row["그룹"], row["사례"]) != (TASKS[s["task"]], s["group"], cases[s["case"]]["label"]):
            raise ValueError("평가표의 ID·영역·그룹·사례는 변경하지 마세요")
        path = mc.record_path(out, s)
        states[s["id"]] = mc.read_json(path) if path.exists() else {"state": "planned", "cost_microusd": None}
        grades = [row[c] for c in CRITERIA]
        if states[s["id"]]["state"] == "completed":
            verdicts[s["id"]] = "불합격" if any(g in {"부분 충족", "미충족"} for g in grades) else "합격" if all(g == "충족" for g in grades) else "미평가"
        else:
            verdicts[s["id"]] = "생성 전" if states[s["id"]]["state"] == "planned" else "생성 실패"
    summary = []
    for task in TASKS:
        for model in manifest["config"]["models"]:
            selected = [s for s in samples if s["task"] == task and s["model"] == model]
            records = [states[s["id"]] for s in selected]
            attempted = [r for r in records if r["state"] != "planned"]
            known = sum(r["cost_microusd"] for r in attempted if r.get("cost_microusd") is not None)
            unknown = sum(r.get("cost_microusd") is None for r in attempted)
            passed = sum(verdicts[s["id"]] == "합격" for s in selected)
            unreviewed = sum(verdicts[s["id"]] == "미평가" for s in selected)
            times = [r["api_seconds"] for r in attempted if "api_seconds" in r]
            pass_times = [states[s["id"]]["api_seconds"] for s in selected if verdicts[s["id"]] == "합격"]
            ready_times = [r["cli_artifact_ready_seconds"] for r in attempted if "cli_artifact_ready_seconds" in r]
            bases = {r.get("cost_basis", "unknown") for r in attempted if r.get("cost_microusd") is not None}
            if unknown:
                bases.add("unknown")
            all_sent = all(r.get("request_started_at") for r in records)
            summary.append({"task": TASKS[task], "model": model, "group": selected[0]["group"], "planned": len(selected),
                            "completed": sum(r["state"] == "completed" for r in records), "passed": passed, "unreviewed": unreviewed,
                            "failed_or_unknown": sum(r["state"] not in {"completed", "planned", "not_sent"} for r in records),
                            "not_sent": sum(r["state"] == "not_sent" for r in records),
                            "known_cost_usd": str(Decimal(known) / 1_000_000), "unknown_cost_count": unknown,
                            "cost_per_pass_usd": str(Decimal(known) / 1_000_000 / passed) if passed and not unknown and not unreviewed and all_sent else "",
                            "api_median_seconds": statistics.median(times) if times else "",
                            "passed_api_median_seconds": statistics.median(pass_times) if pass_times else "",
                            "cli_artifact_ready_median_seconds": statistics.median(ready_times) if ready_times else "",
                            "cost_basis": next(iter(bases)) if len(bases) == 1 else "mixed" if bases else "not_measured",
                            "browser_user_wait_seconds": ""})
    mc.write_csv(out / "summary.csv", summary, tuple(summary[0]))
    mc.save_json(out / "results.json", {"summary": summary, "evaluator": evaluator,
        "style_selection_valid": False, "style_scope_note": STYLE_SCOPE_NOTE,
        "attempts": [{**s, **states[s["id"]], "verdict": verdicts[s["id"]]} for s in samples],
        "human_reviews": list(reviews.values()), "human_preferences": list(preferences.values()),
        "evidence_scope": "기능별 원본·합성 사진 또는 합성 지표 입력 5개, 각 1회. 실제 피드 전반·매출 효과·동일 입력 반복 안정성 증거 아님.",
        "cost_note": "표준 단가의 사용량 추정. 청구 확인 정산과 혼합·비용 불명을 구분함. 예약 금액은 실제 청구액이 아님."})
    esc = lambda v: html.escape(str(v))
    sections = []
    for task, label in TASKS.items():
        sections.append('<h2>' + esc(label) + '</h2>')
        for case in manifest["cases"]:
            if case["task"] != task:
                continue
            data = mc.read_json(out / case["input"])
            media = '<img alt="분석 입력" src="' + esc(case["image"]) + '">' if case["image"] else ""
            sections.append('<section><h3>' + esc(case["label"]) + '</h3><p>' + esc(case["provenance"]) + '</p>'
                            + media + '<details><summary>공통 입력 확인</summary><pre>' + esc(json.dumps(data["model_input"], ensure_ascii=False, indent=2)) + '</pre></details><div class="pair">')
            for s in sorted((s for s in samples if s["case"] == case["id"]), key=lambda s: s["group"]):
                path = out / "samples" / s["id"] / "output.json"
                output = display_output(task, mc.read_json(path)) if path.exists() else "아직 완성된 출력 없음"
                checks = " · ".join(c + ": " + (reviews[(s["id"],)][c] or "미평가") for c in CRITERIA)
                sections.append('<article><h4>그룹 ' + esc(s["group"]) + '</h4><small>ID: ' + esc(s["id"])
                                + '</small><pre>' + esc(output) + '</pre><p>' + esc(states[s["id"]]["state"])
                                + ' / ' + esc(verdicts[s["id"]]) + '</p><small>' + esc(checks) + '</small></article>')
            sections.append('</div></section>')
    page = ('<!doctype html><html lang="ko"><meta charset="utf-8"><title>스타일·추천 A/B 검토</title>'
            '<style>body{font:16px system-ui;max-width:1200px;margin:30px auto;padding:0 20px;background:#f8f4ee;color:#39291f}img{width:240px;height:auto}.pair{display:grid;grid-template-columns:1fr 1fr;gap:18px}article{background:white;padding:18px;border-radius:12px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}section{margin:30px 0}small{line-height:1.7}@media(max-width:650px){.pair{grid-template-columns:1fr}}</style>'
            '<h1>스타일 분석·추천 A/B 검토</h1><p style="background:#fff0ce;padding:16px">' + esc(STYLE_SCOPE_NOTE) + '</p><p>모델명과 비용을 가린 결과입니다. 각 기능의 A/B 표시는 독립적입니다.</p>'
            '<p><a href="protocol.md">평가 기준과 실험 범위</a> · <a href="reviews.csv">개별 평가표 20행</a> · <a href="preferences.csv">선호 평가표 2행</a></p>'
            '<p>자료 일치·근거 연결·단정 억제·활용 가능에 충족/부분 충족/미충족을 입력하세요. 부분 충족은 불합격이며, 문제가 있는 결과와 상대 선호에만 이유를 씁니다. 공통 평가자는 evaluator.txt의 본인입니다. 생성 완료는 품질 합격이 아닙니다.</p>'
            + ''.join(sections) + '</html>')
    atomic_write(out / "review.html", page.encode())
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("prepare")
    p.add_argument("--config", type=Path, default=ROOT / "comparison/analysis-mini.json")
    p.add_argument("--out", type=Path, required=True)
    for command in ("run", "report", "settle"):
        p = commands.add_parser(command)
        p.add_argument("out", type=Path)
        if command == "run":
            p.add_argument("--execute", action="store_true")
            p.add_argument("--limit", type=int)
        if command == "settle":
            p.add_argument("--sample", required=True)
            p.add_argument("--cost-usd", required=True)
            p.add_argument("--note", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            prepare(args.config, args.out)
            print("스타일·추천 비교 20개 계획 준비 완료: " + str(args.out.resolve()))
            return
        out = args.out.resolve()
        with (out / ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            manifest = mc.frozen_manifest(out)
            if manifest.get("suite") != "analysis":
                raise ValueError("스타일·추천 비교 폴더가 아닙니다")
            if args.command == "report":
                report(out)
                print("사람 판정 집계 완료: " + str(out / "summary.csv"))
            elif args.command == "settle":
                mc.settle(out, args.sample, args.cost_usd, args.note, Settings.from_env(), reporter=report)
                print("청구 확인 금액 기록 완료. 같은 슬롯을 재생성하지 않습니다")
            elif not args.execute:
                print("유료 호출 없음. 남은 계획 " + str(sum(not mc.record_path(out, s).exists() for s in manifest["samples"])))
            else:
                if args.limit is not None and args.limit < 1:
                    raise ValueError("limit은 1 이상이어야 합니다")
                with httpx.Client(transport=httpx.HTTPTransport(retries=0), timeout=httpx.Timeout(180, connect=10)) as client:
                    mc.run(out, Settings.from_env(), client, args.limit, reporter=report)
    except (ValueError, OSError, KeyError, StopIteration, HTTPException) as exc:
        parser.exit(1, (str(exc.detail) if isinstance(exc, HTTPException) else str(exc)) + "\n")


if __name__ == "__main__":
    main()
