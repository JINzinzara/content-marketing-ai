"""저장 메뉴로 당일 공지를 만들고 확정한 정보만 게시한다. AI 호출은 없다."""

import io
import json
import math
import time
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from PIL import Image, ImageDraw, ImageOps

from .db import connect, owned_post
from .rendering import ASPECTS, atomic_write, draw_text

KST = ZoneInfo("Asia/Seoul")


def today():
    return datetime.now(KST).date().isoformat()


def current(con, who, *, create=False):
    row = con.execute("SELECT * FROM lineups WHERE owner_id=? AND day=?", (who, today())).fetchone()
    if not row and create:
        con.execute("INSERT INTO lineups(id, owner_id, day) VALUES (?, ?, ?)", (uuid.uuid4().hex, who, today()))
        return current(con, who)
    return dict(row) if row else None


def items(con, lineup_id):
    return [dict(row) for row in con.execute(
        "SELECT m.*, i.sold_out FROM lineup_items i JOIN menus m ON m.id=i.menu_id WHERE i.lineup_id=? ORDER BY m.created_at, m.id",
        (lineup_id,),
    )]


def change_guard(con, who, lineup, revision):
    if lineup["revision"] != revision:
        raise HTTPException(409, "오늘 메뉴가 변경됐어요. 새로고침 후 확인해주세요")
    if con.execute("""SELECT 1 FROM publish_schedules s JOIN notices n ON n.post_id=s.post_id
        WHERE n.lineup_id=? AND s.state='processing'""", (lineup["id"],)).fetchone():
        raise HTTPException(409, "예약 게시를 처리 중이에요. 결과를 먼저 확인해주세요")
    if con.execute("""SELECT 1 FROM notices n JOIN posts p ON p.id=n.post_id
        WHERE n.lineup_id=? AND p.publish_state IN ('creating','container_ready','publishing','unknown')""", (lineup["id"],)).fetchone():
        raise HTTPException(409, "공지 게시 결과를 먼저 확인해주세요")


def invalidate(con, lineup_id):
    con.execute("""UPDATE publish_schedules SET state='cancelled', message='오늘 메뉴가 변경되어 재확인이 필요해요'
        WHERE post_id IN (SELECT post_id FROM notices WHERE lineup_id=?) AND state='pending'""", (lineup_id,))


def save_selection(cfg, who, menu_ids, revision, *, copy_yesterday=False):
    with connect(cfg, write=True) as con:
        lineup = current(con, who, create=True)
        change_guard(con, who, lineup, revision)
        if copy_yesterday:
            day = (datetime.now(KST).date() - timedelta(days=1)).isoformat()
            previous = con.execute("SELECT id FROM lineups WHERE owner_id=? AND day=?", (who, day)).fetchone()
            if not previous:
                raise HTTPException(422, "어제 저장한 구성이 없어요")
            menu_ids = [row["id"] for row in items(con, previous["id"])]
        menu_ids = list(dict.fromkeys(menu_ids))
        if not menu_ids:
            raise HTTPException(422, "오늘 판매할 메뉴를 선택해주세요")
        if any(not con.execute("SELECT 1 FROM menus WHERE id=? AND owner_id=?", (identifier, who)).fetchone() for identifier in menu_ids):
            raise HTTPException(404, "등록 메뉴를 찾을 수 없어요")
        old = {row["id"]: row["sold_out"] for row in items(con, lineup["id"])}
        con.execute("DELETE FROM lineup_items WHERE lineup_id=?", (lineup["id"],))
        con.executemany("INSERT INTO lineup_items(lineup_id, menu_id, sold_out) VALUES (?, ?, ?)",
                        [(lineup["id"], identifier, 0 if copy_yesterday else old.get(identifier, 0)) for identifier in menu_ids])
        con.execute("UPDATE lineups SET revision=revision+1, confirmed_at=NULL WHERE id=?", (lineup["id"],))
        invalidate(con, lineup["id"])


def confirm(cfg, who, revision):
    with connect(cfg, write=True) as con:
        lineup = current(con, who, create=True)
        change_guard(con, who, lineup, revision)
        if not items(con, lineup["id"]):
            raise HTTPException(422, "오늘 메뉴를 먼저 저장해주세요")
        con.execute("UPDATE lineups SET confirmed_at=? WHERE id=?", (int(time.time()), lineup["id"]))


