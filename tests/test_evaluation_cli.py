"""CLI가 서비스 UI·라우터·실험 DB 없이 실행·계측·결과 보존하는지 검사한다."""

import copy
import csv
import json
import subprocess
import sys
import zipfile
from decimal import Decimal

import httpx
import pytest

from app.db import initialize
from evaluations.measurement import fill_blanks
from evaluations.run import Store, VERSIONS, load_dataset, paired_metrics, record_review, run_automated, run_check, run_live, sha, write_report


@pytest.fixture(autouse=True)
def isolate_results(settings, tmp_path):
    settings.evaluation_dir = tmp_path / "evaluation-results"


@pytest.mark.parametrize("case", load_dataset("checklist-v1.json")["cases"], ids=lambda case: case["id"])
def test_frozen_checklist_contract(settings, case):
    result = run_check(settings, case)
    assert result["passed"], result["checks"]
    assert result["model"] is None and result["llm_evaluation"] is None


def test_automated_results_preserve_source_and_never_create_app_database(settings):
    settings.openai_key = "secret-do-not-export"
    run, folder = run_automated(settings)
    assert sum(row["passed"] is True for row in run["results"]) == 23
    assert sum(row["passed"] is None for row in run["results"]) == 3
    assert run["human_task_ms"] is None and run["human_review"] is None
    assert not settings.data_dir.exists()
    assert not list(settings.evaluation_dir.rglob("*.sqlite3"))
    assert "secret-do-not-export" not in (folder / "run.json").read_text()
    assert run["manifest"]["variants"]["baseline-v1"]["role"] == "control"
    archive = settings.evaluation_dir / run["manifest"]["source_archive"]
    with zipfile.ZipFile(archive) as zipped:
        assert "evaluations/run.py" in zipped.namelist()
        assert not any(".env" in name or "data/" in name or "results/" in name for name in zipped.namelist())
        assert {name: sha(zipped.read(name)) for name in zipped.namelist()} == run["manifest"]["source_files"]


def test_json_store_owner_separation_and_invalid_identifier(settings):
    store = Store(settings)
    run = store.create("one", "automated", "baseline-v1")
    with pytest.raises(FileNotFoundError):
        store.get("other", run["id"])
    with pytest.raises(ValueError):
        store.get("one", "../../.env")
    assert store.get("one", run["id"])["variant_definition"] == VERSIONS["baseline-v1"]


def test_blank_style_preserves_existing_and_ignores_whitespace():
    result = fill_blanks({"palette": "  ", "voice": " 존댓말 "},
                         {"palette": " 베이지 ", "voice": "반말"})
    assert result["palette"] == "베이지" and result["voice"] == "존댓말"


def test_paired_comparison_requires_human_quality_and_identical_conditions(settings):
    first = Store(settings).create("local-owner", "task", "baseline-v1", operator="human")
    first.update(pair_id="P01:lineup:1", state="completed", quality_passed=True, human_task_ms=10000)
    second = copy.deepcopy(first)
    second.update(id="other", variant="service-v1", human_task_ms=5000)
    assert paired_metrics([first, second])[0]["reduction_percent"] == 50
    second["operator"] = "agent"
    assert paired_metrics([first, second])[0]["reduction_percent"] is None
    second["operator"] = "human"
    second["photo_sha256"] = "different"
    assert paired_metrics([first, second])[0]["status"] == "different_conditions"
    assert paired_metrics([first, first, second])[0]["status"] == "ambiguous_duplicate_runs"


