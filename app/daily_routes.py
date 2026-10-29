"""오늘 메뉴·공지·브리핑 화면. 기존 미리보기·확정·게시 경로를 재사용한다."""

import json
import time
import uuid

from fastapi import APIRouter, HTTPException, Request
from starlette.datastructures import UploadFile

from . import lineups
from .db import connect, owned_post
from .rendering import UPLOAD_BYTES, atomic_write, normalize_photo
from .routes import Form, Owner, back, confirmed, page

router = APIRouter()
PUBLISH_LABELS = {"not_requested": "미게시", "creating": "준비 중", "container_ready": "이미지 처리 중", "publishing": "게시 요청 중", "unknown": "결과 확인 필요", "published": "게시 완료", "failed": "실패"}
SCHEDULE_LABELS = {"pending": "대기", "processing": "처리 중", "unknown": "결과 확인 필요", "published": "게시 완료", "cancelled": "취소", "failed": "실패", "expired": "당일 기간 만료"}


def revision(form):
    try:
        value = int(form.get("revision", ""))
        if value < 1:
            raise ValueError
        return value
    except (TypeError, ValueError):
        raise HTTPException(422, "새로고침 후 현재 내용을 확인해주세요") from None


@router.get("/lineups")
def daily(request: Request, who: Owner):
    with connect(request.app.state.settings) as con:
        lineup = lineups.current(con, who)
        menus = [dict(row) for row in con.execute("SELECT * FROM menus WHERE owner_id=? ORDER BY created_at, id", (who,))]
        selected = lineups.items(con, lineup["id"]) if lineup else []
        notices = [dict(row) for row in con.execute("""SELECT p.id, p.product_name, p.aspect, p.publish_state, s.state AS schedule_state, s.due_at
            FROM notices n JOIN posts p ON p.id=n.post_id LEFT JOIN publish_schedules s ON s.post_id=p.id
            WHERE n.lineup_id=? AND p.owner_id=? AND p.deleted_at IS NULL ORDER BY p.created_at DESC, p.rowid DESC""", (lineup["id"] if lineup else "", who))]
    return page(request, "lineups.html", who, publish_labels=PUBLISH_LABELS, schedule_labels=SCHEDULE_LABELS, lineup=lineup or {"revision": 1, "confirmed_at": None}, day=lineups.today(), menus=menus, selected=selected, selected_ids=[item["id"] for item in selected], notices=notices)


@router.post("/menus")
def add_menu(request: Request, who: Owner, form: Form):
    name = str(form.get("name", "")).strip()
    if not 1 <= len(name) <= 60:
        raise HTTPException(422, "메뉴명을 입력해주세요")
    with connect(request.app.state.settings, write=True) as con:
        con.execute("INSERT INTO menus(id, owner_id, name, filename, created_at) VALUES (?, ?, ?, '', ?)", (uuid.uuid4().hex, who, name, int(time.time())))
    return back("/lineups")


@router.post("/lineups/save")
def select(request: Request, who: Owner, form: Form):
    lineups.save_selection(request.app.state.settings, who, form.getlist("menu_id"), revision(form), copy_yesterday=form.get("copy") == "yesterday")
    request.session["notice"] = "구성을 저장했어요. 오늘 정보가 맞는지 확인 후 확정해주세요"
    return back("/lineups")


@router.post("/lineups/confirm")
def confirm_lineup(request: Request, who: Owner, form: Form):
    lineups.confirm(request.app.state.settings, who, confirmed(form))
    return back("/lineups")


@router.post("/lineups/status")
def status(request: Request, who: Owner, form: Form):
    action = form.get("action")
    if action not in {"soldout", "undo"}:
        raise HTTPException(422, "품절 여부를 확인해주세요")
    lineups.sold_out(request.app.state.settings, who, str(form.get("menu_id", "")), confirmed(form), int(action == "soldout"))
    request.session["notice"] = "품절 상태를 저장했어요. 새 스토리로 공지할 수 있어요" if action == "soldout" else "잘못 누른 품절을 내부에서 취소했어요. 판매 재개 공지를 만들지 않습니다"
    return back("/lineups")


@router.post("/lineups/notice")
def notice(request: Request, who: Owner, form: Form):
    identifier = lineups.create_notice(request.app.state.settings, who, revision(form), str(form.get("aspect", "story")), menu_id=str(form.get("menu_id", "")))
    return back("/notices/"+identifier)


@router.get("/notices/{post_id}")
def notice_view(request: Request, post_id: str, who: Owner):
    cfg = request.app.state.settings
    with connect(cfg) as con:
        post = owned_post(con, who, post_id)
        if post["content_kind"] == "promotion":
            return back("/posts/"+post_id)
        schedule = con.execute("SELECT * FROM publish_schedules WHERE post_id=? AND owner_id=?", (post_id, who)).fetchone()
        stale = ""
        try:
            lineups.validate_notice(con, post)
        except HTTPException as exc:
            stale = exc.detail
    return page(request, "notice.html", who, publish_labels=PUBLISH_LABELS, schedule_labels=SCHEDULE_LABELS, post=post, has_photo=bool(json.loads(post["notice_snapshot"]).get("photo_file")), schedule=dict(schedule) if schedule else None, stale=stale, today=lineups.today())


@router.post("/notices/{post_id}/photo")
def notice_photo(request: Request, post_id: str, who: Owner, form: Form):
    photo = form.get("photo")
    if form.get("remove") == "yes":
        raw = None
    elif isinstance(photo, UploadFile) and photo.filename:
        raw = normalize_photo(photo.file.read(UPLOAD_BYTES+1))
    else:
        raise HTTPException(422, "이번 공지에 쓸 사진을 선택해주세요")
    lineups.change_photo(request.app.state.settings, who, post_id, revision(form), raw)
    return back("/notices/"+post_id)


@router.post("/notices/{post_id}/schedule")
def schedule_notice(request: Request, post_id: str, who: Owner, form: Form):
    cfg = request.app.state.settings
    if not cfg.instagram_ready:
        raise HTTPException(503, "Instagram 연결 설정을 먼저 완료해주세요")
    request.app.state.instagram.connection(who)
    lineups.schedule(cfg, who, post_id, confirmed(form), str(form.get("due_at", "")))
    return back("/notices/"+post_id)


@router.post("/notices/{post_id}/cancel")
def cancel_schedule(request: Request, post_id: str, who: Owner, form: Form):
    with connect(request.app.state.settings, write=True) as con:
        owned_post(con, who, post_id)
        if not con.execute("UPDATE publish_schedules SET state='cancelled', message='사용자가 예약을 취소했어요' WHERE post_id=? AND owner_id=? AND state='pending'", (post_id, who)).rowcount:
            raise HTTPException(409, "이미 처리 중이거나 취소할 예약이 없어요. 게시 상태를 확인해주세요")
    return back("/notices/"+post_id)
