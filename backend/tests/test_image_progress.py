"""상태 조회의 소유권·실측 시간·모델 설정 구분과 추가 AI 호출 방지 검증"""

import json
from decimal import Decimal
from types import SimpleNamespace

from backend.db import connect, ensure_account


def test_progress_uses_matching_history_without_starting_requests(app, client, new_post, settings, monkeypatch):
    now = 2_000_000_000
    monkeypatch.setattr("backend.image_ai.time", SimpleNamespace(time=lambda: now))
    settings.image_enabled, settings.openai_key = True, "not-a-real-key"
    settings.ai_budget_usd, settings.image_reserve_usd = Decimal("1"), Decimal("0.1")
    settings.image_text_rate, settings.image_input_rate, settings.image_output_rate = Decimal("5"), Decimal("8"), Decimal("30")
    current, history, different, foreign = [new_post(preset_id="minimal") for _ in range(4)]
    assert client.get(f"/posts/{current}/image-progress").json() == {"job": None}
    for identifier, duration, quality in ((history, 40, "low"), (different, 400, "high")):
        job = app.state.image_ai.queue("local-owner", identifier, 1)
        with connect(settings, write=True) as con:
            parameters = json.loads(con.execute("SELECT parameters FROM image_generations WHERE id=?", (job,)).fetchone()[0])
            parameters["quality"] = quality
            con.execute("UPDATE image_generations SET state='completed', created_at=?, completed_at=?, parameters=? WHERE id=?",
                        (now - duration - 10, now - 10, json.dumps(parameters), job))
    job = app.state.image_ai.queue("local-owner", current, 1)
    first = client.get(f"/posts/{current}/image-progress").json()["job"]
    assert first["elapsed_seconds"] == 0 and first["expected_seconds"] == 40 and first["sample_count"] == 1
    with connect(settings, write=True) as con:
        con.execute("UPDATE image_generations SET state='running', created_at=? WHERE id=?", (now - 7, job))
        ensure_account(con, "someone-else")
        con.execute("UPDATE posts SET owner_id='someone-else' WHERE id=?", (foreign,))
    second = client.get(f"/posts/{current}/image-progress").json()["job"]
    assert second["state"] == "running" and second["elapsed_seconds"] == 7
    assert set(second) == {"id", "state", "message", "created_at", "elapsed_seconds", "expected_seconds", "sample_count"}
    html = client.get(f"/posts/{current}").text
    assert 'data-state="running"' in html and 'class="spinner"' in html and "약 40초 · 참고용" in html
    assert client.get(f"/posts/{foreign}/image-progress").status_code == 404
    assert client.get("/posts/missing/image-progress").status_code == 404
    with connect(settings) as con:
        assert con.execute("SELECT COUNT(*) FROM image_generations").fetchone()[0] == 3


def test_first_image_has_no_invented_time_estimate(app, client, new_post, settings):
    identifier = new_post(preset_id="minimal")
    settings.image_enabled, settings.openai_key = True, "not-a-real-key"
    settings.ai_budget_usd, settings.image_reserve_usd = Decimal("1"), Decimal("0.1")
    settings.image_text_rate, settings.image_input_rate, settings.image_output_rate = Decimal("5"), Decimal("8"), Decimal("30")
    app.state.image_ai.queue("local-owner", identifier, 1)
    job = client.get(f"/posts/{identifier}/image-progress").json()["job"]
    assert job["expected_seconds"] is None and job["sample_count"] == 0


def test_generation_limit_is_visible_and_uses_the_same_rule_as_queue(app, client, new_post, settings):
    from fastapi import HTTPException
    import pytest
    from test_image_generation import enable_image

    identifier = new_post(preset_id="natural")
    enable_image(settings)
    settings.image_attempts_per_post = 1
    app.state.image_ai.queue("local-owner", identifier, 1)
    with connect(settings, write=True) as con:
        con.execute("UPDATE image_generations SET state='failed'")
    assert "게시물 생성 한도 도달" in client.get(f"/posts/{identifier}").text
    with pytest.raises(HTTPException) as exc:
        app.state.image_ai.queue("local-owner", identifier, 1)
    assert exc.value.status_code == 429 and exc.value.detail == "게시물 생성 한도 도달"


def test_automatic_estimate_excludes_previous_mask_work(app, client, new_post, settings, monkeypatch):
    from test_image_generation import enable_image
    enable_image(settings)
    now = 2_000_000_000
    monkeypatch.setattr("backend.image_ai.time", SimpleNamespace(time=lambda: now))
    for mode, duration in (("masked", 400), ("automatic", 50)):
        identifier = new_post(preset_id="natural")
        job = app.state.image_ai.queue("local-owner", identifier, 1)
        with connect(settings, write=True) as con:
            parameters = json.loads(con.execute("SELECT parameters FROM image_generations WHERE id=?", (job,)).fetchone()[0])
            parameters["edit_mode"] = mode  # 과거 마스크 작업 이력을 재현한다.
            con.execute("UPDATE image_generations SET state='completed', created_at=?, completed_at=?, parameters=? WHERE id=?", (now-duration-10, now-10, json.dumps(parameters), job))
    identifier = new_post(preset_id="natural")
    app.state.image_ai.queue("local-owner", identifier, 1)
    job = client.get(f"/posts/{identifier}/image-progress").json()["job"]
    assert job["expected_seconds"] == 50 and job["sample_count"] == 1