def test_live_batch_uses_existing_api_budget_and_keeps_reviews_separate(settings, monkeypatch):
    settings.prepare()
    initialize(settings)
    settings.openai_enabled, settings.openai_key = True, "never-export-this"
    settings.ai_budget_usd = Decimal("1")
    settings.input_ceiling_rate, settings.output_rate = Decimal("2.5"), Decimal("15")
    actual_client, captured = httpx.Client, []
    def handler(request):
        payload = json.loads(request.content)
        if request.url.path.endswith("input_tokens"):
            return httpx.Response(200, json={"input_tokens": 100})
        captured.append(payload)
        name = payload["text"]["format"]["name"]
        outputs = {
            "draft": {"title": "가게", "caption": "가게 소개입니다.", "hashtags": "", "layout": "split"},
            "style": {"summary": "자료의 스타일", "attributes": {key: "" for key in ("palette", "lighting", "background", "composition", "typography", "voice")}, "evidence": [], "uncertainties": []},
            "recommendation": {"observation": "관찰", "next_content": "다음 시도", "time_hypothesis": "보류", "limitations": "합성 자료"},
        }
        return httpx.Response(200, json={"id": "resp-mock", "status": "completed", "model": settings.openai_model,
            "usage": {"input_tokens": 100, "output_tokens": 80},
            "output": [{"content": [{"type": "output_text", "text": json.dumps(outputs[name])}]}]})
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: actual_client(transport=httpx.MockTransport(handler), **kwargs))
    summaries = run_live(settings)
    assert len(summaries) == 8 and all(row["output_received"] for row in summaries)
    assert captured[0]["model"] == captured[1]["model"]
    assert captured[0]["instructions"] != captured[1]["instructions"]
    first_input = json.loads(captured[0]["input"][0]["content"][0]["text"])
    second_input = json.loads(captured[1]["input"][0]["content"][0]["text"])
    assert "confirmed_style" not in first_input and "confirmed_style" in second_input
    store = Store(settings)
    runs = store.list("local-owner")
    assert len({run["manifest"]["code_sha256"] for run in runs}) == 1
    for run in runs:
        attempt = run["attempts"][0]
        assert run["human_task_ms"] is None and run["results"][0]["passed"] is None
        assert attempt["api_calls"][0]["cost_microusd"] > 0
        assert attempt["api_timing"]["generation_api_ms"] >= 0
        assert attempt["trace"]["response"]["id"] == "resp-mock"
        assert "never-export-this" not in (store.export("local-owner", run["id"]) / "run.json").read_text()
    style = next(run for run in runs if run["task"]["kind"] == "style")
    ratings = {"palette": "correct_fill", "lighting": "correct_blank", "background": "wrong_fill",
               "composition": "missed_fill", "typography": "unsupported_fill", "voice": "correct_fill"}
    original = copy.deepcopy(style["attempts"])
    store.update("local-owner", style["id"], lambda value: record_review(value, "P01", "peer", ratings, "자료와 대조한 항목별 판정"))
    reviewed = store.get("local-owner", style["id"])
    assert reviewed["attempts"] == original
    assert reviewed["human_review"]["metrics"]["fill_accuracy"] == 0.5
    assert reviewed["human_review"]["metrics"]["unsupported_fills"] == 1
    folder = write_report(settings)
    rows = list(csv.DictReader((folder / "api-runs.csv").open(encoding="utf-8-sig")))
    assert len(rows) == 8 and all(float(row["ledger_usd"]) > 0 for row in rows)
    assert "fill_accuracy" in (folder / "human-reviews.csv").read_text("utf-8-sig")


def test_cli_runs_without_webserver_and_exports_parseable_results(tmp_path):
    folder = tmp_path / "cli-results"
    result = subprocess.run([sys.executable, "-m", "evaluations", "run", "--results-dir", str(folder)],
                            capture_output=True, text=True, check=True)
    summary = json.loads(result.stdout)
    assert summary["passed"] == 23 and summary["pending"] == 3
    assert (folder / "exports" / summary["run_id"] / "metrics.csv").is_file()
    assert not list(folder.rglob("*.sqlite3"))


def test_app_has_no_experiment_routes_or_menu(client):
    assert client.get("/experiments").status_code == 404
    response = client.get("/")
    assert response.status_code == 200 and "실험 기록" not in response.text
