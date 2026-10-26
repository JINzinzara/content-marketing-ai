"""서비스와 분리된 CLI 평가. 소스 함수를 직접 실행하고 결과 파일을 남긴다."""

import argparse
import copy
import csv
import hashlib
import io
from importlib.metadata import version
import json
import math
import platform
import re
import subprocess
import time
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import HTTPException
from PIL import Image

from app import analytics, styles
from app.config import ROOT, Settings
from app.rendering import atomic_write
from . import lineup
from .measurement import MeasuredTextAI, fill_blanks

PROTOCOL_VERSION = "evaluation-1.1.0-cli"
ASSETS = Path(__file__).parent / "fixtures"
DEFAULT_RESULTS = Path(__file__).parent / "results"
BASELINE_INSTRUCTIONS = """사용자가 직접 입력한 사진·상품 정보·요청에 따라 한국어 Instagram 문구를 작성한다.
입력에 없는 상품 사실·가격·효능은 만들지 않는다. 요청한 JSON 형식으로 반환한다.
layout은 split으로 한다. 제목은 관리용이며 본문·해시태그를 별도로 작성한다."""
VERSIONS = {
    "baseline-v1": {
        "role": "control", "label": "기본 제작 방식",
        "notice": "저장한 공지 서식을 직접 수정한 뒤 공통 렌더러로 출력",
        "promotion": "사진과 상품 정보를 직접 입력하고 같은 GPT API로 생성; 저장 스타일 자동 주입 없음",
        "version": "1.0.0",
    },
    "service-v1": {
        "role": "treatment", "label": "서비스 제작 방식",
        "notice": "저장 메뉴 선택·품절 반영·날짜 입력 후 공통 렌더러로 출력",
        "promotion": "저장 상품 정보·확정 스타일을 재사용하고 같은 GPT API로 생성",
        "version": "1.0.0",
    },
}


def load_dataset(name):
    return json.loads((ASSETS / name).read_text())


def sha(value):
    raw = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(raw).hexdigest()


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def manifest(settings):
    """설정은 허용 목록만 기록한다. 환경 변수·토큰·.env·개인 DB를 복사하지 않는다."""
    source_files = sorted(p for p in (ROOT / "app").rglob("*")
                          if p.is_file() and p.suffix in {".py", ".html", ".css", ".js", ".json", ".sql", ".ttf", ".otf", ".txt"})
    source_files += [ROOT / "pyproject.toml", *sorted((ROOT / "tests").glob("*.py")),
                     *sorted(Path(__file__).parent.glob("*.py")), *sorted(ASSETS.glob("*.json"))]
    contents = {str(p.relative_to(ROOT)): p.read_bytes() for p in source_files}
    hashes = {name: sha(raw) for name, raw in contents.items()}
    fingerprint = sha(hashes)
    folder = Path(getattr(settings, "evaluation_dir", DEFAULT_RESULTS)) / "snapshots"
    folder.mkdir(parents=True, exist_ok=True)
    archive = folder / (fingerprint + ".zip")
    if not archive.exists():
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zipped:
            for name, raw in contents.items():
                zipped.writestr(name, raw)
        atomic_write(archive, buffer.getvalue())
    try:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, stderr=subprocess.DEVNULL, text=True).strip()
    except (subprocess.SubprocessError, OSError):
        commit = None
    return {
        "protocol_version": PROTOCOL_VERSION, "created_at": utc_now(),
        "code_sha256": fingerprint, "source_files": hashes,
        "source_archive": "snapshots/" + archive.name, "git_commit": commit,
        "python": platform.python_version(), "platform": platform.system(),
        "dependencies": {name: version(name) for name in ("fastapi", "httpx", "pydantic", "Pillow")},
        "variants": copy.deepcopy(VERSIONS),
        "tasks_sha256": sha((ASSETS / "tasks-v1.json").read_bytes()),
        "checklist_sha256": sha((ASSETS / "checklist-v1.json").read_bytes()),
        "settings": {"text_model": settings.openai_model, "image_model": settings.image_model,
                     "image_quality": settings.image_quality, "max_output_tokens": settings.max_output_tokens,
                     "input_ceiling_rate": str(settings.input_ceiling_rate), "output_rate": str(settings.output_rate),
                     "comparison_hours": settings.comparison_hours,
                     "comparison_tolerance_hours": settings.comparison_tolerance_hours},
        "measurement_limits": ["자동 실행은 사람의 작업 시간을 측정하지 않습니다",
                               "규칙 기반 브리핑 검사와 실제 GPT·사람 평가는 분리합니다",
                               "원본 사진 합성과 AI 이미지 편집의 시간은 합산 비교하지 않습니다"],
    }


