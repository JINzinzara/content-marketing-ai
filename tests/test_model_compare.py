"""실제 네트워크 없이 비용·중단·입력 고정·사람 판정의 경계를 검증한다."""
import base64
import csv
import io
import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from PIL import Image

import model_compare as mc
from app.config import ROOT, Settings
from app.db import connect


def image_bytes(size=(1024, 1024)):
    out = io.BytesIO()
    Image.new("RGB", size, "tan").save(out, "PNG")
    return out.getvalue()


@pytest.fixture
def experiment(tmp_path):
    config = mc.read_json(ROOT / "comparison/spooky-cookie.json")
    for case in config["cases"]:
        case["file"] = case["id"] + ".png"
        (tmp_path / case["file"]).write_bytes(image_bytes((640, 800)))
    config_path = tmp_path / "config.json"
    mc.save_json(config_path, config)
    out = tmp_path / "run"
    manifest = mc.prepare(config_path, out)
    cfg = Settings(data_dir=tmp_path / "data", openai_enabled=True, image_enabled=True,
                   openai_key="never-save-this-secret", ai_budget_usd=Decimal("5"))
    return out, manifest, cfg, config_path


def handler(request):
    payload = json.loads(request.content)
    if request.url.path.endswith("input_tokens"):
        return httpx.Response(200, json={"input_tokens": 1000})
    if request.url.path.endswith("responses"):
        assert payload["service_tier"] == "default"
        draft = {"title": "스푸키쿠키", "caption": "스푸키쿠키 👻\n\n할로윈 쿠키를 만나보세요.\n\n10월 26일~11월 6일 한정 판매해요.",
                 "hashtags": "#할로윈 #카페 #대구카페 #쿠키", "layout": "frame"}
        return httpx.Response(200, json={"status": "completed", "id": "resp1", "usage": {
            "input_tokens": 1000, "output_tokens": 100, "input_tokens_details": {"cached_tokens": 100}},
            "output": [{"content": [{"type": "output_text", "text": json.dumps(draft)}]}]})
    assert request.url.path == "/v1/images/edits"
    if payload["model"] == "gpt-image-1-mini":
        assert payload["input_fidelity"] == "low"
    else:
        assert "input_fidelity" not in payload
    size = tuple(map(int, payload["size"].split("x")))
    return httpx.Response(200, json={"usage": {"input_tokens": 1100, "output_tokens": 500,
        "input_tokens_details": {"text_tokens": 100, "image_tokens": 1000}},
        "data": [{"b64_json": base64.b64encode(image_bytes(size)).decode()}]})


def fill_reviews(out, grade="충족"):
    with (out / "reviews.csv").open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    for row in rows:
        row.update(grade=grade, reason="사람이 항목을 확인함", evaluator="테스터")
    mc.write_csv(out / "reviews.csv", rows, tuple(rows[0]))
    return rows


def test_freezes_twenty_slots_and_requires_execute(experiment, capsys):
    out, manifest, cfg, config = experiment
    assert len(manifest["samples"]) == 20
    assert all(sum(s["model"] == m for s in manifest["samples"]) == 5 for m in mc.MODELS)
    assert not cfg.data_dir.exists()
    mc.main(["run", str(out)])
    assert "유료 호출 없음" in capsys.readouterr().out
    assert not cfg.data_dir.exists()
    with pytest.raises(FileExistsError):
        mc.prepare(config, out)
    with pytest.raises(ValueError, match="전체"):
        mc.prepare(config, out.parent / "bad", model="gpt-5.4")
    request = out / manifest["samples"][0]["request"]
    request.write_text("{}")
    with pytest.raises(ValueError, match="변경"):
        mc.frozen_manifest(out)


def test_full_mock_run_keeps_raw_preview_usage_and_never_repeats(experiment):
    out, manifest, cfg, _ = experiment
    calls = []
    def counted(request):
        calls.append(request.url.path)
        return handler(request)
    with httpx.Client(transport=httpx.MockTransport(counted)) as client:
        assert mc.run(out, cfg, client, limit=1) == 1
        assert mc.run(out, cfg, client) == 19
        assert mc.run(out, cfg, client) == 0
    assert len(calls) == 30  # 20 생성 + 문구 10건 입력 토큰 사전 계산
    assert calls.count("/v1/responses") == calls.count("/v1/images/edits") == 10
    for sample in manifest["samples"]:
        folder = out / "samples" / sample["id"]
        record = mc.read_json(folder / "attempt.json")
        assert record["state"] == "completed"
        assert record["cost_microusd"] > 0
        assert record["browser_user_wait_seconds"] is None
        if sample["kind"] == "image":
            assert (folder / "generated-original.png").read_bytes() == image_bytes()
            with Image.open(folder / "preview.jpg") as image:
                assert image.size == (1080, 1350)
        else:
            assert (folder / "draft.json").exists()
    with connect(cfg) as con:
        assert con.execute("SELECT COUNT(*) FROM ai_calls").fetchone()[0] == 20
        assert con.execute("SELECT COUNT(*) FROM credits").fetchone()[0] == 0
    assert all(r["passed"] == 0 and r["unreviewed"] == 5 for r in mc.report(out))
    rows = fill_reviews(out)
    rows[0]["grade"] = "부분 충족"
    mc.write_csv(out / "reviews.csv", rows, tuple(rows[0]))
    assert sum(r["passed"] for r in mc.report(out)) == 19
    page = (out / "review.html").read_text()
    assert all(model not in page for model in mc.MODELS)
    assert cfg.openai_key not in "".join(p.read_text() for p in out.rglob("*.json"))


