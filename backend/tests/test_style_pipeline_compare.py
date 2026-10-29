"""피드 분석과 후속 제작의 통제·중단·사람 판정·비용을 모의 호출로 확인."""

import json
from decimal import Decimal

import httpx
import pytest

from backend import model_compare as mc
from backend import style_pipeline_compare as pc
from backend.config import ROOT, Settings
from backend.db import connect
from backend.styles import FIELDS
from test_analysis_compare import csv_rows
from test_model_compare import handler as content_handler, image_bytes


@pytest.fixture
def pipeline(tmp_path):
    config = mc.read_json(ROOT / "backend/comparison/spooky-cookie.json")
    for case in config["cases"]:
        case["file"] = case["id"] + ".png"
        (tmp_path / case["file"]).write_bytes(image_bytes((640, 800)))
    path = tmp_path / "product.json"
    mc.save_json(path, config)
    refs = []
    for i in range(8):
        photo = tmp_path / f"reference-{i}.png"
        photo.write_bytes(image_bytes((600, 800)))
        refs.append({"id": f"ig:{1000+i}", "caption": f"기존 게시물 {i} <script>자료</script>",
            "file": str(photo), "sha256": mc.digest(photo.read_bytes()),
            "permalink": "https://www.instagram.com/p/example/", "published_at": "2026-10-01"})
    reference = tmp_path / "references.json"
    mc.save_json(reference, {"shop": {"name": "codeit", "description": "대구 카페", "reference_notes": "", "color": "#76472e", "style": "과거 분석 절대 입력하지 않기"},
        "profile_version": 2, "previous_profile": {"summary": "과거 분석 절대 입력하지 않기"}, "references": refs})
    out = tmp_path / "pipeline"
    manifest = pc.prepare(reference, out, path)
    settings = Settings(data_dir=tmp_path / "data", openai_enabled=True, image_enabled=True,
        openai_key="mock-only-secret", ai_budget_usd=Decimal("5"))
    return out, manifest, settings


def handler(request):
    payload = json.loads(request.content)
    if payload.get("text", {}).get("format", {}).get("name") != "style":
        return content_handler(request)
    if request.url.path.endswith("input_tokens"):
        return httpx.Response(200, json={"input_tokens": 3000})
    assert request.url.path == "/v1/responses"
    assert len(payload["input"][0]["content"]) == 9
    assert payload["max_output_tokens"] == 2048 and payload["reasoning"] == {"effort": "none"}
    model = payload["model"]
    result = {"summary": "따뜻한 기존 피드 " + model,
        "attributes": {k: model + "의 관찰한 특징" for k in FIELDS},
        "evidence": ["ig:1000 색감, ig:1001 말투"], "uncertainties": ["정확한 조명 기구 미확인"]}
    return httpx.Response(200, json={"id": "mock-style", "status": "completed", "service_tier": "default",
        "usage": {"input_tokens": 3000, "output_tokens": 200, "input_tokens_details": {"cached_tokens": 0}},
        "output": [{"content": [{"type": "output_text", "text": json.dumps(result)}]}]})


def test_freezes_same_real_feed_and_twenty_human_rows(pipeline, capsys):
    out, manifest, settings = pipeline
    style = mc.frozen_manifest(out / "styles")
    payloads = [mc.read_json(out / "styles" / s["request"]) for s in style["samples"]]
    for payload in payloads:
        payload.pop("model")
        assert "과거 분석 절대 입력하지 않기" not in json.dumps(payload, ensure_ascii=False)
        data = json.loads(payload["input"][0]["content"][0]["text"])
        assert [r["image_index"] for r in data["past_post_examples"]] == list(range(1, 9))
    assert payloads[0] == payloads[1]
    assert len(manifest["samples"]) == len(csv_rows(out / "reviews.csv")) == 20
    assert len(csv_rows(out / "bundles.csv")) == 4
    assert len(csv_rows(out / "preferences.csv")) == 2
    assert "T01" not in csv_rows(out / "reviews.csv")[0]
    pc.main(["run", str(out)])
    assert "유료 호출 없음" in capsys.readouterr().out and not settings.data_dir.exists()


