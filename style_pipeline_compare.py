"""같은 인스타 피드 → 두 스타일 분석 → 같은 문구·이미지 모델의 비교."""

import argparse
import base64
import fcntl
import html
import importlib.metadata
import json
import re
import secrets
import sqlite3
import statistics
import sys
import time
from decimal import Decimal
from pathlib import Path

import httpx
from fastapi import HTTPException

import analysis_compare as ac
import model_compare as mc
from app import styles
from app.config import ROOT, Settings
from app.rendering import atomic_write, normalize_photo

KINDS = {"text": "문구", "image": "이미지"}
CRITERIA = {k: tuple(v.values()) for k, v in mc.CRITERIA.items()}
REVIEW_FIELDS = ("sample_id", "영역", "그룹", "사례", *CRITERIA["text"], *CRITERIA["image"], "reason")
BUNDLE_GRADES = ("기존 게시물 일치", "묶음 일관성")
BUNDLE_FIELDS = ("영역", "그룹", *BUNDLE_GRADES, "reason")


def freeze(out, manifest):
    mc.save_json(out / "manifest.json", manifest)
    atomic_write(out / "manifest.sha256", (mc.digest((out / "manifest.json").read_bytes()) + "\n").encode())


def save_input(out, manifest, name, raw):
    atomic_write(out / name, raw)
    manifest["files_sha256"][name] = mc.digest(raw)


def stage_manifest(root, manifest):
    return {k: manifest[k] for k in ("version", "suite", "phase", "created_at", "config", "source_sha256", "dependencies")} | {
        "run_id": secrets.token_hex(8), "samples": [], "regeneration": "first_attempt_only",
        "files_sha256": {"../manifest.json": mc.digest((root / "manifest.json").read_bytes())}}


def text_payload(model, data, result_type, photos, cfg):
    images = ["data:image/jpeg;base64," + base64.b64encode(p).decode() for p in photos]
    payload = mc.response_payload(model, data, result_type, images)
    payload.update(max_output_tokens=cfg["max_output_tokens"], store=False, service_tier="default",
                   reasoning={"effort": cfg["reasoning_effort"]})
    return payload