def test_unknown_usage_stops_and_requires_billing_reconciliation(experiment):
    out, manifest, cfg, _ = experiment
    def unknown(request):
        if request.url.path.endswith("input_tokens"):
            return handler(request)
        raise httpx.ReadTimeout("timeout")
    with httpx.Client(transport=httpx.MockTransport(unknown)) as client:
        assert mc.run(out, cfg, client) == 1
        with pytest.raises(ValueError, match="불명"):
            mc.run(out, cfg, client)
    paths = list((out / "samples").glob("*/attempt.json"))
    record = mc.read_json(paths[0])
    assert record["state"] == "unknown" and record["cost_microusd"] is None
    with connect(cfg) as con:
        row = con.execute("SELECT cost_microusd, reserved_microusd FROM ai_calls").fetchone()
        assert row[0] is None and row[1] > 0
    mc.settle(out, record["sample_id"], "0.0123", "제공사 청구 내역 확인", cfg)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert mc.run(out, cfg, client, limit=1) == 1
    assert mc.read_json(paths[0])["state"] == "interrupted"


def test_shared_budget_and_source_drift_block_calls(experiment, monkeypatch):
    out, manifest, cfg, _ = experiment
    cfg.ai_budget_usd = Decimal("0.000001")
    def preflight_only(request):
        assert request.url.path.endswith("input_tokens")
        return handler(request)
    with httpx.Client(transport=httpx.MockTransport(preflight_only)) as client:
        mc.run(out, cfg, client)
    with connect(cfg) as con:
        assert con.execute("SELECT COUNT(*) FROM ai_calls").fetchone()[0] == 0
    summary = mc.report(out)
    assert all(row["cost_per_pass_usd"] == "" for row in summary)
    monkeypatch.setattr(mc, "source_hashes", lambda: {})
    with httpx.Client(transport=httpx.MockTransport(preflight_only)) as client:
        with pytest.raises(ValueError, match="코드/기준"):
            mc.run(out, cfg, client)


def test_raw_rejected_draft_and_html_escape_are_preserved(experiment):
    out, _, cfg, config = experiment
    # 탐색의 단일 모델·단일 사진도 지원한다.
    out = out.parent / "explore"
    manifest = mc.prepare(config, out, "explore", "gpt-5.4", "original")
    def invalid(request):
        response = handler(request)
        if request.url.path.endswith("responses"):
            body = response.json()
            draft = json.loads(body["output"][0]["content"][0]["text"])
            draft["caption"] = "<script>alert('x')</script> 가격 999원"
            body["output"][0]["content"][0]["text"] = json.dumps(draft)
            return httpx.Response(200, json=body)
        return response
    with httpx.Client(transport=httpx.MockTransport(invalid)) as client:
        mc.run(out, cfg, client)
    folder = out / "samples" / manifest["samples"][0]["id"]
    assert (folder / "response.json").exists() and (folder / "draft.json").exists()
    assert mc.read_json(folder / "attempt.json")["state"] == "failed"
    page = (out / "review.html").read_text()
    assert "&lt;script&gt;" in page and "<script>" not in page
    fill_reviews(out)
    assert mc.report(out)[0]["passed"] == 0


def test_monetary_rates_cache_unknown_and_long_context():
    text = {"input": "2.5", "cached_input": ".25", "output": "15"}
    usage = {"input_tokens": 100, "output_tokens": 20, "input_tokens_details": {"cached_tokens": 40}}
    assert mc.cost_usage("gpt-5.4", usage, text) == 460
    assert mc.cost_usage("gpt-5.4", {"input_tokens": 100, "output_tokens": 1}, text) is None
    usage.update(input_tokens=300_000, output_tokens=100, input_tokens_details={"cached_tokens": 0})
    assert mc.cost_usage("gpt-5.4", usage, text) == 1_502_250
    rates = {"text_input": "2.5", "image_input": "4", "output": "15"}
    image = {"input_tokens": 1100, "output_tokens": 500, "input_tokens_details": {"text_tokens": 100, "image_tokens": 1000}}
    assert mc.cost_usage("gpt-image-2", image, rates) == 11750
    image["input_tokens"] = 1200
    assert mc.cost_usage("gpt-image-2", image, rates) is None
    for invalid in ("NaN", "Infinity", "-1", True, "oops"):
        with pytest.raises(ValueError):
            mc.dollars(invalid)