def sold_out(cfg, who, menu_id, revision, value):
    with connect(cfg, write=True) as con:
        lineup = current(con, who, create=True)
        change_guard(con, who, lineup, revision)
        if not lineup["confirmed_at"]:
            raise HTTPException(409, "오늘 메뉴를 먼저 확정해주세요")
        row = con.execute("SELECT sold_out FROM lineup_items WHERE lineup_id=? AND menu_id=?", (lineup["id"], menu_id)).fetchone()
        if not row:
            raise HTTPException(404, "오늘 메뉴가 아니에요")
        if row["sold_out"] != value:
            con.execute("UPDATE lineup_items SET sold_out=? WHERE lineup_id=? AND menu_id=?", (value, lineup["id"], menu_id))
            con.execute("UPDATE lineups SET revision=revision+1 WHERE id=?", (lineup["id"],))
            invalidate(con, lineup["id"])


def validate_notice(con, post, now=None):
    if post["content_kind"] == "promotion":
        return
    stamp = con.execute("SELECT * FROM notices WHERE post_id=?", (post["id"],)).fetchone()
    lineup = con.execute("SELECT * FROM lineups WHERE id=? AND owner_id=?", (stamp["lineup_id"], post["owner_id"])).fetchone() if stamp else None
    day = (now or datetime.now(KST)).astimezone(KST).date().isoformat()
    if not lineup or not lineup["confirmed_at"] or lineup["day"] != day or lineup["revision"] != stamp["lineup_revision"]:
        raise HTTPException(409, "오늘 확정 정보가 바뀌었거나 날짜가 지났어요. 공지를 새로 만들어주세요")


