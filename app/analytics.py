"""지표의 결측·게시 경과 시간·한국 시간대를 해석하고 비교 근거 생성"""

import hashlib
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .db import connect

METRICS = {
    "likes": "좋아요",
    "comments": "댓글",
    "saved": "저장",
    "shares": "공유",
    "views": "조회",
    "reach": "도달",
}
STATUSES = {
    "available": "확인",
    "pending": "미집계",
    "unsupported": "미지원",
    "error": "조회 실패",
}


def parse_time(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else None
    except (ValueError, AttributeError):
        return None


def snapshot_info(row, settings):
    metrics = json.loads(row["metrics"])
    published, observed = parse_time(row["published_at"]), parse_time(
        row["observed_at"]
    )
    elapsed = (
        (observed - published).total_seconds() / 3600
        if published and observed and observed >= published
        else None
    )
    get = lambda key: (
        metrics.get(key, {}).get("value")
        if metrics.get(key, {}).get("status") == "available"
        else None
    )
    reach, saved = get("reach"), get("saved")
    rate = (
        saved / reach if reach is not None and reach > 0 and saved is not None else None
    )
    local = published.astimezone(ZoneInfo("Asia/Seoul")) if published else None
    return {
        **dict(row),
        "metrics": metrics,
        "elapsed_hours": elapsed,
        "save_rate": rate,
        "local_time": local.strftime("%Y-%m-%d %H:%M") if local else "시각 미확인",
        "weekday": local.weekday() if local else None,
        "hour": local.hour if local else None,
        "in_window": elapsed is not None
        and abs(elapsed - settings.comparison_hours)
        <= settings.comparison_tolerance_hours,
    }


def report(settings, owner_id):
    with connect(settings) as con:
        rows = con.execute(
            """SELECT o.*, p.layout, p.purpose, p.promotion, p.product_name, p.published_snapshot, h.caption
            FROM observations o LEFT JOIN posts p ON p.ig_media_id=o.media_id AND p.owner_id=o.owner_id
            LEFT JOIN history h ON h.media_id=o.media_id AND h.owner_id=o.owner_id
            WHERE o.owner_id=? ORDER BY o.id DESC
    """,
            (owner_id,),
        ).fetchall()
    all_observations = []
    for row in rows:
        values = dict(row)
        if values.get("published_snapshot"):
            published = json.loads(values["published_snapshot"])
            for key in ("layout", "purpose", "product_name", "promotion"):
                values[key] = published.get(key)
        all_observations.append(snapshot_info(values, settings))
    latest, seen = [], set()
    for row in all_observations:
        if row["media_id"] not in seen:
            latest.append(row)
            seen.add(row["media_id"])
    candidates = {}
    for row in all_observations:
        if (
            row["in_window"]
            and row["promotion"] == "organic"
            and row["layout"]
            and row["save_rate"] is not None
        ):
            current = candidates.get(row["media_id"])
            distance = abs(row["elapsed_hours"] - settings.comparison_hours)
            if current is None or distance < abs(
                current["elapsed_hours"] - settings.comparison_hours
            ):
                candidates[row["media_id"]] = row
    groups = {}
    for row in candidates.values():
        groups.setdefault(row["purpose"], []).append(row)
    comparable = [row for group in groups.values() if len(group) >= 2 for row in group]
    evidence = [
        {
            k: row[k]
            for k in (
                "media_id",
                "observed_at",
                "elapsed_hours",
                "save_rate",
                "layout",
                "purpose",
                "product_name",
                "weekday",
                "hour",
            )
        }
        for row in comparable
    ]
    digest = hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()
    return {
        "latest": latest,
        "evidence": evidence,
        "evidence_hash": digest,
        "limitations": "한국 시간 기준 관찰입니다. "
        "같은 목적·유료 홍보가 없는 게시물만 정한 경과 시간 범위에서 비교합니다. "
        "상품·행사·목표 차이가 남으므로 시간·형식의 인과 효과나 매출 상승으로 단정하지 않습니다.",
    }