def test_missing_human_rows_or_reason_cannot_become_pass(experiment):
    out, _, _, _ = experiment
    rows = fill_reviews(out)
    rows[0]["reason"] = ""
    mc.write_csv(out / "reviews.csv", rows, tuple(rows[0]))
    with pytest.raises(ValueError, match="이유"):
        mc.report(out)
    rows.pop()
    mc.write_csv(out / "reviews.csv", rows, tuple(rows[0]))
    rows[0]["reason"] = "확인"
    mc.write_csv(out / "reviews.csv", rows, tuple(rows[0]))
    with pytest.raises(ValueError, match="누락"):
        mc.report(out)


def test_crash_after_reservation_is_not_resent_and_recovers_ledger_id(experiment):
    out, manifest, cfg, _ = experiment
    def crash(request):
        if request.url.path.endswith("input_tokens"):
            return handler(request)
        raise KeyboardInterrupt()
    with httpx.Client(transport=httpx.MockTransport(crash)) as client:
        with pytest.raises(KeyboardInterrupt):
            mc.run(out, cfg, client)
    paths = list((out / "samples").glob("*/attempt.json"))
    record = mc.read_json(paths[0])
    assert record["state"] == "sending"
    record.pop("call_id")  # reserve commit 직후 파일 기록 전 종료도 복구한다.
    record["state"] = "preparing"
    mc.save_json(paths[0], record)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="불명"):
            mc.run(out, cfg, client)
    mc.settle(out, record["sample_id"], "0", "제공사 내역에서 미청구 확인", cfg)
    with connect(cfg) as con:
        row = con.execute("SELECT id, cost_microusd FROM ai_calls").fetchone()
        assert row[1] == 0
        assert mc.read_json(paths[0])["call_id"] == row[0]


def test_image_overrun_stops_before_next_slot_and_can_be_reconciled(experiment):
    _, _, cfg, config_path = experiment
    config = mc.read_json(config_path)
    config["models"]["gpt-image-2"]["reserve_usd"] = "0.000001"
    mc.save_json(config_path, config)
    out = config_path.parent / "overrun"
    manifest = mc.prepare(config_path, out, "explore", "gpt-image-2")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert mc.run(out, cfg, client) == 1
        with pytest.raises(ValueError, match="초과"):
            mc.run(out, cfg, client)
    paths = list((out / "samples").glob("*/attempt.json"))
    assert len(paths) == 1
    record = mc.read_json(paths[0])
    mc.settle(out, record["sample_id"], "0.01175", "사용량과 실제 청구 확인", cfg)
    with connect(cfg) as con:
        row = con.execute("SELECT cost_microusd, reserved_microusd FROM ai_calls").fetchone()
        assert row[0] == row[1] == 11750
    assert mc.read_json(paths[0])["reserved_microusd"] == 1  # 최초 예약 추정치는 기록에 보존


def test_portrait_raw_output_kept_and_existing_app_budget_shared(experiment):
    _, _, cfg, config_path = experiment
    config = mc.read_json(config_path)
    config["models"]["gpt-image-2"]["size"] = "1024x1536"
    mc.save_json(config_path, config)
    out = config_path.parent / "portrait"
    manifest = mc.prepare(config_path, out, "explore", "gpt-image-2", "original")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        mc.run(out, cfg, client)
    folder = out / "samples" / manifest["samples"][0]["id"]
    with Image.open(folder / "generated-original.png") as image:
        assert image.size == (1024, 1536)
    with Image.open(folder / "preview.jpg") as image:
        assert image.size == (1080, 1350)
    with connect(cfg, write=True) as con:
        con.execute("INSERT INTO accounts(id, created_at) VALUES ('ordinary-app-user', 0)")
        con.execute("INSERT INTO ai_calls(id, owner_id, purpose, model, state, reserved_microusd, cost_microusd, created_at) VALUES ('existing-app-call', 'ordinary-app-user', 'draft', 'gpt-5.4', 'completed', 4990000, 4990000, 0)")
    second = config_path.parent / "budget-check"
    mc.prepare(config_path, second, "explore", "gpt-image-2", "original")
    with httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail("예산 초과 시 외부 호출 금지"))) as client:
        mc.run(second, cfg, client)
    assert mc.report(second)[0]["not_sent"] == 1


def test_valid_output_with_missing_usage_is_kept_but_stops(experiment):
    _, _, cfg, config_path = experiment
    out = config_path.parent / "missing-usage"
    manifest = mc.prepare(config_path, out, "explore", "gpt-image-2")
    def missing(request):
        body = handler(request).json()
        body.pop("usage")
        return httpx.Response(200, json=body)
    with httpx.Client(transport=httpx.MockTransport(missing)) as client:
        assert mc.run(out, cfg, client) == 1
    record = mc.read_json(mc.record_path(out, manifest["samples"][0]))
    assert record["state"] == "completed" and record["cost_microusd"] is None
    assert (out / "samples" / record["sample_id"] / "preview.jpg").exists()
    assert mc.report(out)[0]["unknown_cost_count"] == 1
    mc.settle(out, record["sample_id"], "0.01", "제공사 청구 내역 확인", cfg)
    assert mc.read_json(mc.record_path(out, manifest["samples"][0]))["state"] == "completed"
