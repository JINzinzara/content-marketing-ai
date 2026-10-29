"""실제 인사이트 계산의 결측·제외·불변성·방향성 계약 검사."""

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend import analytics


def fixture_rows(specs):
    result = []
    published = datetime(2026, 10, 19, tzinfo=timezone.utc)
    for item in specs:
        values = {
            "reach": item.get("reach", 100),
            "saved": item.get("saved", 10),
            "shares": item.get("shares", 1),
        }
        result.append(
            {
                "media_id": item["id"],
                "layout": "split",
                "purpose": item.get("purpose", "상품 소개"),
                "promotion": item.get("promotion", "organic"),
                "product_name": "평가용 상품",
                "media_product_type": item.get("format", "FEED"),
                "published_at": published.isoformat(),
                "observed_at": (
                    published + timedelta(hours=item.get("elapsed", 24))
                ).isoformat(),
                "metrics": {
                    key: {
                        "value": value,
                        "status": item.get(key + "_status", "available"),
                    }
                    for key, value in values.items()
                },
            }
        )
    return result


CASES = json.loads((Path(__file__).parent / "fixtures/insights.json").read_text())["cases"]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_insight_contract(settings, case):
    settings.comparison_hours, settings.comparison_tolerance_hours = 24, 2
    specs = copy.deepcopy(case["rows"])
    before = analytics.analyze(fixture_rows(specs), settings)
    mutation = case.get("mutation")
    if mutation == "reverse":
        specs.reverse()
    elif mutation == "add_lineup":
        specs += [{"id": "excluded-1", "purpose": "라인업"},
                  {"id": "excluded-2", "purpose": "라인업"}]
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
    for key, expected in case.get("expected_rates", {}).items():
        if expected is None:
            assert rates[key] is None
        else:
            assert rates[key] == pytest.approx(expected)
    if "expected_evidence" in case:
        assert sorted(row["media_id"] for row in after["evidence"]) == sorted(case["expected_evidence"])
    if case["type"] == "INV":
        if mutation == "scale":
            # 원시 수치는 근거에 보존하므로 달라져도 비율·해석은 같아야 한다.
            assert [(row["media_id"], row["save_rate"], row["share_rate"]) for row in before["evidence"]] == [(row["media_id"], row["save_rate"], row["share_rate"]) for row in after["evidence"]]
        else:
            assert before["evidence"] == after["evidence"]
        assert before["briefing"] == after["briefing"]
    if mutation == "increase_saved":
        assert rates["a"] == pytest.approx(case["expected_changed_rate"])
        assert rates["a"] > next(row["save_rate"] for row in before["latest"] if row["media_id"] == "a")
    if mutation == "remove_reach":
        assert not after["evidence"] and not after["briefing"]["cards"]
    for card in after["briefing"]["cards"]:
        for identifier, rate in card["rates"].items():
            assert f"{identifier}: 저장률 {rate * 100:.4g}%" in card["observation"]
