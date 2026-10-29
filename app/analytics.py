"""지표의 결측·게시 경과 시간·한국 시간대를 해석하고 비교 근거 생성"""

import hashlib
import json
import math
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse
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
VERSION = "insights-1.1.0"
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


def local_text(value):
    parsed = parse_time(value)
    return parsed.astimezone(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M KST") if parsed else "시각 미확인"


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
            for key in ("layout", "purpose", "product_name", "promotion", "media_product_type", "content_kind", "revision", "style_snapshot", "recommendation_id"):
                if key in published:
                    values[key] = published[key]
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
            and row.get("content_kind", "promotion") == "promotion"
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
            k: row.get(k)
            for k in (
                "media_id",
                "observed_at",
                "published_at",
                "metrics",
                "elapsed_hours",
                "save_rate",
                "share_rate",
                "layout",
                "purpose",
                "product_name",
                "revision",
                "style_snapshot",
                "recommendation_id",
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
            "next_action": "다음에는 사진의 구도나 첫 문장 중 하나를 바꿔보세요.",
            "limitations": "메뉴 인기·행사 등 다른 영향이 남아 있습니다. 방문·매출 효과는 확인하지 않았습니다.",
        })
    return {"status": "available" if cards else "insufficient_data", "cards": cards}


def report(settings, owner_id):
    with connect(settings) as con:
        rows = con.execute(
            """SELECT o.*, COALESCE(p.layout, 'imported') AS layout, COALESCE(p.purpose, h.purpose) AS purpose,
            COALESCE(p.promotion, h.promotion) AS promotion, p.product_name, p.published_snapshot, p.revision, p.style_snapshot, p.recommendation_id, h.caption,
            COALESCE(p.content_kind, 'promotion') AS content_kind,
            COALESCE(p.media_product_type, h.media_product_type, 'UNKNOWN') AS media_product_type
            FROM observations o LEFT JOIN posts p ON p.ig_media_id=o.media_id AND p.owner_id=o.owner_id
            LEFT JOIN history h ON h.media_id=o.media_id AND h.owner_id=o.owner_id
            WHERE o.owner_id=? ORDER BY o.id DESC""", (owner_id,),
        ).fetchall()
    return analyze(rows, settings)


def history_page(settings, owner_id, page=1):
    """과거 게시물은 분류 없이 표시한다. 비교 근거를 고르는 report와는 별도 조회다."""
    size = settings.history_page_size
    with connect(settings) as con:
        rows = con.execute("""SELECT h.*, COALESCE(p.media_product_type, h.media_product_type) AS format,
            COALESCE(p.content_kind, 'promotion') AS content_kind,
            o.metrics, o.observed_at FROM history h
            LEFT JOIN posts p ON p.owner_id=h.owner_id AND p.ig_media_id=h.media_id
            LEFT JOIN observations o ON o.id=(SELECT id FROM observations WHERE owner_id=h.owner_id AND media_id=h.media_id ORDER BY observed_at DESC, id DESC LIMIT 1)
            WHERE h.owner_id=? AND h.deleted_at IS NULL AND (p.id IS NULL OR p.deleted_at IS NULL)
            ORDER BY h.published_at DESC, h.media_id DESC LIMIT ? OFFSET ?""", (owner_id, size+1, (page-1)*size)).fetchall()
    result = []
    for row in rows[:size]:
        item = dict(row)
        item["metrics"] = json.loads(item["metrics"]) if item["metrics"] else {}
        item["collectable"] = item["format"] == "FEED" and item["content_kind"] == "promotion" and item["purpose"] != "라인업"
        link = urlparse(item["permalink"] or "")
        item["link"] = item["permalink"] if link.scheme == "https" and (link.hostname or "").removeprefix("www.") == "instagram.com" and not link.username else None
        result.append(item)
    return result, len(rows) > size


def refresh_state(app, who):
    with app.state.analytics_lock:
        return dict(app.state.analytics_refreshes.get(who, {"state": "idle", "page": 1, "ids": []}))


def queue_refresh(app, who, tasks, page=1, *, older=False):
    with app.state.analytics_lock:
        current = app.state.analytics_refreshes.get(who, {})
        if current.get("state") == "running":
            return current["page"]
        job_id = uuid.uuid4().hex
        app.state.analytics_refreshes[who] = {"id": job_id, "state": "running", "page": page, "ids": [], "message": "게시물 반응을 가져오고 있어요"}
    tasks.add_task(refresh_history, app, who, job_id, page, older)
    return page


def refresh_history(app, who, job_id, page, older=False):
    """화면은 먼저 보여주고 선택 페이지의 반응을 뒤에서 수집한다. AI 호출은 없다."""
    from fastapi import HTTPException
    from .instagram import InstagramError
    cfg, instagram = app.state.settings, app.state.instagram
    message, errors, ids = "", [], []
    try:
        with connect(cfg) as con:
            sync = con.execute("SELECT * FROM analytics_sync_state WHERE owner_id=?", (who,)).fetchone()
        after = sync["next_cursor"] if older and sync else ""
        if not older or after:
            head_before, _ = history_page(cfg, who)
            next_cursor = instagram.sync_history(who, after, restore_deleted=False)
            head_after, _ = history_page(cfg, who)
            head_changed = [row["media_id"] for row in head_before] != [row["media_id"] for row in head_after]
            with connect(cfg, write=True) as con:
                con.execute("""INSERT INTO analytics_sync_state(owner_id, next_cursor, synced_at) VALUES (?, ?, ?)
                    ON CONFLICT(owner_id) DO UPDATE SET synced_at=excluded.synced_at,
                    next_cursor=CASE WHEN ? THEN excluded.next_cursor ELSE analytics_sync_state.next_cursor END""",
                    (who, next_cursor, datetime.now(timezone.utc).isoformat(), older or head_changed))
        rows, _ = history_page(cfg, who, page)
        ids = [row["media_id"] for row in rows]
        for index, row in enumerate(rows):
            if row["collectable"]:
                try:
                    metrics = instagram.collect_metrics(who, row["media_id"])
                    if metrics and all(item.get("status") == "error" for item in metrics.values()):
                        errors.append(row["media_id"])
                        break
                except InstagramError as exc:
                    errors.append(row["media_id"])
                    if exc.kind in {"auth", "permission", "uncertain"}:
                        break
                except HTTPException as exc:
                    errors.append(row["media_id"])
                    if exc.status_code in {401, 403}:
                        break
            with app.state.analytics_lock:
                app.state.analytics_refreshes[who]["message"] = f"게시물 반응 확인 중 · {index+1}/{len(rows)}"
        message = "일부 반응을 가져오지 못했어요. 확인된 반응은 그대로 보여드려요" if errors else "최근 반응을 확인했어요"
    except (InstagramError, HTTPException):
        message = "반응을 가져오지 못했어요. 연결 상태를 확인한 뒤 다시 불러와주세요"
        errors.append("connection")
    finally:
        with app.state.analytics_lock:
            app.state.analytics_refreshes[who] = {"id": job_id, "state": "error" if errors or not message else "completed", "page": page, "ids": ids,
                                                "message": message or "반응을 가져오지 못했어요", "completed_at": time.time()}
