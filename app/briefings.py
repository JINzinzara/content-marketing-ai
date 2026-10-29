"""앱 내 브리핑의 관측·주기·추천 선택을 보존한다. 정기 실행은 유료 AI를 호출하지 않는다."""

import json
import re
import time
from datetime import datetime, timezone

from fastapi import HTTPException

from . import analytics
from .db import connect, owned_post
from .lineups import KST
from .instagram import InstagramError


def eligible_media(cfg, who):
    with connect(cfg) as con:
        rows = con.execute("""SELECT h.media_id, h.caption, h.published_at,
            COALESCE(p.media_product_type, h.media_product_type) AS media_product_type,
            COALESCE(p.content_kind, 'promotion') AS content_kind,
            COALESCE(json_extract(p.published_snapshot, '$.purpose'), p.purpose, h.purpose) AS purpose
            FROM history h LEFT JOIN posts p ON p.ig_media_id=h.media_id AND p.owner_id=h.owner_id
            WHERE h.owner_id=? AND h.deleted_at IS NULL AND (p.id IS NULL OR p.deleted_at IS NULL)
            ORDER BY h.published_at DESC""", (who,)).fetchall()
    return [dict(row) for row in rows if row["media_product_type"] == "FEED" and row["content_kind"] == "promotion" and row["purpose"] in analytics.PURPOSES]


def collect(cfg, instagram, who, now=None):
    """정기 수집은 비교 창 안의 미관측 게시물만 처리한다. 수동 수집은 별도로 가능하다."""
    now = now or datetime.now(timezone.utc)
    results, errors = [], []
    for row in eligible_media(cfg, who):
        published = analytics.parse_time(row["published_at"])
        if not published or abs((now-published).total_seconds()/3600-cfg.comparison_hours) > cfg.comparison_tolerance_hours:
            continue
        with connect(cfg) as con:
            observations = con.execute("SELECT observed_at FROM observations WHERE owner_id=? AND media_id=?", (who, row["media_id"])).fetchall()
        if any((observed := analytics.parse_time(item["observed_at"])) and abs((observed-published).total_seconds()/3600-cfg.comparison_hours) <= cfg.comparison_tolerance_hours for item in observations):
            continue
        with connect(cfg, write=True) as con:
            claimed = con.execute("INSERT OR IGNORE INTO metric_collection_jobs(owner_id, media_id, state, attempted_at) VALUES (?, ?, 'running', ?)", (who, row["media_id"], now.isoformat())).rowcount
            if not claimed:
                previous = con.execute("SELECT message FROM metric_collection_jobs WHERE owner_id=? AND media_id=?", (who, row["media_id"])).fetchone()
                if previous["message"]:
                    errors.append(previous["message"])
                continue
        message = ""
        try:
            instagram.collect_metrics(who, row["media_id"])
            results.append(row["media_id"])
        except (HTTPException, InstagramError) as exc:
            message = str(exc.detail) if isinstance(exc, HTTPException) else str(exc)
            errors.append(message)
        finally:
            with connect(cfg, write=True) as con:
                con.execute("UPDATE metric_collection_jobs SET state=?, message=? WHERE owner_id=? AND media_id=?", ("completed" if row["media_id"] in results else "error", message or ("수집 중 오류가 발생했어요" if row["media_id"] not in results else ""), who, row["media_id"]))
    return results, errors


def save_settings(cfg, who, mode, local_time, weekdays, dates):
    if mode not in {"off", "daily", "weekdays", "dates"} or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", local_time):
        raise HTTPException(422, "브리핑 주기와 시각을 확인해주세요")
    try:
        weekdays = sorted({int(value) for value in weekdays})
        if any(value < 0 or value > 6 for value in weekdays):
            raise ValueError
        dates = sorted({datetime.strptime(value.strip(), "%Y-%m-%d").date().isoformat() for value in dates if value.strip()})
        if mode == "weekdays" and not weekdays or mode == "dates" and not dates:
            raise ValueError
    except (ValueError, TypeError):
        raise HTTPException(422, "선택한 요일·날짜를 확인해주세요") from None
    with connect(cfg, write=True) as con:
        con.execute("""INSERT INTO briefing_settings(owner_id, mode, local_time, weekdays, dates, enabled_at) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(owner_id) DO UPDATE SET mode=excluded.mode, local_time=excluded.local_time,
            weekdays=excluded.weekdays, dates=excluded.dates, enabled_at=excluded.enabled_at""",
            (who, mode, local_time, json.dumps(weekdays), json.dumps(dates), datetime.now(KST).isoformat()))


