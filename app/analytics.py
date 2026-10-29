"""지표의 결측·게시 경과 시간·한국 시간대를 해석하고 비교 근거 생성"""

import hashlib
import json
import math
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
VERSION = "insights-1.0.0"
PURPOSES = {
    "신메뉴 안내": "상품 소개", "상품 소개": "상품 소개",
    "시즌 안내": "시즌 안내", "매장 소식": "가게 소개",
    "가게 소개": "가게 소개", "행사 안내": "행사 안내",
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
    metrics = row["metrics"] if isinstance(row["metrics"], dict) else json.loads(row["metrics"])
    published, observed = parse_time(row["published_at"]), parse_time(
        row["observed_at"]
    )
    elapsed = (
        (observed - published).total_seconds() / 3600
        if published and observed and observed >= published
        else None
    )
    def get(key):
        item = metrics.get(key, {})
        value = item.get("value")
        return value if (item.get("status") == "available" and
            isinstance(value, (int, float)) and not isinstance(value, bool) and
            math.isfinite(value) and value >= 0) else None
    reach, saved = get("reach"), get("saved")
    rate = (
        saved / reach if reach is not None and reach > 0 and saved is not None else None
    )
    shares = get("shares")
    local = published.astimezone(ZoneInfo("Asia/Seoul")) if published else None
    return {
        **dict(row),
        "metrics": metrics,
        "elapsed_hours": elapsed,
        "save_rate": rate,
        "share_rate": shares / reach if reach is not None and reach > 0 and shares is not None else None,
        "local_time": local.strftime("%Y-%m-%d %H:%M") if local else "시각 미확인",
        "weekday": local.weekday() if local else None,
        "hour": local.hour if local else None,
        "in_window": elapsed is not None
        and abs(elapsed - settings.comparison_hours)
        <= settings.comparison_tolerance_hours,
    }


def analyze(rows, settings):
    """합성 자료와 실제 관측을 동일한 경로에서 분석한다. 외부 API는 호출하지 않는다."""
    all_observations = []
    for row in rows:
        values = dict(row)
        if values.get("published_snapshot"):
            published = json.loads(values["published_snapshot"])
            for key in ("layout", "purpose", "product_name", "promotion"):
                values[key] = published.get(key)
        all_observations.append(snapshot_info(values, settings))
    latest, seen = [], set()
    for row in sorted(all_observations, key=lambda x: x["observed_at"], reverse=True):
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
            and row.get("media_product_type", "FEED") == "FEED"
            and row.get("purpose") in PURPOSES
        ):
            current = candidates.get(row["media_id"])
            distance = abs(row["elapsed_hours"] - settings.comparison_hours)
            if current is None or distance < abs(
                current["elapsed_hours"] - settings.comparison_hours
            ):
                candidates[row["media_id"]] = row
    groups = {}
    for row in candidates.values():
        groups.setdefault(PURPOSES[row["purpose"]], []).append(row)
    comparable = sorted([row for group in groups.values() if len(group) >= 2 for row in group],
                        key=lambda row: (PURPOSES[row["purpose"]], row["media_id"]))
    evidence = [
        {
            k: row[k]
            for k in (
                "media_id",
                "observed_at",
                "elapsed_hours",
                "save_rate",
                "share_rate",
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
        "analysis_version": VERSION,
        "briefing": make_briefing(groups),
        "limitations": "한국 시간 기준 관찰입니다. "
        "같은 목적·유료 홍보가 없는 게시물만 정한 경과 시간 범위에서 비교합니다. "
        "상품·행사·목표 차이가 남으므로 시간·형식의 인과 효과나 매출 상승으로 단정하지 않습니다.",
    }


def make_briefing(groups):
    """관측 수치를 설명하는 규칙 기반 브리핑. 사진의 의미나 인과 원인을 추정하지 않는다."""
    cards = []
    for purpose, rows in sorted(groups.items()):
        if len(rows) < 2:
            continue
        ranked = sorted(rows, key=lambda row: (-row["save_rate"], row["media_id"]))
        cards.append({
            "purpose": purpose,
            "evidence_ids": [row["media_id"] for row in ranked],
            "rates": {row["media_id"]: row["save_rate"] for row in ranked},
            "observation": " / ".join(f'{row["media_id"]}: 저장률 {row["save_rate"] * 100:.4g}%'
                                      for row in ranked),
            "reason": "같은 목적과 관측 시점의 저장 비율 차이를 확인했습니다. 차이의 원인은 이 수치만으로 알 수 없습니다.",
            "next_action": f"다음 {purpose} 제작에서 대표 사진 또는 첫 문장 중 하나를 선택해 바꿔보세요. 적용 여부는 직접 선택합니다.",
            "limitations": "메뉴 인기·행사 등 다른 영향이 남아 있습니다. 방문·매출 효과는 확인하지 않았습니다.",
        })
    return {"status": "available" if cards else "insufficient_data", "cards": cards}


def report(settings, owner_id):
    with connect(settings) as con:
        rows = con.execute(
            """SELECT o.*, p.layout, p.purpose, p.promotion, p.product_name, p.published_snapshot, h.caption,
            CASE WHEN p.id IS NOT NULL THEN 'FEED' ELSE 'UNKNOWN' END AS media_product_type
            FROM observations o LEFT JOIN posts p ON p.ig_media_id=o.media_id AND p.owner_id=o.owner_id
            LEFT JOIN history h ON h.media_id=o.media_id AND h.owner_id=o.owner_id
            WHERE o.owner_id=? ORDER BY o.id DESC""", (owner_id,),
        ).fetchall()
    return analyze(rows, settings)


def local_text(value):
    parsed = parse_time(value)
    return parsed.astimezone(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M KST") if parsed else "시각 미확인"