def prepare(references, out, config_path=ROOT / "comparison/spooky-cookie.json"):
    references, out, config_path = Path(references).resolve(), Path(out).resolve(), Path(config_path).resolve()
    source, product = mc.read_json(references), mc.read_json(config_path)
    refs = source["references"]
    if len(refs) != 8 or len({r["id"] for r in refs}) != 8 or any(not re.fullmatch(r"ig:\d+", r["id"]) for r in refs):
        raise ValueError("이전 확정 스타일에 사용한 인스타 참고 자료 8개가 필요합니다")
    ref_photos = [Path(r["file"]).read_bytes() for r in refs]
    if any(mc.digest(p) != r["sha256"] for r, p in zip(refs, ref_photos)):
        raise ValueError("확보한 인스타 사진 해시가 다릅니다")
    ref_photos = [normalize_photo(p) for p in ref_photos]
    cases = product["cases"]
    if len(cases) != 5 or len({c["id"] for c in cases}) != 5 or any(not re.fullmatch(r"[a-z0-9-]+", c["id"]) for c in cases):
        raise ValueError("서로 다른 상품 입력 5개가 필요합니다")
    case_photos = [normalize_photo((config_path.parent / c["file"]).read_bytes()) for c in cases]
    models = {m: product["models"][m] for m in ("gpt-5.4-mini", "gpt-5.4", "gpt-image-2")}
    for model, cfg in models.items():
        expected = {"input", "cached_input", "output"} if mc.MODELS[model] == "text" else {"text_input", "image_input", "output"}
        if set(cfg["rates_per_million_usd"]) != expected or any(mc.dollars(v) <= 0 for v in cfg["rates_per_million_usd"].values()):
            raise ValueError("필수 단가를 양수로 입력하세요")
        if mc.MODELS[model] == "text" and (cfg["max_output_tokens"] != 2048 or cfg["reasoning_effort"] != "none"):
            raise ValueError("텍스트 설정은 2048·none으로 고정합니다")
    if models["gpt-image-2"]["quality"] != "low" or models["gpt-image-2"]["size"] != "1024x1024" or mc.dollars(models["gpt-image-2"]["reserve_usd"]) <= 0:
        raise ValueError("이미지 설정은 low·1024x1024와 양수 예약 금액이 필요합니다")
    # 과거 5.4의 분석 내용은 감사 기록에만 남기고 어느 모델의 입력에도 섞지 않는다.
    shop = {k: source["shop"][k] for k in ("name", "description", "reference_notes", "color")}
    names = ["gpt-5.4", "gpt-5.4-mini"]
    secrets.SystemRandom().shuffle(names)
    groups = dict(zip(("A", "B"), names))
    manifest = {"version": 1, "suite": "style_pipeline", "phase": "final", "created_at": time.time(),
        "config": {"models": models, **{k: product[k] for k in ("product", "caption_requirements", "main_product")}},
        "source_sha256": mc.source_hashes(), "files_sha256": {}, "groups": groups, "shop": shop,
        "references": [], "cases": [], "style_slots": [], "samples": [], "python_version": sys.version,
        "dependencies": {n: importlib.metadata.version(n) for n in ("httpx", "Pillow", "pydantic")}}
    out.mkdir(parents=True, exist_ok=False, mode=0o700)
    for name in ("inputs", "references", "styles", "styles/requests", "styles/samples"):
        (out / name).mkdir()
    save_input(out, manifest, "protocol.md", (ROOT / "README.md").read_bytes())
    audit = {"profile_version": source["profile_version"], "previous_profile": source["previous_profile"]}
    save_input(out, manifest, "previous-confirmed-profile.json", json.dumps(audit, ensure_ascii=False, indent=2).encode())
    for i, (ref, photo) in enumerate(zip(refs, ref_photos), 1):
        name = "references/" + ref["id"].replace(":", "-") + ".jpg"
        save_input(out, manifest, name, photo)
        manifest["references"].append({k: ref[k] for k in ("id", "caption", "permalink", "published_at")} | {
            "image": name, "image_index": i, "source_sha256": ref["sha256"]})
    for case, photo in zip(cases, case_photos):
        name = "inputs/" + case["id"] + ".jpg"
        save_input(out, manifest, name, photo)
        manifest["cases"].append({k: case[k] for k in ("id", "provenance")} | {"image": name})
    for group, analyzer in groups.items():
        manifest["style_slots"].append({"id": secrets.token_hex(6), "case": "instagram-feed", "kind": "text",
            "task": "style", "model": analyzer, "group": group})
        for case in cases:
            for kind, model in (("text", "gpt-5.4-mini"), ("image", "gpt-image-2")):
                manifest["samples"].append({"id": secrets.token_hex(6), "case": case["id"], "kind": kind,
                    "model": model, "group": group, "analyzer": analyzer})
    freeze(out, manifest)
    stage = stage_manifest(out, manifest)
    data = {"task": "style_analysis", "shop_description": shop["description"], "reference_notes": shop["reference_notes"],
            "past_post_examples": [{"id": r["id"], "caption": r["caption"][:2200], "image_index": r["image_index"]} for r in manifest["references"]]}
    for sample in manifest["style_slots"]:
        payload = text_payload(sample["model"], data, mc.Style, ref_photos, models[sample["model"]])
        name = "requests/" + sample["id"] + ".json"
        save_input(out / "styles", stage, name, json.dumps(payload, ensure_ascii=False).encode())
        stage["samples"].append(sample | {"request": name})
    freeze(out / "styles", stage)
    rows = [{**dict.fromkeys(REVIEW_FIELDS, ""), "sample_id": s["id"], "영역": KINDS[s["kind"]],
             "그룹": s["group"], "사례": s["case"]} for s in sorted(manifest["samples"], key=lambda s: (s["kind"], s["case"], s["group"]))]
    mc.write_csv(out / "reviews.csv", rows, REVIEW_FIELDS)
    mc.write_csv(out / "bundles.csv", [{"영역": kind, "그룹": group, **dict.fromkeys((*BUNDLE_GRADES, "reason"), "")}
                 for kind in KINDS.values() for group in groups], BUNDLE_FIELDS)
    mc.write_csv(out / "preferences.csv", [{"영역": kind, "preference": "", "reason": ""} for kind in KINDS.values()], ("영역", "preference", "reason"))
    atomic_write(out / "evaluator.txt", "본인\n".encode())
    report(out)
    return manifest


