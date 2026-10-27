"""사람이 수행한 같은 과제의 수작업·서비스 기록만 CSV로 비교한다."""

import argparse
import csv
import math
from pathlib import Path
import sys

FIELDS = [
    "case_id",
    "scenario",
    "method",
    "evaluator",
    "version",
    "seconds",
    "satisfaction",
    "facts_ok",
    "style_ok",
    "product_ok",
    "usable",
    "notes",
]


def read_records(path):
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames != FIELDS:
            raise ValueError("열 이름을 records.example.csv와 같게 작성해주세요")
        records, seen = [], set()
        for line, values in enumerate(reader, 2):
            if None in values or any(value is None for value in values.values()):
                raise ValueError(f"{line}행: 열 개수를 확인해주세요")
            row = {key: value.strip() for key, value in values.items()}
            try:
                if not all(row[key] for key in ("case_id", "evaluator", "version")):
                    raise ValueError("과제·평가자·버전을 작성해주세요")
                if row["method"] not in {"manual", "service"}:
                    raise ValueError("method는 manual 또는 service입니다")
                if row["scenario"] not in {"first_use", "repeat_use"}:
                    raise ValueError("scenario는 first_use 또는 repeat_use입니다")
                row["seconds"] = float(row["seconds"])
                if not math.isfinite(row["seconds"]) or row["seconds"] <= 0:
                    raise ValueError("seconds는 실제 측정한 양수여야 합니다")
                row["satisfaction"] = int(row["satisfaction"])
                if not 1 <= row["satisfaction"] <= 5:
                    raise ValueError("만족도는 1~5로 작성해주세요")
                for key in ("facts_ok", "style_ok", "product_ok", "usable"):
                    choices = {"yes", "no", "unreviewed"}
                    if key == "product_ok":
                        choices.add("na")
                    if row[key] not in choices:
                        raise ValueError(f"{key}의 판정 값을 확인해주세요")
                key = (row["case_id"], row["scenario"], row["evaluator"], row["method"])
                if key in seen:
                    raise ValueError("같은 과제·평가자·방식이 중복됐습니다")
                seen.add(key)
                records.append(row)
            except ValueError as error:
                raise ValueError(f"{line}행: {error}") from error
    return records


def compare(records):
    pairs = {}
    for row in records:
        pairs.setdefault((row["case_id"], row["scenario"], row["evaluator"]), {})[
            row["method"]
        ] = row
    results = []
    for (case_id, scenario, evaluator), pair in pairs.items():
        result = {"case_id": case_id, "scenario": scenario, "evaluator": evaluator}
        for method in ("manual", "service"):
            row = pair.get(method, {})
            for key in ("version", "seconds", "satisfaction", "style_ok"):
                result[f"{method}_{key}"] = row.get(key, "")
        status, reduction = "missing_pair", ""
        if len(pair) == 2:
            verdicts = [
                row[key]
                for row in pair.values()
                for key in ("facts_ok", "product_ok", "usable")
            ]
            # 일관성 차이는 비교 결과이므로 낮은 평가를 이유로 시간 비교에서 빼지 않는다.
            if "no" in verdicts:
                status = "quality_not_met"
            elif "unreviewed" in verdicts:
                status = "awaiting_review"
            else:
                status = "comparable"
                reduction = round(
                    (pair["manual"]["seconds"] - pair["service"]["seconds"])
                    / pair["manual"]["seconds"]
                    * 100,
                    2,
                )
        result.update(status=status, time_reduction_percent=reduction)
        results.append(result)
    return results


def main():
    parser = argparse.ArgumentParser(description="실제 수작업·앱 사용 기록 비교")
    parser.add_argument(
        "csv", nargs="?", type=Path, default=Path(__file__).with_name("records.csv")
    )
    args = parser.parse_args()
    try:
        rows = compare(read_records(args.csv))
    except (OSError, ValueError) as error:
        parser.error(str(error))
    fields = (
        ["case_id", "scenario", "evaluator"]
        + [
            f"{method}_{key}"
            for method in ("manual", "service")
            for key in ("version", "seconds", "satisfaction", "style_ok")
        ]
        + ["status", "time_reduction_percent"]
    )
    writer = csv.DictWriter(sys.stdout, fieldnames=fields)
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                key: (
                    "'" + value
                    if isinstance(value, str) and value.startswith(("=", "+", "-", "@"))
                    else value
                )
                for key, value in row.items()
            }
        )