class Store:
    def __init__(self, settings):
        self.settings = settings
        self.root = Path(getattr(settings, "evaluation_dir", DEFAULT_RESULTS))
        self.root.mkdir(parents=True, exist_ok=True)

    def run_path(self, owner, identifier):
        if not re.fullmatch(r"[a-f0-9]{32}", identifier):
            raise ValueError("실험 ID를 확인해주세요")
        return self.root / "runs" / sha(owner) / (identifier + ".json")

    def save(self, owner, run):
        path = self.run_path(owner, run["id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, json.dumps(run, ensure_ascii=False, indent=2, allow_nan=False).encode())

    def create(self, owner, kind, variant, *, task=None, operator="automated", frozen_manifest=None):
        if variant not in VERSIONS or operator not in {"automated", "human", "agent"}:
            raise ValueError("실험 방식과 수행자를 확인해주세요")
        run = {
            "id": uuid.uuid4().hex, "kind": kind, "variant": variant,
            "variant_definition": VERSIONS[variant], "operator": operator,
            "state": "running", "started_at": utc_now(), "finished_at": None,
            "manifest": copy.deepcopy(frozen_manifest) if frozen_manifest else manifest(self.settings), "task": task,
            "results": [], "attempts": [], "events": [], "reviews": [],
            "human_task_ms": None, "human_review": None,
        }
        self.save(owner, run)
        return run

    def get(self, owner, identifier):
        return json.loads(self.run_path(owner, identifier).read_text())

    def update(self, owner, identifier, mutate):
        value = self.get(owner, identifier)
        mutate(value)
        self.save(owner, value)
        return value

    def list(self, owner):
        paths = (self.root / "runs" / sha(owner)).glob("*.json")
        return sorted((json.loads(path.read_text()) for path in paths), key=lambda value: value["started_at"], reverse=True)

    def export(self, owner, identifier):
        run = self.get(owner, identifier)
        folder = self.root / "exports" / identifier
        folder.mkdir(parents=True, exist_ok=True)
        atomic_write(folder / "run.json", json.dumps(run, ensure_ascii=False, indent=2, allow_nan=False).encode())
        fields = ["run_id", "variant", "code_sha256", "data_source", "id", "category", "check_type",
                  "passed", "execution_ms", "human_task_ms", "model", "error", "notes"]
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for result in run["results"]:
            row = {"run_id": identifier, "variant": run["variant"], "code_sha256": run["manifest"]["code_sha256"],
                   "human_task_ms": run["human_task_ms"], **{key: result.get(key) for key in fields if key in result},
                   "execution_ms": result.get("execution_ms", result.get("server_ms"))}
            for key, value in row.items():
                if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
                    row[key] = "'" + value
            writer.writerow(row)
        atomic_write(folder / "metrics.csv", output.getvalue().encode("utf-8-sig"))
        attempts = io.StringIO(newline="")
        attempt_fields = ["run_id", "variant", "attempt_id", "execution", "execution_ms",
                          "generation_api_ms", "preflight_api_ms", "model", "api_call_count", "error"]
        attempt_writer = csv.DictWriter(attempts, fieldnames=attempt_fields)
        attempt_writer.writeheader()
        for attempt in run["attempts"]:
            timing = attempt.get("api_timing", {})
            row = {"run_id": identifier, "variant": run["variant"], "attempt_id": attempt["id"],
                **{key: attempt.get(key) for key in ("execution", "model", "error")},
                "execution_ms": attempt.get("execution_ms", attempt.get("server_ms")),
                "generation_api_ms": timing.get("generation_api_ms"), "preflight_api_ms": timing.get("preflight_api_ms"),
                "api_call_count": len(attempt.get("api_calls", []))}
            attempt_writer.writerow({key: "'" + value if isinstance(value, str) and value.startswith(("=", "+", "-", "@")) else value
                                     for key, value in row.items()})
        atomic_write(folder / "attempts.csv", attempts.getvalue().encode("utf-8-sig"))
        atomic_write(folder / "summary.md", summary_markdown(run).encode())
        return folder


def summary_markdown(run):
    groups = {}
    for result in run["results"]:
        group = groups.setdefault(result["category"], [])
        if isinstance(result.get("passed"), bool):
            group.append(result["passed"])
    lines = ["# 실험 결과", "", f'- 실행: {run["id"]}', f'- 방식: {run["variant"]}',
             f'- 상태: {run["state"]}', f'- 코드: {run["manifest"]["code_sha256"]}', ""]
    for category, values in groups.items():
        lines.append(f'- {category}: {sum(values)}/{len(values)} 통과' if values else f'- {category}: 판정 대기')
    lines += ["", f'- 사람의 전체 제작 시간(ms): {run["human_task_ms"] if run["human_task_ms"] is not None else "미측정"}',
              f'- 사람 검수: {"기록 있음" if run["human_review"] is not None else "미실행"}',
              "- 함수 실행 시간으로 사람의 전체 제작 시간이나 GPT 이미지 편집 시간을 대신하지 않습니다.",
              "- 합성 데이터의 규칙 기반 브리핑 검사는 실제 GPT 브리핑 품질·마케팅 성과를 입증하지 않습니다.",
              "- 잘못된 사실과 스타일 적합성은 사람 검수 전에는 통과로 확정하지 않습니다."]
    review = run.get("human_review") or {}
    if review.get("metrics"):
        lines += ["", "## 사람 검수 수치", ""]
        lines += [f"- {key}: {value if value is not None else '미측정'}" for key, value in run["human_review"]["metrics"].items()]
    return "\n".join(lines) + "\n"


def review_spec(run):
    if run["kind"] != "live_api" or not run["attempts"] or not run["attempts"][-1].get("output"):
        raise HTTPException(422, "응답이 있는 실제 API 실험만 검수할 수 있습니다")
    kind = run["task"]["kind"]
    if kind == "style":
        return styles.FIELDS, {
            "unreviewed": "미검수", "correct_fill": "관찰 가능 · 올바르게 채움",
            "missed_fill": "관찰 가능 · 누락", "wrong_fill": "관찰 가능 · 잘못 채움",
            "correct_blank": "관찰 불가 · 적절히 비움", "unsupported_fill": "관찰 불가 · 근거 없이 채움"}
    items = ({"facts": "확인된 사실 일치", "purpose": "목적 적합", "style": "스타일 적합"}
             if kind == "promotion" else {"numbers": "수치 일치", "evidence": "근거 게시물 일치",
             "hypothesis": "이유와 가설 구분", "no_causal_claim": "인과·매출 효과 단정 없음", "actionable": "다음 행동 실행 가능"})
    return items, {"unreviewed": "미검수", "pass": "통과", "fail": "실패"}


def record_review(run, reviewer, role, ratings, notes):
    items, options = review_spec(run)
    if not reviewer.strip() or len(reviewer) > 40 or role not in {"developer", "peer", "instructor"}:
        raise HTTPException(422, "평가자 번호와 관계를 확인해주세요")
    if set(ratings) != set(items) or any(value not in options for value in ratings.values()) or not notes.strip():
        raise HTTPException(422, "모든 항목의 판정과 근거를 기록해주세요")
    values = list(ratings.values())
    metrics = {"reviewed_items": sum(value != "unreviewed" for value in values), "total_items": len(values)}
    if run["task"]["kind"] == "style":
        denominator = sum(value in {"correct_fill", "missed_fill", "wrong_fill"} for value in values)
        metrics.update(correctly_filled=values.count("correct_fill"), assessable_fields=denominator,
                       fill_accuracy=values.count("correct_fill") / denominator if denominator else None,
                       unsupported_fills=values.count("unsupported_fill"), missed_fills=values.count("missed_fill"),
                       wrong_fills=values.count("wrong_fill"))
    else:
        metrics.update(passed_items=values.count("pass"), failed_items=values.count("fail"))
    review = {"at": utc_now(), "reviewer": reviewer.strip(), "role": role,
              "ratings": ratings, "notes": notes[:4000], "metrics": metrics, "method": "human_rubric"}
    run["reviews"].append(review)
    run["human_review"] = review
    return review


def fixture_rows(specs):
    result = []
    published = datetime(2026, 10, 19, tzinfo=timezone.utc)
    for item in specs:
        values = {"reach": item.get("reach", 100), "saved": item.get("saved", 10), "shares": item.get("shares", 1)}
        result.append({
            "media_id": item["id"], "layout": "split", "purpose": item.get("purpose", "상품 소개"),
            "promotion": item.get("promotion", "organic"), "product_name": "평가용 상품",
            "media_product_type": item.get("format", "FEED"),
            "published_at": published.isoformat(),
            "observed_at": (published + timedelta(hours=item.get("elapsed", 24))).isoformat(),
            "metrics": {key: {"value": value, "status": item.get(key + "_status", "available")}
                        for key, value in values.items()},
        })
    return result


def run_check(settings, case):
    original = fixture_rows(case["rows"])
    before = analytics.analyze(original, settings)
    specs = copy.deepcopy(case["rows"])
    mutation = case.get("mutation")
    if mutation == "reverse":
        specs.reverse()
    elif mutation == "add_lineup":
        specs += [{"id": "excluded-1", "purpose": "라인업"}, {"id": "excluded-2", "purpose": "라인업"}]
    elif mutation == "scale":
        for row in specs:
            row["reach"] *= 10
            row["saved"] *= 10
            row["shares"] = row.get("shares", 1) * 10
    elif mutation == "increase_saved":
        specs[0]["saved"] = 20
    elif mutation == "remove_reach":
        specs[0]["reach"] = None
    after = analytics.analyze(fixture_rows(specs), settings) if mutation else before
    rates = {row["media_id"]: row["save_rate"] for row in after["latest"]}
    checks = []
    for key, expected in case.get("expected_rates", {}).items():
        actual = rates.get(key)
        checks.append({"name": key + ":save_rate", "expected": expected, "actual": actual,
                       "passed": actual is None if expected is None else actual is not None and math.isclose(actual, expected)})
    if "expected_evidence" in case:
        actual = sorted(row["media_id"] for row in after["evidence"])
        checks.append({"name": "evidence_ids", "expected": sorted(case["expected_evidence"]), "actual": actual,
                       "passed": actual == sorted(case["expected_evidence"])})
    if case["type"] == "INV":
        checks.append({"name": "evidence_and_briefing_invariant", "passed": before["evidence"] == after["evidence"] and before["briefing"] == after["briefing"]})
    if mutation == "increase_saved":
        checks.append({"name": "rate_increases", "expected": case["expected_changed_rate"], "actual": rates["a"],
                       "passed": rates["a"] == case["expected_changed_rate"] and rates["a"] > next(row["save_rate"] for row in before["latest"] if row["media_id"] == "a")})
    if mutation == "remove_reach":
        checks.append({"name": "withdraw_comparison", "passed": not after["evidence"] and not after["briefing"]["cards"]})
    for card in after["briefing"]["cards"]:
        for identifier, rate in card["rates"].items():
            checks.append({"name": identifier + ":briefing_numeric_consistency", "passed":
                           f"{identifier}: 저장률 {rate * 100:.4g}%" in card["observation"]})
    return {"id": case["id"], "category": "G3:" + case["type"], "check_type": case["type"],
            "data_source": "synthetic", "execution": "deterministic_pipeline", "model": None,
            "input": original, "mutated_input": fixture_rows(specs), "expected": case,
            "output": after, "before": before if mutation else None, "checks": checks,
            "passed": bool(checks) and all(row["passed"] for row in checks),
            "human_review": None, "llm_evaluation": None}


def run_automated(settings, owner="local-owner"):
    store = Store(settings)
    run = store.create(owner, "automated", "service-v1")
    results = []
    fixture_settings = copy.copy(settings)
    fixture_settings.comparison_hours, fixture_settings.comparison_tolerance_hours = 24, 2
    for case in load_dataset("checklist-v1.json")["cases"]:
        start = time.perf_counter()
        try:
            result = run_check(fixture_settings, case)
        except Exception as error:
            result = {"id": case["id"], "category": "G3:" + case["type"], "data_source": "synthetic",
                      "passed": False, "error": type(error).__name__}
        result["execution_ms"] = (time.perf_counter() - start) * 1000
        results.append(result)
    tasks = load_dataset("tasks-v1.json")
    for task in tasks["tasks"]:
        if task["kind"] != "notice":
            results.append({"id": task["id"], "category": "G1:live_generation", "data_source": "synthetic",
                            "passed": None, "execution_ms": None, "model": settings.openai_model,
                            "notes": "미실행: 실제 API·사진 입력 및 사람의 과제 수행은 별도 세션 필요"})
            continue
        start = time.perf_counter()
        text = lineup.compose(tasks["menus"], task["selected"], task["sold_out"], tasks["day"], tasks["shop"])
        image = lineup.render_notice(text, task["format"])
        elapsed = (time.perf_counter() - start) * 1000
        image_path = store.root / (run["id"] + "-" + task["id"] + ".jpg")
        atomic_write(image_path, image)
        expected_size = (1080, 1920) if task["format"] == "STORY" else (1080, 1350)
        size = Image.open(io.BytesIO(image)).size
        expected_names = [tasks["menus"][identifier] for identifier in task["selected"]]
        passed = all(name in text for name in expected_names) and size == expected_size
        passed = passed and all(name not in text for identifier, name in tasks["menus"].items() if identifier not in task["selected"])
        passed = passed and all((tasks["menus"][identifier] + " · 품절") in text for identifier in task["sold_out"])
        results.append({"id": task["id"], "category": "G1:notice_renderer", "data_source": "synthetic",
                        "execution": "local_render_only", "passed": passed, "execution_ms": elapsed,
                        "input": task, "output": {"text": text, "dimensions": size, "image_file": image_path.name},
                        "target_met": None, "model": None, "human_review": None})
    style_cases = [
        ("fill-empty", {"voice": "존댓말"}, {"palette": "따뜻한 베이지", "voice": "반말"}, {"palette": "따뜻한 베이지", "voice": "존댓말"}),
        ("keep-existing", {key: "기존 값" for key in styles.FIELDS}, {key: "새 값" for key in styles.FIELDS}, {key: "기존 값" for key in styles.FIELDS}),
        ("unknown-remains-empty", {}, {}, {key: "" for key in styles.FIELDS}),
    ]
    for identifier, existing, proposal, expected in style_cases:
        actual = fill_blanks(existing, proposal)
        results.append({"id": identifier, "category": "G2:merge_rules", "data_source": "synthetic",
                        "input": {"existing": existing, "proposal": proposal}, "expected": expected, "output": actual,
                        "passed": all(actual[key] == value for key, value in expected.items()),
                        "model": None, "notes": "빈칸 병합 규칙 검사. AI 스타일 추출·사람의 적합성 평가는 미실행"})
    def finish(value):
        value.update(results=results, state="completed", finished_at=utc_now())
        value["fixture_settings"] = {"comparison_hours": 24, "tolerance_hours": 2, "source": "fixture_contract_not_optimal_values"}
    store.update(owner, run["id"], finish)
    folder = store.export(owner, run["id"])
    return store.get(owner, run["id"]), folder


def run_live(settings, owner="local-owner"):
    """두 방식에 같은 모델·자료를 사용한 실제 API 실행. 사람 작업 시간은 기록하지 않는다."""
    import base64
    import httpx
    from app.db import connect, ensure_account
    from app.text_ai import Draft, Recommendation, validate_draft
    dataset = load_dataset("tasks-v1.json")
    store = Store(settings)
    frozen_manifest = manifest(settings)
    buffer = io.BytesIO()
    Image.new("RGB", (640, 480), "#b6c4b0").save(buffer, "JPEG")
    photo = buffer.getvalue()
    image = "data:image/jpeg;base64," + base64.b64encode(photo).decode()
    with connect(settings, write=True) as con:
        ensure_account(con, owner)
    runs = []
    with httpx.Client(timeout=httpx.Timeout(120, connect=10), follow_redirects=False) as client:
        jobs = []
        for task in dataset["tasks"]:
            if task["kind"] != "promotion":
                continue
            prompt = json.dumps({"상품 정보": task["facts"], "스타일 요청": dataset["saved_style"]}, ensure_ascii=False)
            jobs += [("baseline-v1", task, {"task": "basic_draft", "user_request": prompt}, Draft, BASELINE_INSTRUCTIONS, [image]),
                     ("service-v1", task, {"task": "confirmed_product_only", "shop": dataset["shop"],
                      "confirmed_style": dataset["saved_style"], "confirmed_product": task["facts"], "show_price": True}, Draft, None, [image])]
        style_task = {"id": "style-extraction", "label": "합성 참고자료의 스타일 추출", "kind": "style"}
        style_data = {"task": "style_analysis", "shop_description": "평가용 가상 카페",
                      "reference_notes": "확인 가능한 특징만 기록", "past_post_examples": [
                          {"id": "ref-1", "caption": "오늘도 편안한 하루 보내세요. 준비한 음료와 함께 기다리겠습니다.", "image_index": 1},
                          {"id": "ref-2", "caption": "가을 한정 메뉴를 준비했습니다. 매장에서 만나보세요.", "image_index": None}]}
        jobs.append(("service-v1", style_task, style_data, styles.Style, None, [image]))
        fixture_settings = copy.copy(settings)
        fixture_settings.comparison_hours, fixture_settings.comparison_tolerance_hours = 24, 2
        report_data = analytics.analyze(fixture_rows(load_dataset("checklist-v1.json")["cases"][0]["rows"]), fixture_settings)
        jobs.append(("service-v1", {"id": "insight-briefing", "label": "합성 인사이트의 실제 GPT 브리핑", "kind": "briefing"},
                     {"observed_comparisons": report_data["evidence"], "limitations": report_data["limitations"]}, Recommendation, None, []))
        for variant, task, data, schema, instructions, images in jobs:
            run = store.create(owner, "live_api", variant, task={**task, "dataset": dataset}, operator="automated", frozen_manifest=frozen_manifest)
            identifier = run["id"]
            attempt = {"id": "1", "at": utc_now(), "execution": "live_api", "input": data,
                       "model": settings.openai_model, "photo_source": "synthetic_placeholder_not_product_photo",
                       "photo_sha256": sha(photo), "api_timing": {}, "trace": {}, "output": None, "error": None}
            atomic_write(store.root / (identifier + "-original.jpg"), photo)
            started = time.perf_counter()
            purpose = "experiment:" + identifier
            try:
                ai = MeasuredTextAI(settings, client, instructions=instructions,
                                    timing=attempt["api_timing"], trace=attempt["trace"])
                result = ai.generate(owner, purpose, data, schema, images=images)
                attempt["output"] = result.model_dump()
                if schema is Draft:
                    try:
                        validate_draft(result, task["facts"])
                        attempt["fact_guard_passed"] = True
                    except HTTPException as error:
                        attempt["fact_guard_passed"] = False
                        attempt["validation_error"] = str(error.detail)
            except HTTPException as error:
                attempt["error"] = str(error.detail)
            except Exception as error:
                attempt["error"] = "처리 실패: " + type(error).__name__
            attempt["execution_ms"] = (time.perf_counter() - started) * 1000
            with connect(settings) as con:
                attempt["api_calls"] = [dict(row) for row in con.execute(
                    "SELECT id, model, state, input_tokens, output_tokens, response_id, reserved_microusd, cost_microusd FROM ai_calls WHERE owner_id=? AND purpose=?", (owner, purpose))]
            category = {"promotion": "G1:live_text_only", "style": "G2:live_extraction", "briefing": "G3:live_briefing"}[task["kind"]]
            def finish(value):
                value.update(state="completed" if attempt["output"] else "blocked", finished_at=utc_now(), attempts=[attempt])
                value["results"] = [{"id": task["id"], "category": category, "data_source": "synthetic_input_real_api",
                    "passed": None, "execution_ms": attempt["execution_ms"], "model": settings.openai_model, "error": attempt["error"],
                    "notes": "실제 API 처리 시간과 원본 응답 기록. 사람 검수·사진 편집·전체 사용자 작업 시간은 미평가"}]
            store.update(owner, identifier, finish)
            folder = store.export(owner, identifier)
            runs.append({"id": identifier, "variant": variant, "task": task["id"], "output_received": attempt["output"] is not None,
                         "execution_ms": attempt["execution_ms"], "cost_microusd": (sum(c["cost_microusd"] for c in attempt["api_calls"])
                         if attempt["api_calls"] and all(c["cost_microusd"] is not None for c in attempt["api_calls"]) else None), "export": str(folder)})
            print(json.dumps(runs[-1], ensure_ascii=False), flush=True)
    return runs


def paired_metrics(runs):
    groups = {}
    for run in runs:
        if run["kind"] == "task" and run.get("pair_id"):
            groups.setdefault(run["pair_id"], []).append(run)
    result = []
    for pair_id, values in groups.items():
        baseline = [row for row in values if row["variant"] == "baseline-v1"]
        service = [row for row in values if row["variant"] == "service-v1"]
        row = {"pair_id": pair_id, "baseline_ms": None, "service_ms": None, "reduction_percent": None, "status": "pending"}
        if len(baseline) > 1 or len(service) > 1:
            row["status"] = "ambiguous_duplicate_runs"
        elif baseline and service:
            first, second = baseline[0], service[0]
            same = first["manifest"]["settings"] == second["manifest"]["settings"] and first.get("photo_sha256") == second.get("photo_sha256")
            same = same and first["manifest"]["code_sha256"] == second["manifest"]["code_sha256"] and first["manifest"]["tasks_sha256"] == second["manifest"]["tasks_sha256"]
            same = same and first.get("image_mode") == second.get("image_mode")
            complete = all(r["state"] == "completed" and r.get("quality_passed") and r.get("operator") == "human" and r.get("human_task_ms") is not None for r in (first, second))
            row.update(baseline_run=first["id"], service_run=second["id"])
            if not same:
                row["status"] = "different_conditions"
            elif complete and first["human_task_ms"] > 0:
                row.update(baseline_ms=first["human_task_ms"], service_ms=second["human_task_ms"], status="comparable",
                           reduction_percent=(first["human_task_ms"] - second["human_task_ms"]) / first["human_task_ms"] * 100)
        result.append(row)
    return result


def write_report(settings, owner="local-owner"):
    """모든 실행을 보존한 집계. 자동 검사와 유료 호출·사람 평가의 분모를 섞지 않는다."""
    store = Store(settings)
    runs = sorted(store.list(owner), key=lambda value: value["started_at"])
    folder = store.root / "report"
    folder.mkdir(parents=True, exist_ok=True)
    def write_csv(name, fields, rows):
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: "'" + value if isinstance(value, str) and value.startswith(("=", "+", "-", "@")) else value
                             for key, value in row.items() if key in fields})
        atomic_write(folder / name, stream.getvalue().encode("utf-8-sig"))
    api_rows, checks, reviews = [], [], []
    for run in runs:
        for attempt in run["attempts"]:
            if attempt.get("execution") != "live_api":
                continue
            calls, timing = attempt.get("api_calls", []), attempt.get("api_timing", {})
            api_rows.append({"run_id": run["id"], "task": run["task"]["id"], "variant": run["variant"],
                "code_sha256": run["manifest"]["code_sha256"], "attempt_id": attempt["id"],
                "requested_model": attempt.get("model"), "returned_model": timing.get("returned_model"),
                "execution_ms": attempt.get("execution_ms", attempt.get("server_ms")), "generation_api_ms": timing.get("generation_api_ms"),
                "preflight_api_ms": timing.get("preflight_api_ms"), "output_received": bool(attempt.get("output")),
                "fact_guard_passed": attempt.get("fact_guard_passed"), "ledger_call_count": len(calls),
                "input_tokens": sum(c["input_tokens"] for c in calls) if calls and all(c["input_tokens"] is not None for c in calls) else None,
                "output_tokens": sum(c["output_tokens"] for c in calls) if calls and all(c["output_tokens"] is not None for c in calls) else None,
                "ledger_usd": sum(c["cost_microusd"] for c in calls) / 1e6 if calls and all(c["cost_microusd"] is not None for c in calls) else None,
                "photo_source": attempt.get("photo_source"), "error": attempt.get("error")})
        if run["kind"] == "automated":
            checks.extend({"run_id": run["id"], "code_sha256": run["manifest"]["code_sha256"],
                           **{key: result.get(key) for key in ("id", "category", "passed", "error")},
                           "execution_ms": result.get("execution_ms", result.get("server_ms"))}
                          for result in run["results"])
        for index, review in enumerate(run.get("reviews", []), 1):
            for key, value in review.get("metrics", {}).items():
                reviews.append({"run_id": run["id"], "review": index, "reviewer": review["reviewer"],
                                "role": review.get("role"), "metric": key, "value": value, "at": review["at"]})
    write_csv("api-runs.csv", ["run_id", "task", "variant", "code_sha256", "attempt_id", "requested_model", "returned_model",
        "execution_ms", "generation_api_ms", "preflight_api_ms", "output_received", "fact_guard_passed", "ledger_call_count",
        "input_tokens", "output_tokens", "ledger_usd", "photo_source", "error"], api_rows)
    write_csv("automated-checks.csv", ["run_id", "code_sha256", "id", "category", "passed", "execution_ms", "error"], checks)
    write_csv("human-reviews.csv", ["run_id", "review", "reviewer", "role", "metric", "value", "at"], reviews)
    pairs = paired_metrics(runs)
    write_csv("paired-comparison.csv", ["pair_id", "status", "baseline_run", "service_run", "baseline_ms", "service_ms", "reduction_percent"], pairs)
    lines = ["# 실험 실행 집계", "", f"집계 시각: {utc_now()}", "",
        f"- 실제 API 시도: {len(api_rows)}건, 응답 수신 {sum(row['output_received'] for row in api_rows)}건",
        f"- 비용 확인 가능한 호출의 앱 기록액: ${sum(row['ledger_usd'] for row in api_rows if row['ledger_usd'] is not None):.6f}",
        f"- 비용 미확정 시도: {sum(row['ledger_usd'] is None for row in api_rows)}건",
        f"- 사람 검수 수치: {len(reviews)}개 기록 · 비교 가능한 사람 과제 쌍: {sum(row['status'] == 'comparable' for row in pairs)}개", "",
        "## 자동 검사 실행별 결과", ""]
    for run in runs:
        if run["kind"] == "automated":
            lines.append(f"- {run['id']}: 통과 {sum(r.get('passed') is True for r in run['results'])}, 실패 {sum(r.get('passed') is False for r in run['results'])}, 별도 API·사람 평가 대기 {sum(r.get('passed') is None for r in run['results'])} · 코드 {run['manifest']['code_sha256']}")
            for category in sorted(set(r["category"] for r in run["results"])):
                assessed = [r for r in run["results"] if r["category"] == category and isinstance(r.get("passed"), bool)]
                if assessed:
                    lines.append(f"  - {category}: {sum(r['passed'] for r in assessed)}/{len(assessed)} 통과")
    lines += ["", "## 실제 API 시도별 결과", ""]
    for row in api_rows:
        duration = f"{row['generation_api_ms'] / 1000:.3f}초" if row['generation_api_ms'] is not None else "미측정"
        lines.append(f"- {row['task']} / {row['variant']}: 생성 API {duration} · 실행 {row['run_id']}")
    lines += ["", "## 해석 범위", "", "- 모든 실행과 실패를 포함합니다. 버전이 다른 결과는 CSV의 코드 해시로 구분하세요.",
        "- 앱 기록액은 설정된 단가로 계산하며 공급자 청구서 금액과 구분합니다.",
        "- API 응답 수신·제한된 사실 검사는 사람의 품질 통과와 다릅니다.",
        "- 실제 API 파일은 입력 자료 출처를 포함합니다. 합성 단색 사진으로 상품 보존·브랜드 사진 품질은 검증할 수 없습니다.",
        "- 생성 API 시간·함수 실행 시간·전체 사람 과제 시간·Instagram 발행 시간을 서로 대체하지 마세요.",
        "- 소수 실행의 지연 차이는 모델 성능 향상·워크플로 시간 절감·마케팅 효과의 증거가 아닙니다."]
    atomic_write(folder / "report.md", ("\n".join(lines) + "\n").encode())
    return folder


