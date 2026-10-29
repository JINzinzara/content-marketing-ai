"""스타일·추천의 동일 입력·스키마·예산·중단·사람 판정을 모의 HTTP로 검증."""

import csv
import json
from decimal import Decimal

import httpx
import pytest

import analysis_compare as ac
import model_compare as mc
from app.config import ROOT, Settings
from app.db import connect
from app.styles import FIELDS
from test_model_compare import image_bytes


@pytest.fixture
def analysis_experiment(tmp_path):
    cfg = mc.read_json(ROOT / "comparison/analysis-mini.json")
    for case in cfg["style_cases"]:
        case["file"] = case["id"] + ".png"
        (tmp_path / case["file"]).write_bytes(image_bytes((640, 800)))
    path = tmp_path / "config.json"
    mc.save_json(path, cfg)
    out = tmp_path / "analysis"
    manifest = ac.prepare(path, out)
    settings = Settings(data_dir=tmp_path / "data", openai_enabled=True,
                        openai_key="mock-only-never-save", ai_budget_usd=Decimal("5"))
    return out, manifest, settings, path


def handler(request):
    payload = json.loads(request.content)
    assert payload["model"] in {"gpt-5.4-mini", "gpt-5.4"}
    assert payload["reasoning"] == {"effort": "none"}
    if request.url.path.endswith("input_tokens"):
        assert not {"max_output_tokens", "store", "service_tier"} & payload.keys()
        return httpx.Response(200, json={"input_tokens": 1000})
    assert request.url.path == "/v1/responses" and payload["service_tier"] == "default"
    assert payload["store"] is False and payload["max_output_tokens"] == 2048
    data = json.loads(payload["input"][0]["content"][0]["text"])
    schema = payload["text"]["format"]["name"]
    if schema == "style":
        assert payload["input"][0]["content"][1]["type"] == "input_image"
        result = {"summary": "관찰한 스타일 <script>예시</script>",
                  "attributes": {k: "입력 사진과 캡션에서 관찰한 특징" for k in FIELDS},
                  "evidence": [data["past_post_examples"][0]["id"]], "uncertainties": ["정확한 글꼴은 미확인"]}
    else:
        assert schema == "recommendation" and len(payload["input"][0]["content"]) == 1
        assert data["observed_comparisons"]
        result = {"observation": "제공된 비교 비율을 관찰했습니다", "next_content": "대표 사진 하나를 바꿔 시험하세요",
                  "time_hypothesis": "시각 차이는 추가 시험할 가설입니다", "limitations": "원인이나 매출 효과는 확인하지 않았습니다"}
    return httpx.Response(200, json={"id": "mock-response", "status": "completed", "service_tier": "default",
        "usage": {"input_tokens": 1000, "output_tokens": 150, "input_tokens_details": {"cached_tokens": 100}},
        "output": [{"content": [{"type": "output_text", "text": json.dumps(result, ensure_ascii=False)}]}]})