def resolved_style(shop, profile, refs):
    with sqlite3.connect(":memory:") as con:
        con.row_factory = sqlite3.Row
        con.executescript((ROOT / "app/schema.sql").read_text())
        con.execute("INSERT INTO accounts(id,name,description,color,created_at) VALUES (?,?,?,?,?)",
                    ("experiment", shop["name"], shop["description"], shop["color"], 0))
        styles.save_profile(con, "experiment", profile | {"locked_fields": []}, refs, "confirmed")
        return styles.resolve(con, "experiment", "keep")


def prepare_content(out, manifest):
    stage = stage_manifest(out, manifest)
    snapshots = {}
    for sample in manifest["style_slots"]:
        name = "../styles/samples/" + sample["id"] + "/output.json"
        raw = (out / "content" / name).read_bytes()
        profile = mc.Style.model_validate_json(raw).model_dump()
        stage["files_sha256"][name] = mc.digest(raw)
        snapshots[sample["group"]] = resolved_style(manifest["shop"], profile, [r["id"] for r in manifest["references"]])
    stage["resolved_styles"] = snapshots
    cfg = manifest["config"]
    cases = {c["id"]: c for c in manifest["cases"]}
    schedule = manifest["samples"].copy()
    secrets.SystemRandom().shuffle(schedule)
    for sample in schedule:
        style = snapshots[sample["group"]]
        photo = (out / cases[sample["case"]]["image"]).read_bytes()
        settings = cfg["models"][sample["model"]]
        if sample["kind"] == "text":
            data = {"task": "confirmed_product_only", "shop": manifest["shop"]["name"], "confirmed_style": style["summary"],
                "resolved_visual_style": style, "past_post_examples": [{"id": r["id"], "caption": r["caption"][:2200]} for r in manifest["references"]],
                "show_price": False, "confirmed_product": cfg["product"], "caption_requirements": cfg["caption_requirements"],
                "caption_format": "제품명으로 시작하는 2~4개 짧은 문단, 부드러운 존댓말"}
            payload = text_payload(sample["model"], data, mc.Draft, [photo], settings)
        else:
            payload = mc.edit_payload(sample["model"], photo, mc.edit_prompt(cfg["product"]["product_name"], style) + "\n" + cfg["main_product"], settings["quality"], settings["size"])
        name = "requests/" + sample["id"] + ".json"
        save_input(out / "content", stage, name, json.dumps(payload, ensure_ascii=False).encode())
        stage["samples"].append(sample | {"request": name})
    freeze(out / "content", stage)


def state(out, stage, sample):
    path = mc.record_path(out / stage, sample)
    return mc.read_json(path) if path.exists() else {"state": "planned", "cost_microusd": None}


def run(out, settings, client):
    manifest = mc.frozen_manifest(out)
    report(out)  # 잘못된 평가표·고정 의존 관계도 유료 호출 전에 검증한다.
    count = mc.run(out / "styles", settings, client, reporter=lambda _: None)
    if any(state(out, "styles", s)["state"] != "completed" for s in manifest["style_slots"]):
        report(out)
        print("두 스타일 분석이 완성되지 않아 후속 제작을 시작하지 않습니다", flush=True)
        return count
    if not (out / "content/manifest.json").exists():
        for name in ("content", "content/requests", "content/samples"):
            (out / name).mkdir(exist_ok=True)
        prepare_content(out, manifest)
    count += mc.run(out / "content", settings, client, reporter=lambda _: None)
    report(out)
    return count


