"""단일 앱 프로세스의 확인된 게시 예약·반응 관측·앱 내 브리핑 실행."""

import asyncio
import logging
from datetime import datetime, timezone

from . import briefings
from .db import connect, owned_post
from .lineups import KST, validate_notice

LOG = logging.getLogger(__name__)


def recover(cfg):
    with connect(cfg, write=True) as con:
        con.execute("UPDATE publish_schedules SET state='unknown', message='앱이 중단되어 결과 확인이 필요해요. 자동 재게시하지 않습니다' WHERE state='processing'")
        con.execute("UPDATE metric_collection_jobs SET state='error', message='앱 중단으로 수집이 끝나지 않았어요. 게시물 반응 화면에서 다시 수집해주세요' WHERE state='running'")
        con.execute("UPDATE briefing_runs SET state='error', message='앱 중단으로 수집이 끝나지 않았어요. 다음 주기에 다시 확인합니다' WHERE state='running'")


def tick(app, now=None, *, kind="all"):
    cfg, instagram = app.state.settings, app.state.instagram
    now = now or datetime.now(timezone.utc)
    with connect(cfg) as con:
        schedules = [dict(row) for row in con.execute("SELECT * FROM publish_schedules WHERE state IN ('pending','processing','unknown')")] if kind in {"all", "publishing"} else []
        settings = [dict(row) for row in con.execute("SELECT * FROM briefing_settings WHERE mode!='off'")] if kind in {"all", "briefing"} else []
    for job in schedules:
        due = datetime.fromisoformat(job["due_at"])
        if due > now:
            continue
        try:
            with connect(cfg, write=True) as con:
                post = owned_post(con, job["owner_id"], job["post_id"])
                if post["publish_state"] == "published":
                    con.execute("UPDATE publish_schedules SET state='published', message='' WHERE post_id=?", (post["id"],))
                    continue
                if job["state"] == "unknown":
                    continue
                validate_notice(con, post, now)
                if post["revision"] != job["revision"]:
                    raise ValueError("예약한 내용이 변경되어 재확인이 필요해요")
                if job["state"] == "pending":
                    if not con.execute("UPDATE publish_schedules SET state='processing' WHERE post_id=? AND state='pending'", (post["id"],)).rowcount:
                        continue
            instagram.publish(job["owner_id"], job["post_id"], expected_revision=job["revision"], check_only=job["state"] == "processing")
            with connect(cfg, write=True) as con:
                post = owned_post(con, job["owner_id"], job["post_id"])
                state = "published" if post["publish_state"] == "published" else "unknown" if post["publish_state"] in {"unknown", "publishing"} else "processing"
                con.execute("UPDATE publish_schedules SET state=?, message=? WHERE post_id=?", (state, post["publish_message"], post["id"]))
        except Exception as exc:
            from fastapi import HTTPException
            from .instagram import InstagramError
            if not isinstance(exc, (HTTPException, InstagramError, ValueError)):
                LOG.exception("Scheduled publication failed")
            with connect(cfg, write=True) as con:
                row = con.execute("SELECT publish_state FROM posts WHERE id=?", (job["post_id"],)).fetchone()
                uncertain = row and row["publish_state"] in {"creating", "container_ready", "publishing", "unknown"}
                message = exc.detail if isinstance(exc, HTTPException) else str(exc) if isinstance(exc, (InstagramError, ValueError)) else "예약 처리 오류. 결과를 확인해주세요"
                con.execute("UPDATE publish_schedules SET state=?, message=? WHERE post_id=?", ("unknown" if uncertain else "expired" if due.astimezone(KST).date() != now.astimezone(KST).date() else "failed", message, job["post_id"]))
    for setting in settings:
        who = setting["owner_id"]
        try:
            _, errors = briefings.collect(cfg, instagram, who, now)
            slot = briefings.due_slot(setting, now)
            if not slot:
                continue
            with connect(cfg, write=True) as con:
                if not con.execute("INSERT OR IGNORE INTO briefing_runs(owner_id, scheduled_for, created_at, state) VALUES (?, ?, ?, 'running')", (who, slot, now.isoformat())).rowcount:
                    continue
            briefings.prepare(cfg, who, slot, errors)
        except Exception:
            LOG.exception("Briefing collection failed")
            with connect(cfg, write=True) as con:
                con.execute("UPDATE briefing_runs SET state='error', message='반응 수집 중 오류가 발생했어요. 다음 주기에 다시 확인합니다' WHERE owner_id=? AND state='running'", (who,))


async def run(app, kind):
    # shortcut: 단일 프로세스에서 30초마다 확인한다. 다중 서버 운영 때 외부 작업자로 분리한다.
    while True:
        try:
            pending = asyncio.create_task(asyncio.to_thread(tick, app, kind=kind))
            try:
                await asyncio.shield(pending)
            except asyncio.CancelledError:
                await pending
                raise
        except Exception:
            LOG.exception("Background job cycle failed")
        await asyncio.sleep(30)