def csv_rows(path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def test_freezes_twenty_paired_requests_and_uses_clear_review_headers(analysis_experiment, capsys):
    out, manifest, settings, _ = analysis_experiment
    assert len(manifest["samples"]) == 20 and not settings.data_dir.exists()
    for case in manifest["cases"]:
        selected = [s for s in manifest["samples"] if s["case"] == case["id"]]
        assert len(selected) == 2 and {s["group"] for s in selected} == {"A", "B"}
        payloads = [mc.read_json(out / s["request"]) for s in selected]
        for payload in payloads:
            payload.pop("model")
        assert payloads[0] == payloads[1]
    rows = csv_rows(out / "reviews.csv")
    assert len(rows) == 20 and set(rows[0]) == set(ac.REVIEW_FIELDS)
    assert all(not row[c] for row in rows for c in ac.CRITERIA)
    assert not (out / "bundles.csv").exists()
    ac.main(["run", str(out)])
    assert "유료 호출 없음" in capsys.readouterr().out and not settings.data_dir.exists()


def test_full_mock_run_uses_task_schemas_without_writing_app_proposals(analysis_experiment):
    out, manifest, settings, _ = analysis_experiment
    calls = []
    def counted(request):
        calls.append(request.url.path)
        return handler(request)
    with httpx.Client(transport=httpx.MockTransport(counted)) as client:
        assert mc.run(out, settings, client, limit=1, reporter=ac.report) == 1
        assert mc.run(out, settings, client, reporter=ac.report) == 19
        assert mc.run(out, settings, client, reporter=ac.report) == 0
    assert len(calls) == 40 and calls.count("/v1/responses") == 20
    for s in manifest["samples"]:
        folder = out / "samples" / s["id"]
        value = mc.read_json(folder / "output.json")
        mc.RESULT_TYPES[s["task"]].model_validate(value)
        assert not (folder / "draft.json").exists() and not (folder / "caption.txt").exists()
    with connect(settings) as con:
        assert con.execute("SELECT COUNT(*) FROM ai_calls").fetchone()[0] == 20
        for table in ("style_profiles", "recommendations", "credits", "posts"):
            assert con.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] == 0
    summary = ac.report(out)
    assert all(row["completed"] == row["unreviewed"] == 5 and row["passed"] == 0 for row in summary)
    page = (out / "review.html").read_text()
    assert all(model not in page for model in mc.MODELS)
    assert "&lt;script&gt;" in page and "<script>" not in page
    assert settings.openai_key not in ''.join(p.read_text() for p in out.rglob('*.json'))
    rows = csv_rows(out / "reviews.csv")
    for row in rows:
        row.update(dict.fromkeys(ac.CRITERIA, "충족"))
    mc.write_csv(out / "reviews.csv", rows, ac.REVIEW_FIELDS)
    assert sum(row["passed"] for row in ac.report(out)) == 20
    rows[0][ac.CRITERIA[0]] = "부분 충족"
    with pytest.raises(ValueError, match="이유"):
        mc.write_csv(out / "reviews.csv", rows, ac.REVIEW_FIELDS)
        ac.report(out)
    rows[0]["reason"] = "관찰 대상과 다른 설명"
    rows[1][ac.CRITERIA[0]] = ""
    mc.write_csv(out / "reviews.csv", rows, ac.REVIEW_FIELDS)
    summary = ac.report(out)
    assert sum(row["passed"] for row in summary) == 18
    assert sum(row["unreviewed"] for row in summary) == 1


def test_unknown_request_is_not_repeated_and_can_be_settled(analysis_experiment):
    out, _, settings, _ = analysis_experiment
    def unknown(request):
        if request.url.path.endswith('input_tokens'):
            return handler(request)
        raise httpx.ReadTimeout('mock timeout')
    with httpx.Client(transport=httpx.MockTransport(unknown)) as client:
        assert mc.run(out, settings, client, reporter=ac.report) == 1
        with pytest.raises(ValueError, match="불명"):
            mc.run(out, settings, client, reporter=ac.report)
    record = mc.read_json(next((out / 'samples').glob('*/attempt.json')))
    assert record['state'] == 'unknown' and record['cost_microusd'] is None
    mc.settle(out, record['sample_id'], '0.01', '모의 청구 확인', settings, reporter=ac.report)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert mc.run(out, settings, client, limit=1, reporter=ac.report) == 1
    assert mc.read_json(out/'samples'/record['sample_id']/'attempt.json')['state'] == 'interrupted'


def test_budget_and_wrong_runner_block_paid_calls(analysis_experiment):
    out, _, settings, _ = analysis_experiment
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="analysis_compare.py"):
            mc.run(out, settings, client)
        settings.ai_budget_usd = Decimal('0.000001')
        assert mc.run(out, settings, client, reporter=ac.report) == 1
    with connect(settings) as con:
        assert con.execute('SELECT COUNT(*) FROM ai_calls').fetchone()[0] == 0
    assert sum(row['not_sent'] for row in ac.report(out)) == 1


def test_synthetic_metrics_keep_zero_missing_ties_and_purpose_groups():
    cases = mc.read_json(ROOT/'comparison/analysis-mini.json')['recommendation_cases']
    inputs = {c['id']: ac.recommendation_input(c) for c in cases}
    missing = inputs['recommend-missing']['observed_comparisons']
    assert missing[0]['save_rate'] == 0 and missing[0]['share_rate'] is None
    assert missing[1]['share_rate'] == 0
    assert [r['save_rate'] for r in inputs['recommend-rates']['observed_comparisons']] == [0.02, 0.05]
    assert len({r['save_rate'] for r in inputs['recommend-tie']['observed_comparisons']}) == 1
    assert {r['hour'] for r in inputs['recommend-time']['observed_comparisons']} == {9, 18}
    assert len(inputs['recommend-purposes']['observed_comparisons']) == 4
    for value in inputs.values():
        assert all('reach' not in row and 'saved' not in row for row in value['observed_comparisons'])
    with pytest.raises(ValueError, match="자료 부족"):
        ac.recommendation_input({'id':'empty','rows':[]})