def metrics(records):
    attempted = [r for r in records if r["state"] != "planned"]
    known = sum(r["cost_microusd"] for r in attempted if r.get("cost_microusd") is not None)
    unknown = sum(r.get("cost_microusd") is None for r in attempted)
    times = [r["api_seconds"] for r in attempted if "api_seconds" in r]
    bases = {r.get("cost_basis", "unknown") for r in attempted}
    return {"planned": len(records), "completed": sum(r["state"] == "completed" for r in records),
        "attempted": len(attempted), "known_cost_usd": str(Decimal(known) / 1_000_000), "unknown_cost_count": unknown,
        "api_median_seconds": statistics.median(times) if times else "", "api_sum_seconds": sum(times) if times else "",
        "cost_basis": next(iter(bases)) if len(bases) == 1 else "mixed" if bases else "not_measured",
        "browser_user_wait_seconds": ""}


def report(out):
    manifest = mc.frozen_manifest(out)
    if manifest.get("suite") != "style_pipeline":
        raise ValueError("인스타 스타일 경로 비교 폴더를 지정하세요")
    mc.frozen_manifest(out / "styles")
    if (out / "content/manifest.json").exists():
        mc.frozen_manifest(out / "content")
    evaluator = (out / "evaluator.txt").read_text().strip()
    if not evaluator:
        raise ValueError("평가자 이름이 필요합니다")
    samples = manifest["samples"]
    reviews = mc.reviewed_rows(out / "reviews.csv", ("sample_id",), {(s["id"],) for s in samples}, evaluator=evaluator,
        criteria={(s["id"],): CRITERIA[s["kind"]] for s in samples}, fields=REVIEW_FIELDS)
    bundle_keys = {(k, g) for k in KINDS.values() for g in manifest["groups"]}
    bundles = mc.reviewed_rows(out / "bundles.csv", ("영역", "그룹"), bundle_keys, value_column=BUNDLE_GRADES[0], evaluator=evaluator)
    second = mc.reviewed_rows(out / "bundles.csv", ("영역", "그룹"), bundle_keys, value_column=BUNDLE_GRADES[1], evaluator=evaluator)
    for key in bundles:
        bundles[key][BUNDLE_GRADES[1]] = second[key][BUNDLE_GRADES[1]]
    preferences = mc.reviewed_rows(out / "preferences.csv", ("영역",), {(k,) for k in KINDS.values()}, ac.PREFERENCES, "preference", evaluator=evaluator)
    states = {s["id"]: state(out, "content", s) for s in samples}
    style_states = {s["id"]: state(out, "styles", s) for s in manifest["style_slots"]}
    verdicts = {}
    for s in samples:
        row = reviews[(s["id"],)]
        if (row["영역"], row["그룹"], row["사례"]) != (KINDS[s["kind"]], s["group"], s["case"]):
            raise ValueError("평가표의 ID·영역·그룹·사례는 변경하지 마세요")
        grades = [row[c] for c in CRITERIA[s["kind"]]]
        status = states[s["id"]]["state"]
        verdicts[s["id"]] = ("불합격" if any(g in {"부분 충족", "미충족"} for g in grades) else "합격" if all(g == "충족" for g in grades) else "미평가") if status == "completed" else "생성 전" if status == "planned" else "생성 실패"
    summary, paths = [], []
    for group, analyzer in manifest["groups"].items():
        upstream = [s for s in manifest["style_slots"] if s["group"] == group]
        summary.append({"stage": "스타일 분석", "group": group, "analyzer": analyzer, "generator": analyzer,
            "passed": "", "unreviewed": "", **metrics([style_states[s["id"]] for s in upstream])})
        for kind, label in KINDS.items():
            selected = [s for s in samples if s["group"] == group and s["kind"] == kind]
            summary.append({"stage": label, "group": group, "analyzer": analyzer, "generator": selected[0]["model"],
                "passed": sum(verdicts[s["id"]] == "합격" for s in selected),
                "unreviewed": sum(verdicts[s["id"]] == "미평가" for s in selected), **metrics([states[s["id"]] for s in selected])})
        selected = [s for s in samples if s["group"] == group]
        records = [style_states[s["id"]] for s in upstream] + [states[s["id"]] for s in selected]
        totals = metrics(records)
        paired = sum(all(verdicts[s["id"]] == "합격" for s in selected if s["case"] == c["id"]) for c in manifest["cases"])
        complete_cost = not totals["unknown_cost_count"] and all(r.get("request_started_at") for r in records)
        judged = all(verdicts[s["id"]] in {"합격", "불합격", "생성 실패"} for s in selected)
        paths.append({"group": group, "analyzer": analyzer, "passed_post_pairs": paired, **totals,
            "cost_per_passed_post_usd": str(Decimal(totals["known_cost_usd"]) / paired) if paired and complete_cost and judged else "",
            **{label + "_" + criterion: bundles[(label, group)][criterion] for label in KINDS.values() for criterion in BUNDLE_GRADES}})
    mc.write_csv(out / "summary.csv", summary, tuple(summary[0]))
    mc.write_csv(out / "paths.csv", paths, tuple(paths[0]))
    mc.save_json(out / "results.json", {"summary": summary, "paths": paths, "evaluator": evaluator,
        "attempts": [{**s, **states[s["id"]], "verdict": verdicts[s["id"]]} for s in samples],
        "human_reviews": list(reviews.values()), "human_bundles": list(bundles.values()), "human_preferences": list(preferences.values()),
        "evidence_scope": "한 인스타 피드 8개, 모델별 분석 1회, 같은 상품 입력 5개(원본1·합성4)의 후속 제작. 이미지 무작위성·반복 안정성·다른 가게에 일반화 불가",
        "cost_note": "이전 잘못 설계한 실험 비용은 별도 보존. 이번 경로 비용은 분석1+문구5+이미지5. 추정 비용이며 브라우저 대기 시간은 미측정"})
    render_review(out, manifest, states, verdicts, reviews)
    return summary


