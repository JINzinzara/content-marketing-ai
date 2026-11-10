"""판정기 규칙과 유료 호출 원장 기록을 고정 문자열·모의 HTTP로 확인"""

import json
from dataclasses import replace

import httpx

from backend.db import connect, initialize
from backend.eval_judge import call_judge, judge_payload, kappa, rule_text

GOOD = "스푸키쿠키 👻\n할로윈 쿠키예요.\n10월 26일부터 11월 6일까지 한정 판매됩니다.\n\n가볍게 즐겨주세요."
TAGS = "#할로윈 #카페 #대구카페 #쿠키 #스푸키쿠키"


def grades(caption, hashtags=TAGS):
    return {k: v[0] for k, v in rule_text(caption, hashtags).items()}


def test_rule_judge_grades():
    assert set(grades(GOOD).values()) == {"충족"}
    # 본문 끝에 태그 묶음을 다시 노출하면 고유 태그는 같아도 T09 부분 충족, 문단 수에는 넣지 않는다.
    dup = grades(GOOD + "\n\n" + TAGS)
    assert dup["T09"] == "부분 충족" and dup["T06"] == "충족" and dup["T08"] == "충족"
    assert grades(GOOD.replace("11월 6일", "11월 7일"))["T03"] == "부분 충족"
    assert grades("🎃 " + GOOD)["T05"] == "충족" and grades("할로윈 " + GOOD)["T05"] == "미충족"
    assert grades(GOOD.replace("\n\n", "\n"))["T06"] == "미충족"
    assert grades(GOOD, "#할로윈 #카페 #쿠키 #a")["T08"] == "부분 충족"
    assert grades(GOOD, TAGS + " #a #b")["T09"] == "미충족"


def test_kappa_known_values():
    assert abs(kappa([1, 1, 0, 0], [1, 0, 0, 0]) - 0.5) < 1e-9
    assert kappa(list("aaaa"), list("aaaa")) is None


def test_call_judge_reserves_and_settles(settings, tmp_path):
    settings = replace(settings, openai_key="test", ai_budget_usd=5)
    settings.data_dir.mkdir(parents=True)
    initialize(settings)
    verdict = {"reason": "근거", "grade": "충족"}
    result = {"T02": verdict, "T04": verdict, "T07": verdict}

    def handler(request):
        if request.url.path.endswith("/input_tokens"):
            return httpx.Response(200, json={"input_tokens": 1000})
        return httpx.Response(200, json={
            "id": "resp", "status": "completed", "service_tier": "default",
            "usage": {"input_tokens": 1000, "output_tokens": 100, "input_tokens_details": {"cached_tokens": 0}},
            "output": [{"content": [{"type": "output_text", "text": json.dumps(result, ensure_ascii=False)}]}]})

    rates = {"input": "2.50", "cached_input": "0.25", "output": "15.00"}
    payload = judge_payload("text", {"post": "x"})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        record = call_judge(tmp_path / "s-1.json", payload, "text", rates, "judge:test:s:1", settings, client)
    assert record["state"] == "completed" and record["result"] == result
    assert record["cost_microusd"] == 4000 and record["reserved_microusd"] == 2500 + 9000
    with connect(settings) as con:
        row = con.execute("SELECT state, cost_microusd FROM ai_calls WHERE purpose='judge:test:s:1'").fetchone()
    assert tuple(row) == ("completed", 4000)
