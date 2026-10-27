import csv
import subprocess
import sys

import pytest

from evaluations.report import FIELDS, compare, read_records


def record(method="manual", **changes):
    return {"case_id": "test-case", "method": method, "evaluator": "test-evaluator",
            "version": "test-version", "seconds": "100", "text_edits": "1",
            "image_edits": "0", "satisfaction": "4", "facts_ok": "yes",
            "style_ok": "yes", "product_ok": "yes", "usable": "yes", "notes": "",
            **changes}


def save(tmp_path, rows):
    path = tmp_path / "records.csv"
    with path.open("w", newline="") as source:
        writer = csv.DictWriter(source, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return path


def test_real_record_comparison_and_negative_savings(tmp_path):
    path = save(tmp_path, [record(), record("service", seconds="80")])
    result = compare(read_records(path))[0]
    assert result["status"] == "comparable"
    assert result["time_reduction_percent"] == 20
    path = save(tmp_path, [record(), record("service", seconds="125")])
    assert compare(read_records(path))[0]["time_reduction_percent"] == -25


@pytest.mark.parametrize("changes,status", [
    ({"facts_ok": "no"}, "quality_not_met"),
    ({"product_ok": "no"}, "quality_not_met"),
    ({"style_ok": "unreviewed"}, "awaiting_review"),
    ({"evaluator": "other-evaluator"}, "missing_pair"),
])
def test_failures_and_unmatched_people_are_not_claimed_as_savings(tmp_path, changes, status):
    results = compare(read_records(save(tmp_path, [record(), record("service", **changes)])))
    assert all(row["status"] == status for row in results)
    assert all(row["time_reduction_percent"] == "" for row in results)


@pytest.mark.parametrize("key,value", [
    ("seconds", "nan"), ("seconds", "-1"), ("seconds", "0"),
    ("text_edits", "-1"), ("image_edits", "1.5"),
    ("satisfaction", "6"), ("facts_ok", "maybe"), ("version", ""),
])
def test_invalid_measurements_are_rejected(tmp_path, key, value):
    with pytest.raises(ValueError):
        read_records(save(tmp_path, [record(**{key: value})]))


def test_duplicate_records_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="중복"):
        read_records(save(tmp_path, [record(), record()]))


def test_empty_csv_has_no_fabricated_results_and_cli_reads_only(tmp_path):
    path = save(tmp_path, [])
    before = path.read_bytes()
    result = subprocess.run([sys.executable, "-m", "evaluations", str(path)],
                            capture_output=True, text=True, check=True)
    assert list(csv.DictReader(result.stdout.splitlines())) == []
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


def test_csv_export_does_not_execute_spreadsheet_formulas(tmp_path):
    path = save(tmp_path, [record(case_id="=1+1")])
    result = subprocess.run([sys.executable, "-m", "evaluations", str(path)],
                            capture_output=True, text=True, check=True)
    assert next(csv.DictReader(result.stdout.splitlines()))["case_id"] == "'=1+1"


def test_app_has_no_experiment_routes_or_menu(client):
    assert client.get("/experiments").status_code == 404
    response = client.get("/")
    assert response.status_code == 200 and "실험 기록" not in response.text