def render_notice(cfg, shop, lineup, menus, aspect, kind, *, photo=None):
    width, height = ASPECTS[aspect]
    image = Image.new("RGB", (width, height), "#fffaf3")
    draw = ImageDraw.Draw(image)
    color = shop["color"]
    draw_text(draw, shop["name"] or "오늘의 메뉴", (64, 110, width-128, 90), size=48, fill=color, weight=500)
    title = "오늘 품절 안내" if kind == "soldout" else "오늘의 라인업"
    draw_text(draw, title, (64, 220, width-128, 110), size=66, fill=color, weight=500)
    draw_text(draw, lineup["day"], (64, 342, width-128, 64), size=32, fill=color, weight=500)
    top = 470
    if photo:
        photo_height = (height-650)//2
        with Image.open(io.BytesIO(photo)) as src:
            tile = ImageOps.contain(src.convert("RGB"), (width-128, photo_height), Image.Resampling.LANCZOS)
        image.paste(tile, ((width-tile.width)//2, top))
        top += photo_height+40
    columns = 1 if len(menus) <= (4 if photo else 8) else 2
    rows = math.ceil(len(menus)/columns)
    cell_width, cell_height = (width-128)//columns, (height-top-190)//rows
    # shortcut: 한 장에서 메뉴명이 읽히는 범위만 출력한다. 다장 출력은 별도 서식이 필요하다.
    if cell_height < 70:
        raise HTTPException(422, "한 장에 메뉴가 너무 많아요. 메뉴를 줄이거나 별도 공지로 나눠주세요")
    for index, menu in enumerate(menus):
        x, y = 64+(index % columns)*cell_width, top+(index//columns)*min(cell_height, 150)
        label = menu["name"]+(" · 품절" if menu["sold_out"] else "")
        draw_text(draw, label, (x, y, cell_width-24, min(cell_height-12, 130)), size=48, minimum=24, fill=color, weight=500)
    footer = "오늘 준비한 수량이 모두 판매되었어요." if kind == "soldout" else "오늘 준비한 메뉴를 안내해 드려요."
    draw_text(draw, footer, (64, height-140, width-128, 80), size=32, fill=color, weight=500)
    out = io.BytesIO()
    image.save(out, "JPEG", quality=92)
    return out.getvalue()


def create_notice(cfg, who, revision, aspect, *, menu_id=""):
    if aspect not in {"portrait", "story"}:
        raise HTTPException(422, "피드 세로형 또는 스토리를 선택해주세요")
    kind = "soldout" if menu_id else "lineup"
    if menu_id and aspect != "story":
        raise HTTPException(422, "품절 공지는 새 스토리로 만들어요")
    with connect(cfg, write=True) as con:
        lineup = current(con, who, create=True)
        change_guard(con, who, lineup, revision)
        if not lineup["confirmed_at"]:
            raise HTTPException(409, "오늘 메뉴를 먼저 확정해주세요")
        old = con.execute("SELECT post_id FROM notices WHERE lineup_id=? AND lineup_revision=? AND menu_id=?", (lineup["id"], revision, menu_id)).fetchone()
        if old:
            post = owned_post(con, who, old["post_id"])
            if post["aspect"] != aspect:
                raise HTTPException(409, "이 구성의 공지를 이미 만들었어요. 기존 공지를 확인해주세요")
            return post["id"]
        menus = items(con, lineup["id"])
        if menu_id:
            menus = [menu for menu in menus if menu["id"] == menu_id and menu["sold_out"]]
        if not menus:
            raise HTTPException(422, "공지할 메뉴와 품절 상태를 확인해주세요")
        shop = dict(con.execute("SELECT * FROM accounts WHERE id=?", (who,)).fetchone())
        raw = render_notice(cfg, shop, lineup, menus, aspect, kind)
        identifier = uuid.uuid4().hex
        filename = identifier + ".jpg"
        atomic_write(cfg.data_dir / "originals" / filename, raw)
        title = "오늘 품절 안내" if menu_id else "오늘의 라인업"
        caption = lineup["day"] + " " + title + "\n\n" + "\n".join(menu["name"] + (" · 품절" if menu["sold_out"] else "") for menu in menus)
        snapshot = json.dumps({"day": lineup["day"], "menus": menus, "shop_name": shop["name"], "color": shop["color"]}, ensure_ascii=False)
        con.execute("""INSERT INTO posts(id, owner_id, product_name, facts, purpose, original_file, title, caption,
            layout, aspect, color, source, created_at, content_kind, media_product_type, notice_snapshot)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'split', ?, ?, 'template', ?, ?, ?, ?)""",
            (identifier, who, title, caption, "품절 공지" if menu_id else "라인업", filename, title, caption, aspect, shop["color"], int(time.time()), kind,
             "STORY" if aspect == "story" else "FEED", snapshot))
        con.execute("INSERT INTO notices(post_id, lineup_id, lineup_revision, menu_id) VALUES (?, ?, ?, ?)", (identifier, lineup["id"], revision, menu_id))
        return identifier


def schedule(cfg, who, post_id, revision, local_due):
    try:
        due = datetime.fromisoformat(local_due)
        if due.tzinfo is not None:
            raise ValueError
        due = due.replace(tzinfo=KST)
        if due <= datetime.now(KST) or due.date().isoformat() != today():
            raise ValueError
    except (TypeError, ValueError):
        raise HTTPException(422, "오늘의 미래 게시 시각을 선택해주세요") from None
    with connect(cfg, write=True) as con:
        post = owned_post(con, who, post_id)
        validate_notice(con, post)
        if post["content_kind"] == "promotion" or post["revision"] != revision or post["rendered_revision"] != revision or post["publish_state"] != "not_requested":
            raise HTTPException(409, "미리보기 확인 후 현재 완성본을 확정해주세요")
        changed = con.execute("""INSERT INTO publish_schedules(post_id, owner_id, revision, due_at, state) VALUES (?, ?, ?, ?, 'pending')
            ON CONFLICT(post_id) DO UPDATE SET revision=excluded.revision, due_at=excluded.due_at, state='pending', message=''
            WHERE publish_schedules.state IN ('pending','cancelled','failed','expired')""", (post_id, who, revision, due.isoformat())).rowcount
        if not changed:
            raise HTTPException(409, "이미 예약 게시를 처리 중이거나 결과 확인이 필요해요")


def change_photo(cfg, who, post_id, revision, raw):
    """사진은 이번 공지에만 적용한다. 등록 메뉴와 이전 완성본 파일은 보존한다."""
    saved = []
    try:
        with connect(cfg, write=True) as con:
            post = owned_post(con, who, post_id)
            if post["content_kind"] == "promotion" or post["revision"] != revision or post["publish_state"] not in {"not_requested", "failed"}:
                raise HTTPException(409, "확인한 공지가 바뀌었거나 이미 게시 중이에요")
            validate_notice(con, post)
            if con.execute("SELECT 1 FROM publish_schedules WHERE post_id=? AND state IN ('processing','unknown')", (post_id,)).fetchone():
                raise HTTPException(409, "예약 게시 결과를 먼저 확인해주세요")
            snapshot = json.loads(post["notice_snapshot"])
            photo_file = uuid.uuid4().hex+".jpg" if raw else ""
            if raw:
                path = cfg.data_dir/"originals"/photo_file
                atomic_write(path, raw); saved.append(path)
            snapshot["photo_file"] = photo_file
            rendered = render_notice(cfg, {"name": snapshot["shop_name"], "color": snapshot["color"]}, snapshot, snapshot["menus"], post["aspect"], post["content_kind"], photo=raw)
            filename = uuid.uuid4().hex+".jpg"
            path = cfg.data_dir/"originals"/filename
            atomic_write(path, rendered); saved.append(path)
            con.execute("UPDATE posts SET original_file=?, notice_snapshot=?, revision=revision+1 WHERE id=?", (filename, json.dumps(snapshot, ensure_ascii=False), post_id))
            con.execute("UPDATE publish_schedules SET state='cancelled', message='공지 사진이 바뀌어 재확인이 필요해요' WHERE post_id=? AND state='pending'", (post_id,))
    except BaseException:
        for path in saved:
            path.unlink(missing_ok=True)
        raise
