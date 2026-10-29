"""고정 입력·모델의 유료 비교. prepare/report는 오프라인, run --execute만 API 호출."""

import argparse
import base64
import csv
import fcntl
import hashlib
import html
import io
import importlib.metadata
import json
import secrets
import re
import sys
import statistics
import subprocess
import time
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from pathlib import Path

import httpx
from fastapi import HTTPException

from app.config import ROOT, Settings
from app.costs import microusd, reconcile, reserve, spent
from app.db import connect, initialize
from app.image_ai import edit_payload, edit_prompt, generated_png
from app.rendering import atomic_write, fit_photo, normalize_photo
from app.text_ai import Draft, Recommendation, Style, parse_response, response_payload, validate_draft

MODELS = {"gpt-5.4-mini": "text", "gpt-5.4": "text",
          "gpt-image-1-mini": "image", "gpt-image-2": "image"}
RESULT_TYPES = {"text": Draft, "style": Style, "recommendation": Recommendation}
SUITE_RUNNERS = {"analysis": "analysis_compare.py", "style_pipeline": "style_pipeline_compare.py"}
CRITERIA = {
    "text": {"T01": "본문 상품명·종류", "T02": "본문 할로윈", "T03": "정확한 양쪽 날짜·한정 판매",
             "T04": "미제공 사실 없음(제목·본문·태그)", "T05": "본문 제품명으로 시작(이모지 제외)",
             "T06": "본문 2~4개 짧은 문단", "T07": "부드러운 존댓말",
             "T08": "필수 태그 4개", "T09": "고유 태그 4~6개"},
    "image": {"H01": "메인 쿠키 한 개·상품 보존", "H02": "따뜻한 가게 스타일",
              "H03": "경계·그림자·접촉면 자연스러움", "H04": "1080×1350·잘림/새 글자 없음"},
}
GRADES = {"", "충족", "부분 충족", "미충족"}
REVIEW_FIELDS = ("sample_id", "kind", "group", "case", *CRITERIA["text"], *CRITERIA["image"], "reason")
SOURCE_FILES = ("model_compare.py", "analysis_compare.py", "style_pipeline_compare.py", "app/text_ai.py", "app/image_ai.py", "app/rendering.py",
                "app/costs.py", "app/config.py", "app/db.py", "app/schema.sql", "app/styles.py", "app/analytics.py",
                "docs/evaluation-protocol.md", "pyproject.toml")
OWNER = "model-comparison"


def read_json(path):
    return json.loads(Path(path).read_text())


