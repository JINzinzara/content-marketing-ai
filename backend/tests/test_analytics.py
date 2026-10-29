"""결측과 0·게시 시간·같은 경과 시간과 발행 당시의 조건 검증"""

import json
from datetime import datetime, timedelta, timezone

from backend.analytics import report, snapshot_info
from backend.db import connect


def observation(*, elapsed=24, saved=0, reach=100, status="available"):
    posted = datetime(2026, 10, 18, 15, 0, tzinfo=timezone.utc)
    return {
        "media_id": "test",
        "metrics": json.dumps(
            {
                "saved": {"value": saved, "status": status},
                "reach": {"value": reach, "status": "available"},
            }
        ),
        "published_at": posted.isoformat(),
        "observed_at": (posted + timedelta(hours=elapsed)).isoformat(),
    }


def test_seoul_day_rollover_zero_and_missing_are_distinct(settings):
    real_zero = snapshot_info(observation(saved=0), settings)
    assert real_zero["local_time"] == "2026-10-19 00:00"
    assert real_zero["weekday"] == 0 and real_zero["hour"] == 0
    assert real_zero["save_rate"] == 0
    assert (
        snapshot_info(observation(saved=None, status="pending"), settings)["save_rate"]
        is None
    )
    assert snapshot_info(observation(reach=0), settings)["save_rate"] is None
    assert not snapshot_info(observation(elapsed=72), settings)["in_window"]
    assert snapshot_info(observation(elapsed=-1), settings)["elapsed_hours"] is None


def test_comparison_uses_historical_window_and_excludes_paid_and_unknown(
    client, new_post, settings
):
    ids = [new_post() for _ in range(4)]
    with connect(settings, write=True) as con:
        for index, identifier in enumerate(ids):
            promotion = ["organic", "organic", "paid", "unknown"][index]
            media_id = "media-" + str(index)
            con.execute(
                "UPDATE posts SET ig_media_id=?, promotion=? WHERE id=?",
                (media_id, promotion, identifier),
            )
            for elapsed in (24, 72):
                row = observation(elapsed=elapsed, saved=index + 1)
                con.execute(
                    "INSERT INTO observations(owner_id, media_id, observed_at, published_at, metrics) VALUES ('local-owner', ?, ?, ?, ?)",
                    (media_id, row["observed_at"], row["published_at"], row["metrics"]),
                )
    result = report(settings, "local-owner")
    assert len(result["latest"]) == 4
    assert all(x["elapsed_hours"] == 72 for x in result["latest"])
    assert len(result["evidence"]) == 2
    assert all(x["elapsed_hours"] == 24 for x in result["evidence"])
    assert {x["media_id"] for x in result["evidence"]} == {"media-0", "media-1"}


def test_published_layout_preserved_after_later_local_edit(client, new_post, settings):
    identifier = new_post()
    with connect(settings, write=True) as con:
        con.execute(
            "UPDATE posts SET ig_media_id='published', layout='split', promotion='organic', published_snapshot=? WHERE id=?",
            (
                json.dumps(
                    {
                        "layout": "frame",
                        "purpose": "신메뉴 안내",
                        "product_name": "발행한 상품",
                        "promotion": "paid",
                    }
                ),
                identifier,
            ),
        )
        row = observation()
        con.execute(
            "INSERT INTO observations(owner_id, media_id, observed_at, published_at, metrics) VALUES ('local-owner', 'published', ?, ?, ?)",
            (row["observed_at"], row["published_at"], row["metrics"]),
        )
    result = report(settings, "local-owner")
    assert result["latest"][0]["layout"] == "frame"
    assert result["latest"][0]["product_name"] == "발행한 상품"
    assert result["latest"][0]["promotion"] == "paid"


def test_invalid_or_naive_timestamp_not_inferred(settings):
    row = observation()
    row["published_at"] = "2026-10-19T10:00:00"
    value = snapshot_info(row, settings)
    assert value["local_time"] == "시각 미확인" and value["elapsed_hours"] is None