def render_review(out, manifest, states, verdicts, reviews):
    esc = lambda v: html.escape(str(v))
    sections = ['<h2>스타일 분석에 사용한 기존 인스타 게시물 8개</h2><div class="feed">']
    for r in manifest["references"]:
        sections.append('<article><a href="' + esc(r["permalink"]) + '"><img alt="기존 게시물" src="' + esc(r["image"]) + '"></a><pre>' + esc(r["caption"]) + '</pre></article>')
    sections.append('</div><details><summary>추출한 스타일 A/B 확인</summary><div class="pair">')
    for s in manifest["style_slots"]:
        path = out / "styles/samples" / s["id"] / "output.json"
        value = ac.display_output("style", mc.read_json(path)) if path.exists() else "아직 완성된 분석 없음"
        sections.append('<article><h3>' + esc(s["group"]) + '</h3><pre>' + esc(value) + '</pre></article>')
    sections.append('</div></details><h2>최종 문구·이미지 비교</h2>')
    for case in manifest["cases"]:
        sections.append('<section><h3>' + esc(case["id"]) + '</h3><details><summary>공통 상품 입력 (' + esc(case["provenance"]) + ')</summary><img class="input" src="' + esc(case["image"]) + '"></details><div class="pair">')
        for group in manifest["groups"]:
            sections.append('<article><h3>경로 ' + group + '</h3>')
            for kind in ("image", "text"):
                s = next(s for s in manifest["samples"] if (s["case"], s["group"], s["kind"]) == (case["id"], group, kind))
                folder = Path("content/samples") / s["id"]
                if kind == "image" and (out / folder / "preview.jpg").exists():
                    sections.append('<img alt="최종 미리보기" src="' + str(folder / "preview.jpg") + '"><p><a href="' + str(folder / "generated-original.png") + '">생성 원본</a> · 1080×1350</p>')
                elif kind == "text" and (out / folder / "caption.txt").exists():
                    sections.append('<pre>' + esc((out / folder / "caption.txt").read_text()) + '</pre>')
                checks = " · ".join(c + ": " + (reviews[(s["id"],)][c] or "미평가") for c in CRITERIA[kind])
                sections.append('<p>' + KINDS[kind] + ' ID: ' + s["id"] + ' / ' + esc(states[s["id"]]["state"]) + ' / ' + verdicts[s["id"]] + '</p><small>' + esc(checks) + '</small>')
            sections.append('</article>')
        sections.append('</div></section>')
    page = ('<!doctype html><html lang="ko"><meta charset="utf-8"><title>인스타 스타일 경로 A/B 비교</title>'
        '<style>body{font:16px system-ui;max-width:1200px;margin:32px auto;padding:0 20px;background:#f8f4ee;color:#39291f}.pair,.feed{display:grid;grid-template-columns:1fr 1fr;gap:20px}.feed{grid-template-columns:repeat(4,1fr)}article{background:white;border-radius:12px;padding:18px;min-width:0}img{width:100%;height:auto}img.input{width:240px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}small{line-height:1.7}section,details{margin:30px 0}.feed pre{max-height:240px;overflow:auto}@media(max-width:700px){.pair{grid-template-columns:1fr}.feed{grid-template-columns:1fr 1fr}}</style>'
        '<h1>기존 인스타 스타일 → 최종 제작 A/B 비교</h1><p>같은 기존 게시물에서 스타일을 분석했습니다. 두 경로의 문구·이미지 생성 모델은 같습니다. A/B는 분석부터 최종 결과까지 같은 경로이며, 분석 모델명·비용은 가렸습니다.</p>'
        '<p><a href="protocol.md">실험 범위·기준</a> · <a href="reviews.csv">개별 평가 20행</a> · <a href="bundles.csv">묶음 평가 4행</a> · <a href="preferences.csv">선호 평가 2행</a></p>'
        '<p>개별 결과는 해당 영역의 한글 기준만 평가하세요. 등급: 충족/부분 충족/미충족. 부분 충족은 불합격입니다. 문제가 있는 결과와 선호 판정에는 이유를 적으세요. 묶음은 기존 게시물 일치와 결과 간 일관성을 각각 평가합니다. 생성 완료는 품질 합격이 아닙니다.</p>'
        + ''.join(sections) + '</html>')
    atomic_write(out / "review.html", page.encode())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("prepare")
    p.add_argument("--references", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    for command in ("run", "report", "settle"):
        p = commands.add_parser(command)
        p.add_argument("out", type=Path)
        if command == "run":
            p.add_argument("--execute", action="store_true")
        if command == "settle":
            p.add_argument("--stage", choices=("styles", "content"), required=True)
            p.add_argument("--sample", required=True)
            p.add_argument("--cost-usd", required=True)
            p.add_argument("--note", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            prepare(args.references, args.out)
            print("인스타 스타일 분석 2건·후속 제작 20건 준비 완료: " + str(args.out.resolve()))
            return
        out = args.out.resolve()
        with (out / ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            manifest = mc.frozen_manifest(out)
            if manifest.get("suite") != "style_pipeline":
                raise ValueError("인스타 스타일 경로 비교 폴더를 지정하세요")
            if args.command == "report":
                report(out)
                print("사람 판정 집계 완료: " + str(out / "paths.csv"))
            elif args.command == "settle":
                mc.settle(out / args.stage, args.sample, args.cost_usd, args.note, Settings.from_env(), reporter=lambda _: None)
                report(out)
            elif not args.execute:
                print("유료 호출 없음. 남은 생성 계획 " + str(sum(not mc.record_path(out / stage, s).exists() for stage, slots in (("styles", manifest["style_slots"]), ("content", manifest["samples"])) for s in slots)))
            else:
                with httpx.Client(transport=httpx.HTTPTransport(retries=0), timeout=httpx.Timeout(180, connect=10)) as client:
                    run(out, Settings.from_env(), client)
    except (ValueError, OSError, KeyError, StopIteration, HTTPException) as exc:
        parser.exit(1, (str(exc.detail) if isinstance(exc, HTTPException) else str(exc)) + "\n")


if __name__ == "__main__":
    main()