def save_json(path, value):
    atomic_write(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def dollars(value):
    if isinstance(value, bool):
        raise ValueError("금액에 true/false를 쓸 수 없습니다")
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("금액·단가는 숫자여야 합니다") from None
    if not number.is_finite() or number < 0:
        raise ValueError("금액·단가는 유한한 음수 아닌 수여야 합니다")
    return number


def token(value):
    return type(value) is int and value >= 0


def cost_usage(model, usage, rates):
    """반환 단위는 microUSD. 이미지 캐시 구분이 없어 비캐시 단가의 상한 추정치 사용."""
    if not isinstance(usage, dict):
        return None
    incoming, outgoing = usage.get("input_tokens"), usage.get("output_tokens")
    details = usage.get("input_tokens_details") or {}
    if not isinstance(details, dict) or not all(map(token, (incoming, outgoing))):
        return None
    r = {k: dollars(v) for k, v in rates.items()}
    if MODELS[model] == "text":
        cached = details.get("cached_tokens")
        if not token(cached) or cached > incoming:
            return None
        # GPT-5.4의 272K 초과 입력 장문 할증도 예약·정산에 적용한다.
        long = model == "gpt-5.4" and incoming > 272_000
        value = ((incoming - cached) * r["input"] + cached * r["cached_input"]) * (2 if long else 1)
        value += outgoing * r["output"] * (Decimal("1.5") if long else 1)
    else:
        text, image = details.get("text_tokens"), details.get("image_tokens")
        if not all(map(token, (text, image))) or text + image != incoming:
            return None
        value = text * r["text_input"] + image * r["image_input"] + outgoing * r["output"]
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def write_csv(path, rows, fields):
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    atomic_write(path, out.getvalue().encode("utf-8-sig"))


def review_template(samples):
    return [{**dict.fromkeys(REVIEW_FIELDS, ""), "sample_id": s["id"],
             "kind": s["kind"], "group": s["group"], "case": s["case"]} for s in samples]


def source_hashes(root=ROOT):
    return {name: digest((root / name).read_bytes()) for name in SOURCE_FILES}


def prepare(config_path, out, phase="final", model=None, case=None):
    config_path, out = Path(config_path).resolve(), Path(out).resolve()
    cfg = read_json(config_path)
    if set(cfg["models"]) != set(MODELS) or len(cfg["cases"]) != 5:
        raise ValueError("비교 후보 4개와 입력 5개가 필요합니다")
    if any(not re.fullmatch(r"[a-z0-9-]+", c["id"]) for c in cfg["cases"]):
        raise ValueError("입력 ID에는 영문 소문자·숫자·하이픈만 허용합니다")
    if len({c["id"] for c in cfg["cases"]}) != 5:
        raise ValueError("입력 ID가 중복됐습니다")
    if phase == "final" and (model or case):
        raise ValueError("최종 비교는 모델 4개·사진 5개 전체를 사용합니다")
    if model and model not in MODELS or case and case not in {c["id"] for c in cfg["cases"]}:
        raise ValueError("알 수 없는 모델/입력입니다")
    for name, settings in cfg["models"].items():
        rates = settings["rates_per_million_usd"]
        expected = {"input", "cached_input", "output"} if MODELS[name] == "text" else {"text_input", "image_input", "output"}
        if set(rates) != expected or any(dollars(v) <= 0 for v in rates.values()):
            raise ValueError("필수 단가를 모두 양수로 입력하세요")
        if MODELS[name] == "text":
            if type(settings["max_output_tokens"]) is not int or not 256 <= settings["max_output_tokens"] <= 8192:
                raise ValueError("출력 한도는 256~8192 정수여야 합니다")
            if settings["reasoning_effort"] not in {"none", "low", "medium", "high", "xhigh"}:
                raise ValueError("지원하는 추론 설정을 확인하세요")
        elif settings["quality"] not in {"low", "medium", "high"} or settings["size"] not in {"1024x1024", "1024x1536", "1536x1024"} or dollars(settings["reserve_usd"]) <= 0:
            raise ValueError("이미지 품질·크기·예약 금액을 확인하세요")
    # 경로·이미지를 검증한 뒤에만 새 실험 폴더를 만든다.
    inputs = [(c, (config_path.parent / c["file"]).read_bytes()) for c in cfg["cases"] if not case or c["id"] == case]
    normalized = [(c, raw, normalize_photo(raw)) for c, raw in inputs]
    secrets.SystemRandom().shuffle(normalized)
    out.mkdir(parents=True, exist_ok=False, mode=0o700)
    (out / "inputs").mkdir()
    (out / "samples").mkdir()
    (out / "requests").mkdir()
    models = [m for m in MODELS if not model or m == model]
    groups = {}
    for kind in ("text", "image"):
        names = [m for m in models if MODELS[m] == kind]
        secrets.SystemRandom().shuffle(names)
        groups.update({name: chr(65 + i) for i, name in enumerate(names)})
    manifest = {"version": 1, "run_id": secrets.token_hex(8), "phase": phase,
                "created_at": time.time(), "config": cfg, "source_sha256": source_hashes(),
                "files_sha256": {}, "samples": [], "regeneration": "first_attempt_only",
                "browser_user_wait_seconds": None, "python_version": sys.version,
                "dependencies": {name: importlib.metadata.version(name) for name in ("httpx", "Pillow", "pydantic")}}
    try:
        manifest["git_head"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, stderr=subprocess.DEVNULL, text=True).strip()
    except subprocess.CalledProcessError:
        manifest["git_head"] = None
    atomic_write(out / "protocol.md", (ROOT / "docs/evaluation-protocol.md").read_bytes())
    manifest["files_sha256"]["protocol.md"] = digest((out / "protocol.md").read_bytes())
    for c, raw, photo in normalized:
        input_name = "inputs/" + c["id"] + ".jpg"
        atomic_write(out / input_name, photo)
        manifest["files_sha256"][input_name] = digest(photo)
        schedule = models.copy()
        secrets.SystemRandom().shuffle(schedule)
        for name in schedule:
            sid = secrets.token_hex(6)
            settings = cfg["models"][name]
            kind = MODELS[name]
            if kind == "text":
                data = {"task": "confirmed_product_only", "shop": cfg["shop"],
                        "confirmed_style": cfg["caption_style"], "resolved_visual_style": cfg["visual_style"],
                        "past_post_examples": [], "show_price": False, "confirmed_product": cfg["product"],
                        "caption_requirements": cfg["caption_requirements"]}
                payload = response_payload(name, data, Draft, ["data:image/jpeg;base64," + base64.b64encode(photo).decode()])
                payload.update(max_output_tokens=settings["max_output_tokens"], store=False, service_tier="default",
                               reasoning={"effort": settings["reasoning_effort"]})
            else:
                prompt = edit_prompt(cfg["product"]["product_name"], cfg["visual_style"])
                prompt += "\n" + cfg["main_product"]
                payload = edit_payload(name, photo, prompt, settings["quality"], settings["size"])
            request_name = "requests/" + sid + ".json"
            save_json(out / request_name, payload)
            manifest["files_sha256"][request_name] = digest((out / request_name).read_bytes())
            manifest["samples"].append({"id": sid, "case": c["id"], "kind": kind, "model": name,
                "group": groups[name], "request": request_name, "input": input_name,
                "source_sha256": digest(raw), "provenance": c["provenance"]})
    save_json(out / "manifest.json", manifest)
    atomic_write(out / "manifest.sha256", (digest((out / "manifest.json").read_bytes()) + "\n").encode())
    write_csv(out / "reviews.csv", review_template(manifest["samples"]), REVIEW_FIELDS)
    atomic_write(out / "evaluator.txt", "본인\n".encode())
    bundle_rows = [{"kind": k, "group": g, "grade": "", "reason": ""}
                   for k, g in sorted({(s["kind"], s["group"]) for s in manifest["samples"]})]
    write_csv(out / "bundles.csv", bundle_rows, ("kind", "group", "grade", "reason"))
    write_csv(out / "preferences.csv", [{"kind": k, "preference": "", "reason": ""}
              for k in sorted({s["kind"] for s in manifest["samples"]})
              if len({s["group"] for s in manifest["samples"] if s["kind"] == k}) == 2], ("kind", "preference", "reason"))
    report(out)
    return manifest


def frozen_manifest(out):
    manifest_path = out / "manifest.json"
    if digest(manifest_path.read_bytes()) != (out / "manifest.sha256").read_text().strip():
        raise ValueError("실험 명세가 변경됐습니다. 설정을 바꾸려면 새 폴더로 prepare하세요")
    manifest = read_json(manifest_path)
    for name, expected in manifest["files_sha256"].items():
        if digest((out / name).read_bytes()) != expected:
            raise ValueError("고정 입력/요청이 변경됐습니다: " + name)
    return manifest


def record_path(out, sample):
    return out / "samples" / sample["id"] / "attempt.json"


def ledger_update(settings, record):
    if not record.get("call_id"):
        return
    usage = record.get("usage") if isinstance(record.get("usage"), dict) else {}
    with connect(settings, write=True) as con:
        con.execute("UPDATE ai_calls SET state=?, cost_microusd=?, input_tokens=?, output_tokens=?, response_id=? WHERE id=?",
                    (record["state"], record.get("cost_microusd"), usage.get("input_tokens"), usage.get("output_tokens"),
                     record.get("response_id"), record["call_id"]))


def run(out, settings, client, limit=None, *, reporter=None):
    manifest = frozen_manifest(out)
    if manifest.get("suite") in SUITE_RUNNERS and reporter is None:
        raise ValueError(SUITE_RUNNERS[manifest["suite"]] + "로 실행하세요")
    if manifest["dependencies"] != {name: importlib.metadata.version(name) for name in manifest["dependencies"]}:
        raise ValueError("prepare 이후 라이브러리 버전이 변경됐습니다. 새 실험을 준비하세요")
    if source_hashes() != manifest["source_sha256"]:
        raise ValueError("실행 코드/기준이 prepare 이후 변경됐습니다. 새 실험을 준비하세요")
    kinds = {s["kind"] for s in manifest["samples"]}
    if not settings.openai_key or not settings.openai_enabled or ("image" in kinds and not settings.image_enabled):
        raise ValueError(".env의 OPENAI_API_KEY·OPENAI_ENABLED·OPENAI_IMAGE_ENABLED 설정을 확인하세요")
    if dollars(settings.ai_budget_usd) <= 0:
        raise ValueError("OPENAI_BUDGET_USD를 양수로 설정하세요(앱과 공유하는 누적 한도)")
    binding_path = out / "budget-ledger.json"
    binding = {"database": str((settings.data_dir / "app.sqlite3").resolve())}
    if binding_path.exists() and read_json(binding_path) != binding:
        raise ValueError("이 실험이 사용하는 예산 DB 경로가 바뀌었습니다")
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    initialize(settings)
    save_json(binding_path, binding)
    headers = {"Authorization": "Bearer " + settings.openai_key}
    records = [read_json(p) for p in (out / "samples").glob("*/attempt.json")]
    if any(r.get("call_id") and r.get("cost_microusd") is None or r["state"] in {"preparing", "sending"} for r in records):
        raise ValueError("이전 요청 결과/비용이 불명입니다. 확인 후 settle로 비용을 기록하세요. 자동 재호출하지 않습니다")
    if any(r.get("cost_microusd") is not None and r.get("reserved_microusd") is not None
           and r["cost_microusd"] > r["reserved_microusd"] and not r.get("overrun_acknowledged") for r in records):
        raise ValueError("예약 금액을 초과한 요청이 있습니다. 청구 확인 후 settle로 정산하세요")
    completed = 0
    for sample in manifest["samples"]:
        path = record_path(out, sample)
        if path.exists():
            continue
        if limit is not None and completed >= limit:
            break
        folder = path.parent
        folder.mkdir()
        started = time.monotonic()
        record = {"sample_id": sample["id"], "state": "preparing", "started_at": time.time(),
                  "cost_microusd": None, "browser_user_wait_seconds": None}
        save_json(path, record)
        payload = read_json(out / sample["request"])
        model = sample["model"]
        cfg = manifest["config"]["models"][model]
        rates = cfg["rates_per_million_usd"]
        sent = None
        try:
            if sample["kind"] == "text":
                base = {k: v for k, v in payload.items() if k not in {"max_output_tokens", "store", "service_tier"}}
                before = time.monotonic()
                response = client.post("https://api.openai.com/v1/responses/input_tokens", json=base, headers=headers)
                record["preflight_seconds"] = time.monotonic() - before
                atomic_write(folder / "preflight.json", response.content)
                response.raise_for_status()
                count = response.json().get("input_tokens")
                if not token(count):
                    raise ValueError("입력 토큰 사전 계산 실패")
                amount = cost_usage(model, {"input_tokens": count, "output_tokens": cfg["max_output_tokens"],
                                            "input_tokens_details": {"cached_tokens": 0}}, rates)
            else:
                amount = microusd(dollars(cfg["reserve_usd"]))
            with connect(settings, write=True) as con:
                con.execute("INSERT OR IGNORE INTO accounts(id, created_at) VALUES (?, ?)", (OWNER, int(time.time())))
                record["call_id"] = reserve(con, settings, OWNER, "compare:" + manifest["run_id"] + ":" + sample["id"], model, amount)
            record.update(state="sending", reserved_microusd=amount, request_started_at=time.time())
            save_json(path, record)
            sent = time.monotonic()
            endpoint = "responses" if sample["kind"] == "text" else "images/edits"
            response = client.post("https://api.openai.com/v1/" + endpoint, json=payload, headers=headers)
            record["api_seconds"] = time.monotonic() - sent
            record["http_status"] = response.status_code
            record["request_id"] = response.headers.get("x-request-id")
            atomic_write(folder / "response.json", response.content)
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError("응답 JSON 객체가 아닙니다")
            record["usage"] = body.get("usage")
            record["response_id"] = body.get("id")
            record["returned_model"] = body.get("model")
            record["service_tier"] = body.get("service_tier")
            record["cost_microusd"] = cost_usage(model, body.get("usage"), rates)
            if sample["kind"] == "text" and body.get("service_tier") not in {None, "default"}:
                record["cost_microusd"] = None
            record["cost_basis"] = "usage_uncached_upper_estimate" if sample["kind"] == "image" else "usage_standard_rates"
            response.raise_for_status()
            if sample["kind"] == "text":
                result_type = RESULT_TYPES[sample.get("task", "text")]
                result = parse_response(body, result_type)
                if result_type is Draft:
                    save_json(folder / "draft.json", result.model_dump())
                    atomic_write(folder / "caption.txt", (result.caption + "\n\n" + result.hashtags + "\n").encode())
                    validate_draft(result, manifest["config"]["product"])
                else:
                    save_json(folder / "output.json", result.model_dump())
                record["state"] = "completed"
            else:
                encoded = body["data"][0]["b64_json"]
                if not isinstance(encoded, str) or len(encoded) > 24 * 1024 * 1024:
                    raise ValueError("잘못된 이미지 데이터")
                raw = base64.b64decode(encoded, validate=True)
                atomic_write(folder / "generated-original.png", raw)
                width, height = map(int, cfg["size"].split("x"))
                normalized = generated_png(raw, (width, height))
                preview = fit_photo(normalized, "portrait")
                buffer = io.BytesIO()
                preview.save(buffer, "JPEG", quality=92, optimize=True)
                atomic_write(folder / "preview.jpg", buffer.getvalue())
                record["state"] = "completed"
            record["cli_artifact_ready_seconds"] = time.monotonic() - started
        except (httpx.HTTPError, HTTPException, ValueError, KeyError, IndexError, TypeError, AttributeError, OSError) as exc:
            record["state"] = "unknown" if record.get("call_id") and record.get("cost_microusd") is None else "failed"
            if not record.get("call_id"):
                record.update(state="not_sent", cost_microusd=0, cost_basis="no_generation_sent")
            record["error"] = exc.detail if isinstance(exc, HTTPException) else type(exc).__name__
            if sent is not None and "api_seconds" not in record:
                record["api_seconds"] = time.monotonic() - sent
        record["elapsed_seconds"] = time.monotonic() - started
        ledger_update(settings, record)
        save_json(path, record)
        completed += 1
        print(f'{sample["kind"]} {sample["case"]} {model}: {record["state"]}', flush=True)
        if record["state"] in {"unknown", "not_sent"} or record.get("call_id") and record.get("cost_microusd") is None:
            print("응답/비용/예산 확인 필요. 추가 호출을 멈췄습니다", flush=True)
            break
        if record.get("cost_microusd", 0) > record.get("reserved_microusd", 0):
            print("예약 금액 초과. 청구 확인 후 settle로 정산하세요", flush=True)
            break
    (reporter or report)(out)
    return completed


def settle(out, sample_id, amount, note, settings, *, reporter=None):
    manifest = frozen_manifest(out)
    if manifest.get("suite") in SUITE_RUNNERS and reporter is None:
        raise ValueError(SUITE_RUNNERS[manifest["suite"]] + "로 정산하세요")
    sample = next(s for s in manifest["samples"] if s["id"] == sample_id)
    path = record_path(out, sample)
    record = read_json(path)
    if not note.strip():
        raise ValueError("사용량/청구 확인 근거를 note에 남기세요")
    binding = read_json(out / "budget-ledger.json")
    if binding["database"] != str((settings.data_dir / "app.sqlite3").resolve()):
        raise ValueError("예산 DB 경로가 다릅니다")
    # reserve 직후 프로세스 종료로 call_id 파일 기록이 안 된 경우 원장에서 복구한다.
    if not record.get("call_id"):
        with connect(settings) as con:
            row = con.execute("SELECT id, reserved_microusd FROM ai_calls WHERE purpose=?", ("compare:" + manifest["run_id"] + ":" + sample_id,)).fetchone()
        if row:
            record.update(call_id=row["id"], reserved_microusd=row["reserved_microusd"])
    reconciled = microusd(dollars(amount))
    if not record.get("call_id") and reconciled:
        raise ValueError("예산 원장에 없는 요청의 양수 비용은 기록할 수 없습니다")
    if record["state"] in {"preparing", "sending", "unknown"}:
        record["state"] = "interrupted"  # 응답을 복구했다고 가정하거나 재생성하지 않는다.
    record.update(cost_microusd=reconciled, cost_basis="manual_billing_reconciliation",
                  reconciliation_note=note, reconciled_at=time.time())
    if reconciled > record.get("reserved_microusd", 0):
        record["overrun_acknowledged"] = True
    if record.get("call_id"):
        initialize(settings)
        with connect(settings, write=True) as con:
            reconcile(con, record["call_id"], reconciled, note)
            con.execute("UPDATE ai_calls SET state=? WHERE id=?", (record["state"], record["call_id"]))
    save_json(path, record)
    (reporter or report)(out)


def budget(settings):
    """비밀값 없이 누적 예산과 확인이 필요한 요청 ID를 출력한다."""
    total = 0
    rows = []
    if (settings.data_dir / "app.sqlite3").exists():
        with connect(settings) as con:
            total = spent(con)
            rows = con.execute(
                "SELECT id, purpose, model, state, reserved_microusd, cost_microusd FROM ai_calls WHERE cost_microusd IS NULL OR cost_microusd > reserved_microusd ORDER BY created_at, id"
            ).fetchall()
    print(f"누적 예산 ${dollars(settings.ai_budget_usd)} · 사용량 추정+미확정 예약 ${Decimal(total) / 1_000_000:.6f}")
    for row in rows:
        cost = "미확정" if row["cost_microusd"] is None else str(Decimal(row["cost_microusd"]) / 1_000_000)
        print(f'{row["id"]} | {row["purpose"]} | {row["model"]} | {row["state"]} | 예약 ${Decimal(row["reserved_microusd"]) / 1_000_000} | 비용 {cost}')
    if not rows:
        print("비용 미확정·예약 초과 요청 없음")


def settle_app_call(settings, call_id, amount, note):
    if not (settings.data_dir / "app.sqlite3").exists():
        raise ValueError("기존 앱의 예산 원장이 없습니다")
    initialize(settings)
    with connect(settings, write=True) as con:
        call = con.execute("SELECT purpose FROM ai_calls WHERE id=?", (call_id,)).fetchone()
        if call and call["purpose"].startswith("compare:"):
            raise ValueError("모델 비교 요청은 settle <실행 폴더> --sample로 정산하세요")
        reconcile(con, call_id, microusd(dollars(amount)), note)


def reviewed_rows(path, keys, expected, allowed=GRADES, value_column="grade", evaluator="", criteria=None, fields=REVIEW_FIELDS):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        columns = set(fields[4:-1]) if criteria else {value_column}
        required = set(fields) if criteria else set(keys) | columns | {"reason"}
        if not required.issubset(reader.fieldnames or ()):
            raise ValueError("사람 판정 열이 누락됐습니다: " + path.name)
        if len(reader.fieldnames) != len(set(reader.fieldnames)):
            raise ValueError("사람 판정 열 이름이 중복됐습니다: " + path.name)
        rows = list(reader)
    result = {}
    for row in rows:
        if None in row or any(value is None for value in row.values()):
            raise ValueError("CSV의 열 개수가 맞지 않습니다: " + path.name)
        key = tuple(row[k] for k in keys)
        if key in result or key not in expected:
            raise ValueError("사람 판정 ID가 중복되거나 잘못됐습니다: " + str(key))
        selected = criteria[key] if criteria else (value_column,)
        if criteria and any(row[c].strip() for c in columns - set(selected)):
            raise ValueError("다른 영역의 판정 칸은 비워 두세요: " + str(key))
        for column in selected:
            row[column] = row[column].strip()
            if row[column] not in allowed:
                raise ValueError("판정 값이 잘못됐습니다: " + row[column])
        row["evaluator"] = row.get("evaluator", "").strip() or evaluator
        if any(row[c] for c in selected) and not row["evaluator"]:
            raise ValueError("판정에는 사람 평가자 이름이 필요합니다")
        if any(row[c] not in {"", "충족"} for c in selected) and not row["reason"].strip():
            raise ValueError("부분 충족·미충족 또는 A/B 선호 판정에는 이유가 필요합니다: " + str(key))
        result[key] = row
    if set(result) != expected:
        raise ValueError("사람 판정 행이 누락됐습니다")
    return result


def migrate_review_sheet(out, samples):
    """기존 판정·이유를 보존하고 결과당 한 행으로 옮긴다. 유료 호출은 하지 않는다."""
    with (out / "reviews.csv").open(encoding="utf-8-sig", newline="") as stream:
        if "criterion" not in (csv.DictReader(stream).fieldnames or ()):
            return
    sheets = {}
    for name in ("reviews.csv", "bundles.csv", "preferences.csv"):
        with (out / name).open(encoding="utf-8-sig", newline="") as stream:
            sheets[name] = list(csv.DictReader(stream))
    names = {(row.get("evaluator") or "").strip() for rows in sheets.values() for row in rows} - {""}
    evaluator_path = out / "evaluator.txt"
    evaluator = evaluator_path.read_text().strip() if evaluator_path.exists() else next(iter(names), "본인")
    if not evaluator or names - {evaluator}:
        raise ValueError("평가자가 여러 명이거나 공통 평가자와 다릅니다. 기존 기록을 유지합니다")
    legacy = reviewed_rows(out / "reviews.csv", ("sample_id", "criterion"),
                           {(s["id"], c) for s in samples for c in CRITERIA[s["kind"]]}, evaluator=evaluator)
    reviewed_rows(out / "bundles.csv", ("kind", "group"),
                  {(s["kind"], s["group"]) for s in samples}, evaluator=evaluator)
    reviewed_rows(out / "preferences.csv", ("kind",),
                  {(k,) for k in {s["kind"] for s in samples}
                   if len({s["group"] for s in samples if s["kind"] == k}) == 2},
                  {"", "A 우세", "비슷함", "B 우세"}, "preference", evaluator=evaluator)
    reviews = review_template(samples)
    for row, sample in zip(reviews, samples):
        reasons = []
        for c in CRITERIA[sample["kind"]]:
            old = legacy[(sample["id"], c)]
            row[c] = old["grade"]
            if old["reason"].strip():
                reasons.append(c + ": " + old["reason"].strip())
        row["reason"] = "\n".join(reasons)
    # 먼저 원본 세 파일을 보관하고, 재실행 시 덮어쓰지 않는다.
    for name in sheets:
        backup = out / name.replace(".csv", ".legacy.csv")
        if not backup.exists():
            atomic_write(backup, (out / name).read_bytes())
    atomic_write(evaluator_path, (evaluator + "\n").encode())
    for name in ("bundles.csv", "preferences.csv"):
        fields = ("kind", "group", "grade", "reason") if name == "bundles.csv" else ("kind", "preference", "reason")
        write_csv(out / name, [{k: row[k] for k in fields} for row in sheets[name]], fields)
    write_csv(out / "reviews.csv", reviews, REVIEW_FIELDS)


def report(out):
    manifest = frozen_manifest(out)
    samples = manifest["samples"]
    migrate_review_sheet(out, samples)
    evaluator = (out / "evaluator.txt").read_text().strip()
    if not evaluator:
        raise ValueError("evaluator.txt에 사람 평가자 이름을 한 번 입력하세요")
    reviews = reviewed_rows(out / "reviews.csv", ("sample_id",), {(s["id"],) for s in samples},
                            evaluator=evaluator, criteria={(s["id"],): CRITERIA[s["kind"]] for s in samples})
    for s in samples:
        if any(reviews[(s["id"],)][key] != s[key] for key in ("kind", "group", "case")):
            raise ValueError("사람 판정의 영역·그룹·입력 정보를 변경하지 마세요")
    bundles = reviewed_rows(out / "bundles.csv", ("kind", "group"), {(s["kind"], s["group"]) for s in samples}, evaluator=evaluator)
    preferences = reviewed_rows(out / "preferences.csv", ("kind",),
                  {(k,) for k in {s["kind"] for s in samples} if len({s["group"] for s in samples if s["kind"] == k}) == 2},
                  {"", "A 우세", "비슷함", "B 우세"}, "preference", evaluator=evaluator)
    for (kind, group), row in bundles.items():
        if row["grade"] and sum(s["kind"] == kind and s["group"] == group for s in samples) != 5:
            raise ValueError("5개 미만 탐색 묶음은 최종 일관성 판정 대상이 아닙니다")
    states, verdicts = {}, {}
    for s in samples:
        p = record_path(out, s)
        states[s["id"]] = read_json(p) if p.exists() else {"state": "planned", "cost_microusd": None}
        grades = [reviews[(s["id"],)][c] for c in CRITERIA[s["kind"]]]
        if states[s["id"]]["state"] == "completed":
            verdicts[s["id"]] = "불합격" if any(g in {"부분 충족", "미충족"} for g in grades) else "합격" if all(g == "충족" for g in grades) else "미평가"
        else:
            verdicts[s["id"]] = "생성 실패" if states[s["id"]]["state"] != "planned" else "생성 전"
    summary = []
    for model in dict.fromkeys(s["model"] for s in samples):
        selected = [s for s in samples if s["model"] == model]
        records = [states[s["id"]] for s in selected]
        known = sum(r["cost_microusd"] for r in records if r.get("cost_microusd") is not None)
        attempted = [r for r in records if r["state"] != "planned"]
        unknown = sum(r.get("cost_microusd") is None for r in attempted)
        passed = sum(verdicts[s["id"]] == "합격" for s in selected)
        unreviewed = sum(verdicts[s["id"]] == "미평가" for s in selected)
        artifacts = [r["cli_artifact_ready_seconds"] for r in attempted if "cli_artifact_ready_seconds" in r]
        times = [r["api_seconds"] for r in attempted if "api_seconds" in r]
        pass_times = [states[s["id"]]["api_seconds"] for s in selected if verdicts[s["id"]] == "합격"]
        bases = {r.get("cost_basis", "unknown") for r in attempted if r.get("cost_microusd") is not None}
        if unknown:
            bases.add("unknown")
        basis = next(iter(bases)) if len(bases) == 1 else "mixed" if bases else "not_measured"
        bundle = bundles[(selected[0]["kind"], selected[0]["group"])]["grade"]
        row = {"model": model, "kind": selected[0]["kind"], "group": selected[0]["group"], "planned": len(selected),
               "attempted_slots": len(attempted), "generation_dispatched": sum(bool(r.get("request_started_at")) for r in attempted),
               "not_sent": sum(r["state"] == "not_sent" for r in records),
               "failed_or_unknown": sum(r["state"] in {"failed", "unknown", "interrupted", "preparing", "sending"} for r in records),
               "completed": sum(r["state"] == "completed" for r in records), "passed": passed,
               "unreviewed": unreviewed,
               "all_slots_generated": all(r.get("request_started_at") for r in records),
               "known_cost_usd": str(Decimal(known) / 1_000_000), "unknown_cost_count": unknown,
               "cost_per_pass_usd": str(Decimal(known) / 1_000_000 / passed) if passed and not unknown and not unreviewed and all(r.get("request_started_at") for r in records) else "",
               "api_median_seconds": statistics.median(times) if times else "",
               "api_min_seconds": min(times) if times else "", "api_max_seconds": max(times) if times else "",
               "cli_artifact_ready_median_seconds": statistics.median(artifacts) if artifacts else "",
               "passed_api_median_seconds": statistics.median(pass_times) if pass_times else "",
               "bundle_consistency": bundle or "미평가", "browser_user_wait_seconds": "",
               "phase": manifest["phase"], "cost_basis": basis}
        summary.append(row)
    write_csv(out / "summary.csv", summary, tuple(summary[0]))
    attempts = [{**s, **states[s["id"]], "verdict": verdicts[s["id"]]} for s in samples]
    cases = {s["case"]: s["provenance"] for s in samples}
    originals = sum(value == "actual_original" for value in cases.values())
    synthetics = sum(value == "synthetic_capture_condition" for value in cases.values())
    phase = "최종 비교" if manifest["phase"] == "final" else "탐색"
    scope = (f"{phase} · 입력 {len(cases)}장(원본 {originals}장·합성 {synthetics}장) · "
             f"모델 {len(summary)}개·모델별 계획 {len(cases)}건 · 전체 계획 {len(samples)}건·"
             f"완료 {sum(r['completed'] for r in summary)}건. "
             "실제 별도 촬영/동일 입력 반복의 재현성 증거 아님.")
    save_json(out / "results.json", {"summary": summary, "attempts": attempts,
        "evaluator": evaluator, "human_reviews": list(reviews.values()),
        "human_preferences": list(preferences.values()), "human_bundles": list(bundles.values()),
        "cost_note": "cost_basis: usage_standard_rates=표준 단가 추정, usage_uncached_upper_estimate=캐시 할인 미적용 상한 추정, manual_billing_reconciliation=청구 확인 정산, mixed=근거 혼합, unknown=비용 미확정, not_measured=미실행, no_generation_sent=생성 미전송. 예약은 실제 비용 아님.",
        "evidence_scope": scope})
    esc = lambda value: html.escape(str(value))
    sections = []
    for kind, group in sorted(bundles):
        selected = [s for s in samples if (s["kind"], s["group"]) == (kind, group)]
        cards = []
        for s in selected:
            folder = out / "samples" / s["id"]
            caption = (folder / "caption.txt").read_text() if (folder / "caption.txt").exists() else ""
            media = f'<img alt="입력" src="{esc(s["input"])}">'
            for file, label in (("generated-original.png", "생성 원본"), ("preview.jpg", "최종 미리보기")):
                if (folder / file).exists():
                    media += f'<a href="samples/{s["id"]}/{file}">{label}<img alt="{label}" src="samples/{s["id"]}/{file}"></a>'
            draft = read_json(folder / "draft.json") if (folder / "draft.json").exists() else {}
            checks = " · ".join(f'{c} {esc(reviews[(s["id"],)][c] or "미평가")}' for c in CRITERIA[kind])
            cards.append(f'<article><h3>{esc(s["case"])}</h3><p>ID: {s["id"]}</p>{media}<p>제목: {esc(draft.get("title", ""))}</p><pre>{esc(caption)}</pre><p>{esc(states[s["id"]]["state"])} / {verdicts[s["id"]]}</p><small>{checks}</small></article>')
        consistency = (bundles[(kind, group)]["grade"] or "미평가") if len(selected) == 5 else "탐색: 5개 미만으로 판정 대상 아님"
        sections.append(f'<section><h2>{"문구" if kind == "text" else "이미지"} 그룹 {group}</h2><p>묶음 일관성: {esc(consistency)}</p><div class="row">{"".join(cards)}</div></section>')
    definitions = "".join(f'<li>{key}: {esc(value)}</li>' for criteria in CRITERIA.values() for key, value in criteria.items())
    page = '<!doctype html><html lang="ko"><meta charset="utf-8"><title>스푸키쿠키 사람 검증</title><style>body{font:16px system-ui;margin:24px;background:#f8f4ee;color:#39291f}h1,h2{color:#76472e}.row{display:flex;gap:16px;overflow-x:auto}article{flex:0 0 260px;padding:16px;background:white;border-radius:12px}img{width:100%;height:auto}pre{white-space:pre-wrap;font:inherit}small{line-height:1.7}section{margin:32px 0}</style><h1>스푸키쿠키 사람 검증</h1><p>모델명·비용은 가려져 있습니다. 최종 비교에서는 같은 그룹의 5개를 나란히 보고, 개별 품질과 묶음 일관성을 별도로 평가하세요. 부분 충족은 불합격입니다.</p><p>' + esc(scope) + '</p><p>메인 상품: 큰 원형 스탠드 앞줄 오른쪽, 중앙에 가까운 쿠키 한 개. 다른 쿠키는 제거해야 합니다. 생성 원본과 4:5 미리보기를 모두 확인하세요.</p><p>reviews.csv는 결과당 한 행입니다. 문구 행의 T01~T09 또는 이미지 행의 H01~H04에 등급을 적고, 다른 영역은 비워 두세요. 충족은 reason을 생략할 수 있고, 부분 충족·미충족이 있으면 결과당 한 번 이유를 씁니다. A/B 선호 판정에는 이유가 필요합니다. 공통 평가자는 evaluator.txt의 이름을 사용하며 기본값은 본인입니다. 세부 판정은 최종 비교 기준 총 130개입니다.</p><details><summary>판정 기준</summary><ul>' + definitions + '</ul><p><a href="protocol.md">고정한 전체 평가 기준</a></p><p>bundles.csv: 5개 묶음 일관성 / preferences.csv: A 우세·비슷함·B 우세와 이유. 판정을 저장한 후 report를 다시 실행하세요. 생성 당시 protocol.md의 기준은 유지하고 입력은 위 형식을 따릅니다.</p></details>' + ''.join(sections) + '</html>'
    atomic_write(out / "review.html", page.encode())
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("prepare", help="오프라인 입력/요청/설정 고정")
    p.add_argument("--config", type=Path, default=ROOT / "comparison/spooky-cookie.json")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--phase", choices=("explore", "final"), default="final")
    p.add_argument("--model", choices=tuple(MODELS))
    p.add_argument("--case")
    commands.add_parser("budget", help="기존 예산·미확정·초과 요청 조회(호출 없음)")
    p = commands.add_parser("settle-call", help="기존 앱 요청의 청구액 정산(호출 없음)")
    p.add_argument("--call-id", required=True)
    p.add_argument("--cost-usd", required=True)
    p.add_argument("--note", required=True)
    for command in ("run", "report", "settle"):
        p = commands.add_parser(command)
        p.add_argument("out", type=Path)
        if command == "run":
            p.add_argument("--execute", action="store_true", help="명시적인 유료 실행")
            p.add_argument("--limit", type=int, help="이번 명령에서 처리할 신규 슬롯 수")
        if command == "settle":
            p.add_argument("--sample", required=True)
            p.add_argument("--cost-usd", required=True)
            p.add_argument("--note", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "budget":
            budget(Settings.from_env())
            return
        if args.command == "settle-call":
            settle_app_call(Settings.from_env(), args.call_id, args.cost_usd, args.note)
            print("기존 앱 요청 정산 완료. 변경 전후 금액과 근거를 원장에 보관했습니다")
            return
        if args.command == "prepare":
            manifest = prepare(args.config, args.out, args.phase, args.model, args.case)
            print(f'{len(manifest["samples"])}개 계획 준비 완료: {args.out.resolve()}')
            return
        out = args.out.resolve()
        # 같은 실험을 두 프로세스가 동시에 실행/정산하지 못하게 한다.
        with (out / ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if args.command == "report":
                report(out)
                print("사람 판정 집계 완료: " + str(out / "summary.csv"))
            elif args.command == "settle":
                settle(out, args.sample, args.cost_usd, args.note, Settings.from_env())
                print("청구 확인 금액 기록 완료. 해당 슬롯을 다시 생성하지 않습니다")
            elif not args.execute:
                manifest = frozen_manifest(out)
                remaining = sum(not record_path(out, s).exists() for s in manifest["samples"])
                print(f"유료 호출 없음. 남은 계획 {remaining}개. 실행하려면 --execute를 붙이세요")
            else:
                if args.limit is not None and args.limit < 1:
                    raise ValueError("limit은 1 이상이어야 합니다")
                with httpx.Client(transport=httpx.HTTPTransport(retries=0), timeout=httpx.Timeout(180, connect=10)) as client:
                    run(out, Settings.from_env(), client, args.limit)
    except (ValueError, OSError, KeyError, StopIteration, HTTPException) as exc:
        parser.exit(1, (str(exc.detail) if isinstance(exc, HTTPException) else str(exc)) + "\n")


if __name__ == "__main__":
    main()
