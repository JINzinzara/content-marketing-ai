"""보관된 비교 실험 요약과 앱 실측 기록에서 '사용 가능한 결과 1건' 기준 지표를 다시 계산한다.

기대 시도 수 = 1 / 합격률, 합격 1건당 비용 = 전체 비용 / 합격 수,
기대 대기 시간 = API 중앙값 × 기대 시도 수. 새 API 호출은 하지 않는다.

실행: python -m backend.kpi_report [저장소 루트]
"""

import csv
import sqlite3
import sys
from pathlib import Path

from .config import ROOT


def usable_metrics(passed, planned, cost_usd, median_s):
    """합격률이 0이면 기대값을 계산하지 않는다(무한대로 표시하지 않는다)."""
    if not passed:
        return None
    attempts = planned / passed
    return {"pass": f"{passed}/{planned}", "unit_cost": cost_usd / planned, "cost_per_pass": cost_usd / passed,
            "attempts": attempts, "wait_s": median_s * attempts if median_s else None}


def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def model_rows(runs):
    for r in read_csv(runs / "final-fixed-01" / "summary.csv"):
        m = usable_metrics(int(r["passed"]), int(r["planned"]), float(r["known_cost_usd"]), float(r["api_median_seconds"]))
        yield r["kind"], r["model"], m


def style_rows(runs):
    rows = read_csv(runs / "instagram-style-pipeline-01" / "summary.csv")
    for analyzer in sorted({r["analyzer"] for r in rows}):
        path = [r for r in rows if r["analyzer"] == analyzer]
        analysis = next(r for r in path if r["stage"] == "스타일 분석")
        text = next(r for r in path if r["stage"] == "문구")
        total = sum(float(r["known_cost_usd"]) for r in path)
        yield analyzer, {"analysis_cost": float(analysis["known_cost_usd"]), "analysis_s": float(analysis["api_median_seconds"]),
                         "text_pass": f'{text["passed"]}/{text["planned"]}', "path_cost": total}


def app_timings(db):
    if not db.exists():
        return []
    with sqlite3.connect(db) as con:
        return con.execute("SELECT action, elapsed_ms / 1000.0, revision FROM production_timings ORDER BY created_at").fetchall()


def fmt(v, digits=6):
    return "-" if v is None else f"{v:.{digits}f}"


def main(root):
    runs = root / "backend" / "comparison" / "runs"
    print("| 구분 | 모델 | 합격 | 1회 비용 | 합격 1건당 비용 | 기대 시도 수 | 기대 대기(초) |")
    print("|---|---|---|---|---|---|---|")
    for kind, model, m in model_rows(runs):
        print(f"| {kind} | {model} | {m['pass']} | {fmt(m['unit_cost'])} | {fmt(m['cost_per_pass'])} | {m['attempts']:.2f} | {fmt(m['wait_s'], 1)} |")
    print("\n| 스타일 분석 모델 | 분석 비용 | 분석 시간(초) | 후속 문구 합격 | 분석+후속 경로 비용 |")
    print("|---|---|---|---|---|")
    for analyzer, s in style_rows(runs):
        print(f"| {analyzer} | {fmt(s['analysis_cost'])} | {s['analysis_s']:.1f} | {s['text_pass']} | {fmt(s['path_cost'])} |")
    print("\n| 앱 실측 구간 | 초 | 버전 |")
    print("|---|---|---|")
    for action, sec, revision in app_timings(root / "data" / "instagram-test" / "app.sqlite3"):
        print(f"| {action} | {sec:.1f} | {revision} |")


def _check():
    m = usable_metrics(3, 5, 0.03725, 11.0)
    assert abs(m["cost_per_pass"] - 0.0124167) < 1e-6 and abs(m["attempts"] - 5 / 3) < 1e-9
    assert usable_metrics(0, 5, 0.1, 1.0) is None


if __name__ == "__main__":
    _check()
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT)