def main():
    parser = argparse.ArgumentParser(description="버전별 자동 검사·실제 API 평가와 기록 보존")
    parser.add_argument("command", choices=["run", "list", "live", "report", "review-template", "review"])
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--review-file", type=Path)
    args = parser.parse_args()
    settings = Settings()
    settings.evaluation_dir = args.results_dir
    if args.command == "live":
        import os
        from dotenv import dotenv_values
        if not args.env_file or not args.env_file.is_file():
            parser.error("live 실행에는 실제 .env 파일 경로가 필요합니다")
        values = dotenv_values(args.env_file)
        for key, value in values.items():
            if value is not None:
                os.environ[key] = value
        settings = Settings.from_env()
        configured = Path(values.get("DATA_DIR") or "data")
        settings.data_dir = configured if configured.is_absolute() else args.env_file.parent / configured
        settings.evaluation_dir = args.results_dir
        if not settings.ai_ready or not (settings.data_dir / "app.sqlite3").is_file():
            parser.error("기존 앱의 텍스트 호출 설정과 비용 원장이 필요합니다. 설정·예산을 자동 변경하지 않습니다")
        run_live(settings)
    elif args.command == "run":
        run, folder = run_automated(settings)
        print(json.dumps({"run_id": run["id"], "passed": sum(r.get("passed") is True for r in run["results"]),
                          "failed": sum(r.get("passed") is False for r in run["results"]),
                          "pending": sum(r.get("passed") is None for r in run["results"]),
                          "export": str(folder)}, ensure_ascii=False))
    elif args.command == "report":
        print(write_report(settings))
    elif args.command in {"review-template", "review"}:
        if not args.run_id:
            parser.error("--run-id가 필요합니다")
        store = Store(settings)
        run = store.get("local-owner", args.run_id)
        if args.command == "review-template":
            items, options = review_spec(run)
            print(json.dumps({"reviewer": "", "role": "developer", "ratings": {key: "unreviewed" for key in items},
                              "notes": "", "rating_labels": items, "rating_options": options}, ensure_ascii=False, indent=2))
        else:
            if not args.review_file or not args.review_file.is_file():
                parser.error("--review-file JSON 파일이 필요합니다")
            review = json.loads(args.review_file.read_text())
            store.update("local-owner", args.run_id, lambda value: record_review(value, review.get("reviewer", ""),
                         review.get("role", ""), review.get("ratings", {}), review.get("notes", "")))
            print(store.export("local-owner", args.run_id))
    else:
        print(json.dumps([{key: row[key] for key in ("id", "kind", "variant", "state", "started_at")}
                          for row in Store(settings).list("local-owner")], ensure_ascii=False))


if __name__ == "__main__":
    main()