def due_slot(setting, now):
    local = now.astimezone(KST)
    mode = setting["mode"]
    day = local.date().isoformat()
    if mode == "off" or mode == "weekdays" and local.weekday() not in json.loads(setting["weekdays"]) or mode == "dates" and day not in json.loads(setting["dates"]):
        return None
    hour, minute = map(int, setting["local_time"].split(":"))
    due = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    enabled = analytics.parse_time(setting["enabled_at"])
    return due.isoformat() if enabled and enabled <= due <= local else None


def prepare(cfg, who, slot, errors=()):
    info = analytics.report(cfg, who)
    with connect(cfg, write=True) as con:
        previous = con.execute("SELECT * FROM briefing_runs WHERE owner_id=? AND state IN ('ready','no_new_data','insufficient_data') ORDER BY id DESC LIMIT 1", (who,)).fetchone()
        changed = bool(info["evidence"]) and (not previous or previous["evidence_hash"] != info["evidence_hash"])
        if changed:
            content = {"briefing": info["briefing"], "evidence": info["evidence"], "limitations": info["limitations"],
                       "as_of": max(row["observed_at"] for row in info["evidence"])}
            state = "ready"
        elif previous and json.loads(previous["content"]).get("evidence"):
            content = json.loads(previous["content"])
            state = "no_new_data"
        else:
            content = {"briefing": info["briefing"], "evidence": [], "limitations": info["limitations"],
                       "as_of": max((row["observed_at"] for row in info["latest"]), default=None)}
            state = "insufficient_data"
        con.execute("UPDATE briefing_runs SET state=?, evidence_hash=?, content=?, message=? WHERE owner_id=? AND scheduled_for=?",
                    (state, info["evidence_hash"] if changed else previous["evidence_hash"] if previous else info["evidence_hash"], json.dumps(content, ensure_ascii=False), " / ".join(errors), who, slot))


def record_event(con, who, recommendation_id, action, post_id="", revision=0):
    row = con.execute("SELECT * FROM recommendations WHERE id=? AND owner_id=?", (recommendation_id, who)).fetchone()
    if not row:
        raise HTTPException(404, "추천을 찾을 수 없어요")
    con.execute("""INSERT OR IGNORE INTO recommendation_events(owner_id, recommendation_id, action, post_id, revision, created_at)
        VALUES (?, ?, ?, ?, ?, ?)""", (who, recommendation_id, action, post_id, revision, int(time.time())))
    return dict(row)


def apply(cfg, who, recommendation_id, post_id, revision, action):
    if action not in {"applied", "ignored"}:
        raise HTTPException(422, "적용 여부를 선택해주세요")
    with connect(cfg, write=True) as con:
        post = owned_post(con, who, post_id)
        if post["content_kind"] != "promotion" or post["revision"] != revision or post["publish_state"] != "not_requested":
            raise HTTPException(409, "아직 게시하지 않은 현재 일반 홍보 초안을 선택해주세요")
        record_event(con, who, recommendation_id, action, post_id, revision)
        con.execute("UPDATE posts SET recommendation_id=?, revision=revision+1 WHERE id=?", (recommendation_id if action == "applied" else None, post_id))


def guidance(con, post):
    if not post.get("recommendation_id"):
        return None
    row = con.execute("SELECT content FROM recommendations WHERE id=? AND owner_id=?", (post["recommendation_id"], post["owner_id"])).fetchone()
    return json.loads(row["content"]) if row else None


def guidance_for_post(cfg, post):
    with connect(cfg) as con:
        return guidance(con, post)