def test_full_pipeline_holds_downstream_models_and_only_changes_style(pipeline):
    out, manifest, settings = pipeline
    calls = []
    def counted(request):
        calls.append(request.url.path)
        return handler(request)
    with httpx.Client(transport=httpx.MockTransport(counted)) as client:
        assert pc.run(out, settings, client) == 22
        assert pc.run(out, settings, client) == 0
    assert len(calls) == 34
    assert calls.count("/v1/responses/input_tokens") == 12
    assert calls.count("/v1/images/edits") == 10
    content = mc.frozen_manifest(out / "content")
    assert len(content["samples"]) == 20
    for case in manifest["cases"]:
        for kind, model in (("text", "gpt-5.4-mini"), ("image", "gpt-image-2")):
            selected = [s for s in content["samples"] if s["case"] == case["id"] and s["kind"] == kind]
            payloads = [mc.read_json(out / "content" / s["request"]) for s in selected]
            assert len(payloads) == 2 and all(p["model"] == model for p in payloads)
            if kind == "image":
                assert payloads[0]["images"] == payloads[1]["images"]
                assert payloads[0]["prompt"] != payloads[1]["prompt"]
                for p in payloads:
                    p.pop("prompt")
            else:
                datas = [json.loads(p["input"][0]["content"][0]["text"]) for p in payloads]
                assert datas[0]["confirmed_style"] != datas[1]["confirmed_style"]
                for data in datas:
                    data.pop("confirmed_style")
                    data.pop("resolved_visual_style")
                assert datas[0] == datas[1]
                for p in payloads:
                    p["input"][0]["content"][0]["text"] = "style-only-difference"
            assert payloads[0] == payloads[1]
    with connect(settings) as con:
        assert con.execute("SELECT COUNT(*) FROM ai_calls").fetchone()[0] == 22
        for table in ("style_profiles", "recommendations", "credits", "posts", "history"):
            assert con.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] == 0
    result = mc.read_json(out / "results.json")
    assert all(r["unreviewed"] == 5 and r["passed"] == 0 for r in result["summary"] if r["stage"] != "스타일 분석")
    assert all(p["planned"] == p["completed"] == 11 and p["cost_per_passed_post_usd"] == "" for p in result["paths"])
    assert sum(Decimal(p["known_cost_usd"]) for p in result["paths"]) == sum(Decimal(s["known_cost_usd"]) for s in result["summary"])
    page = (out / "review.html").read_text()
    assert "<script>" not in page and "&lt;script&gt;" in page
    # 실제 모델은 가려지며, 시험용 응답에 모델명을 넣은 위 mock 내용만 별도로 제외한다.
    rows = csv_rows(out / "reviews.csv")
    for row in rows:
        kind = next(k for k, v in pc.KINDS.items() if v == row["영역"])
        row.update(dict.fromkeys(pc.CRITERIA[kind], "충족"))
    mc.write_csv(out / "reviews.csv", rows, pc.REVIEW_FIELDS)
    pc.report(out)
    assert all(p["passed_post_pairs"] == 5 and p["cost_per_passed_post_usd"] for p in mc.read_json(out / "results.json")["paths"])
    row = next(r for r in rows if r["영역"] == "이미지")
    row[pc.CRITERIA["image"][0]] = "부분 충족"
    mc.write_csv(out / "reviews.csv", rows, pc.REVIEW_FIELDS)
    with pytest.raises(ValueError, match="이유"):
        pc.report(out)
    row["reason"] = "메인 쿠키가 다른 모양"
    mc.write_csv(out / "reviews.csv", rows, pc.REVIEW_FIELDS)
    pc.report(out)
    assert sum(p["passed_post_pairs"] for p in mc.read_json(out / "results.json")["paths"]) == 9
    bundle = csv_rows(out / "bundles.csv")
    bundle[0][pc.BUNDLE_GRADES[1]] = "부분 충족"
    mc.write_csv(out / "bundles.csv", bundle, pc.BUNDLE_FIELDS)
    with pytest.raises(ValueError, match="이유"):
        pc.report(out)


def test_style_failure_stops_without_using_previous_profile_or_repeating(pipeline):
    out, manifest, settings = pipeline
    calls = []
    def failed(request):
        calls.append(request.url.path)
        response = handler(request)
        if request.url.path == "/v1/responses":
            body = response.json()
            body["status"] = "incomplete"
            return httpx.Response(200, json=body)
        return response
    with httpx.Client(transport=httpx.MockTransport(failed)) as client:
        assert pc.run(out, settings, client) == 2
        assert pc.run(out, settings, client) == 0
    assert not (out / "content/manifest.json").exists() and len(calls) == 4
    assert all(pc.state(out, "styles", s)["state"] == "failed" for s in manifest["style_slots"])


def test_unknown_and_dependency_changes_block_new_paid_calls(pipeline):
    out, manifest, settings = pipeline
    def unknown(request):
        if request.url.path.endswith("input_tokens"):
            return handler(request)
        raise httpx.ReadTimeout("mock")
    with httpx.Client(transport=httpx.MockTransport(unknown)) as client:
        assert pc.run(out, settings, client) == 1
        with pytest.raises(ValueError, match="불명"):
            pc.run(out, settings, client)
    sample = next(s for s in manifest["style_slots"] if mc.record_path(out / "styles", s).exists())
    mc.settle(out / "styles", sample["id"], "0.01", "모의 청구 확인", settings, reporter=lambda _: None)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert pc.run(out, settings, client) == 1
    assert not (out / "content/manifest.json").exists()
    photo = out / manifest["references"][0]["image"]
    photo.write_bytes(b"changed")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="변경"):
            pc.run(out, settings, client)


def test_content_is_bound_to_analysis_output(pipeline):
    out, manifest, settings = pipeline
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        mc.run(out / "styles", settings, client, reporter=lambda _: None)
    for name in ("content", "content/requests", "content/samples"):
        (out / name).mkdir()
    pc.prepare_content(out, manifest)
    output = out / "styles/samples" / manifest["style_slots"][0]["id"] / "output.json"
    output.write_text("{}")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="변경"):
            pc.run(out, settings, client)
    with connect(settings) as con:
        assert con.execute("SELECT COUNT(*) FROM ai_calls").fetchone()[0] == 2
