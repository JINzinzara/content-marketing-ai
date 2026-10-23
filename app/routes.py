"""사용자의 가게 프로필·스타일 선택·텍스트/이미지 제작·확정·게시·분석 요청을 연결"""

import hashlib
import json
import re
import time
import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.datastructures import UploadFile

from . import analytics, styles
from .text_ai import Recommendation, Style
from .auth import consume_oauth_state, csrf_token, form_data, owner, authorization_url
from .credits import finalize
from .db import account, balance, connect, owned_post
from .costs import spent
from .instagram import caption_text
from .rendering import UPLOAD_BYTES, atomic_write, normalize_photo, render
from .image_ai import BUSY_PUBLISH, mask_points

router = APIRouter()
Owner = Annotated[str, Depends(owner)]
Form = Annotated[object, Depends(form_data)]


class ProductInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    product_name: str = Field(min_length=1, max_length=60)
    facts: str = Field(max_length=1200, default="")
    price: str = Field(max_length=40, default="")
    show_price: bool = False
    period: str = Field(max_length=100, default="")
    purpose: str = Field(pattern="^(신메뉴 안내|시즌 안내|매장 소식|행사 안내)$")
    layout: str = Field(pattern="^(frame|split)$", default="split")
    aspect: str = Field(pattern="^(portrait|square)$", default="portrait")
    preset_id: str = Field(
        pattern="^(keep|natural|minimal|modern|fresh)$", default="keep"
    )


class EditInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=60)
    caption: str = Field(min_length=1, max_length=1800)
    hashtags: str = Field(max_length=250, default="")
    price: str = Field(max_length=40, default="")
    show_price: bool = False
    layout: str = Field(pattern="^(frame|split)$")
    aspect: str = Field(pattern="^(portrait|square)$")
    promotion: str = Field(pattern="^(unknown|organic|paid)$", default="unknown")
    revision: int = Field(ge=1)


def validated(model, form):
    try:
        return model.model_validate(dict(form))
    except ValidationError:
        raise HTTPException(
            422,
            "입력 길이·필수 항목·선택값을 확인해주세요. 작성 내용은 뒤로가기로 확인할 수 있습니다",
        ) from None


def page(request, template, owner_id=None, **context):
    settings = request.app.state.settings
    shop = account(settings, owner_id) if owner_id else None
    if shop:
        shop["connected"] = bool(
            shop["token"]
            and shop["token_expires_at"]
            and shop["token_expires_at"] > time.time()
        )
    remaining = 0
    if owner_id:
        with connect(settings) as con:
            remaining = balance(con, owner_id)
    return request.app.state.templates.TemplateResponse(
        request=request,
        name=template,
        context={
            "shop": shop,
            "remaining": remaining,
            "csrf": csrf_token(request),
            "cfg": {
                "local": settings.environment == "local",
                "ai_ready": settings.ai_ready,
                "image_ready": settings.image_ready,
                "instagram_ready": settings.instagram_ready,
            },
            "notice": request.session.pop("notice", ""),
            "warning": request.session.pop("warning", ""),
            **context,
        },
    )


def back(url):
    return RedirectResponse(url, status_code=303)


def confirmed(form):
    if form.get("confirmed") != "yes":
        raise HTTPException(422, "상품 정보와 게시 내용을 확인한 뒤 체크해주세요")
    try:
        revision = int(form.get("revision", ""))
        if revision < 1:
            raise ValueError
        return revision
    except (ValueError, TypeError):
        raise HTTPException(
            422, "확정할 게시물의 버전을 확인할 수 없습니다. 화면을 새로고침해주세요"
        ) from None


@router.get("/")
def home(request: Request):
    if (
        request.app.state.settings.environment == "production"
        and not request.session.get("owner_id")
    ):
        return page(request, "login.html")
    return back("/studio")


@router.get("/studio")
def studio(request: Request, who: Owner):
    with connect(request.app.state.settings) as con:
        posts = [
            dict(row)
            for row in con.execute(
                "SELECT * FROM posts WHERE owner_id=? AND deleted_at IS NULL ORDER BY created_at DESC, rowid DESC",
                (who,),
            )
        ]
    return page(request, "studio.html", who, posts=posts)


@router.get("/shop")
def shop_view(request: Request, who: Owner):
    with connect(request.app.state.settings) as con:
        history = [
            dict(row)
            for row in con.execute(
                "SELECT * FROM history WHERE owner_id=? ORDER BY published_at DESC LIMIT 50",
                (who,),
            )
        ]
        profile, proposal = styles.latest_profile(con, who), styles.latest_profile(
            con, who, "proposal"
        )
        if profile and proposal and proposal["version"] < profile["version"]:
            proposal = None
        references = styles.reference_list(con, who)
    return page(
        request,
        "shop.html",
        who,
        history=history,
        references=references,
        profile=profile,
        proposal=proposal,
        style_fields=styles.FIELDS,
        next_cursor=request.session.get("history_next", ""),
    )


@router.post("/shop")
def save_shop(request: Request, who: Owner, form: Form):
    values = {
        key: str(form.get(key, "")).strip()
        for key in ("name", "description", "style", "reference_notes", "color")
    }
    if (
        not 1 <= len(values["name"]) <= 40
        or len(values["description"]) > 1000
        or len(values["style"]) > 2400
        or len(values["reference_notes"]) > 5000
    ):
        raise HTTPException(422, "가게 이름과 설명의 길이를 확인해주세요")
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", values["color"]):
        raise HTTPException(422, "올바른 색상을 선택해주세요")
    with connect(request.app.state.settings, write=True) as con:
        if any("style_" + key in form for key in styles.FIELDS):
            current = styles.latest_profile(con, who)
            try:
                version = int(form.get("profile_version", "0"))
                attributes = styles.Attributes.model_validate(
                    {
                        key: str(form.get("style_" + key, "")).strip()
                        for key in styles.FIELDS
                    }
                )
            except (ValueError, ValidationError):
                raise HTTPException(
                    422, "가게 스타일 항목과 버전을 확인해주세요"
                ) from None
            if version != (current["version"] if current else 0):
                raise HTTPException(
                    409,
                    "다른 요청으로 인해 스타일을 변경됐습니다. 새로고침 후 확인해주세요",
                )
            locked = list(dict.fromkeys(form.getlist("locked_fields")))
            if any(key not in styles.FIELDS for key in locked) or any(
                not getattr(attributes, key) for key in locked
            ):
                raise HTTPException(
                    422, "입력한 스타일 항목으로만 내용을 고정할 수 있습니다"
                )
            refs = current["reference_ids"] if current else []
            proposal = None
            if form.get("proposal_version"):
                proposal = con.execute(
                    "SELECT * FROM style_profiles WHERE owner_id=? AND version=? AND status='proposal'",
                    (who, str(form["proposal_version"])),
                ).fetchone()
                if not proposal:
                    raise HTTPException(
                        404, "확인 가능한 스타일 제안을 찾을 수 없습니다"
                    )
                refs = json.loads(proposal["reference_ids"])
            source = (
                json.loads(proposal["content"])
                if proposal
                else (current["content"] if current else {})
            )
            content = {
                "summary": values["style"],
                "attributes": attributes.model_dump(),
                "locked_fields": locked,
                "evidence": source.get("evidence", []),
                "uncertainties": source.get("uncertainties", []),
            }
            styles.save_profile(con, who, content, refs, "confirmed")
        con.execute(
            "UPDATE accounts SET name=?, description=?, style=?, reference_notes=?, color=? WHERE id=?",
            (*values.values(), who),
        )
    request.session["notice"] = "스타일 저장 완료"
    return back("/shop")


@router.post("/shop/analyze")
def analyze_shop(request: Request, who: Owner, form: Form):
    cfg = request.app.state.settings
    shop = account(cfg, who)
    with connect(cfg) as con:
        selected = list(
            dict.fromkeys(str(value) for value in form.getlist("reference_id"))
        )
        if len(selected) > 3:
            raise HTTPException(422, "참고 자료는 최대 3개까지 선택가능합니다")
        references = [
            styles.reference_data(cfg, con, who, identifier) for identifier in selected
        ]
    if not references and not shop["reference_notes"] and not shop["description"]:
        raise HTTPException(
            422, "기존 게시물을 가저오거나 가게 설명·참고 자료를 먼저 저장해주세요"
        )
    images = [row["image"] for row in references if row["image"]]
    result = request.app.state.ai.generate(
        who,
        "style",
        {
            "task": "style_analysis",
            "shop_description": shop["description"],
            "reference_notes": shop["reference_notes"],
            "past_post_examples": [
                {"id": row["id"], "caption": row["caption"][:2200]}
                for row in references
            ],
        },
        Style,
        images=images,
    )
    proposal = (
        result.summary
        + "\n\n근거:\n"
        + "\n".join(result.evidence)
        + "\n\n확인이 필요한 부분:\n"
        + "\n".join(result.uncertainties)
    )
    with connect(cfg, write=True) as con:
        styles.save_profile(
            con, who, {**result.model_dump(), "locked_fields": []}, selected, "proposal"
        )
        con.execute(
            "UPDATE accounts SET style_proposal=? WHERE id=?", (proposal[:5000], who)
        )
    request.session["notice"] = (
        "스타일 제안 확인 필요"
    )
    return back("/shop")


@router.post("/shop/references")
def add_reference(request: Request, who: Owner, form: Form):
    """사용자가 제공한 참고 이미지를 비공개로 저장"""
    cfg, photo = request.app.state.settings, form.get("photo")
    caption = str(form.get("caption", "")).strip()
    if not isinstance(photo, UploadFile) or len(caption) > 2200:
        raise HTTPException(422, "참고 이미지와 본문의 길이를 확인해주세요")
    normalized = normalize_photo(photo.file.read(UPLOAD_BYTES + 1))
    identifier = uuid.uuid4().hex
    filename = identifier + ".jpg"
    atomic_write(cfg.data_dir / "references" / filename, normalized)
    try:
        with connect(cfg, write=True) as con:
            con.execute(
                "INSERT INTO style_references(id, owner_id, filename, caption, created_at) VALUES (?, ?, ?, ?, ?)",
                (identifier, who, filename, caption, int(time.time())),
            )
    except BaseException:
        (cfg.data_dir / "references" / filename).unlink(missing_ok=True)
        raise
    return back("/shop")


@router.get("/shop/references/{reference_id}")
def reference_photo(request: Request, reference_id: str, who: Owner):
    cfg = request.app.state.settings
    with connect(cfg) as con:
        row = con.execute(
            "SELECT filename FROM style_references WHERE id=? AND owner_id=? AND deleted_at IS NULL",
            (reference_id, who),
        ).fetchone()
    if not row:
        raise HTTPException(404, "참고 사진을 찾을 수 없습니다")
    return FileResponse(
        cfg.data_dir / "references" / row["filename"], media_type="image/jpeg"
    )


@router.post("/shop/references/{reference_id}/delete")
def delete_reference(request: Request, reference_id: str, who: Owner, form: Form):
    """본인의 참고 자료를 앱에서 삭제한다. 기존 스타일·인스타 원본은 유지한다."""
    kind, _, identifier = reference_id.partition(":")
    table, key = {"local": ("style_references", "id"), "ig": ("history", "media_id")}.get(kind, (None, None))
    if not table or form.get("confirmed") != "yes":
        raise HTTPException(422, "삭제 대상을 확인하세요")
    with connect(request.app.state.settings, write=True) as con:
        changed = con.execute(
            f"UPDATE {table} SET deleted_at=? WHERE {key}=? AND owner_id=? AND deleted_at IS NULL",
            (int(time.time()), identifier, who),
        ).rowcount
        if not changed:
            raise HTTPException(404, "자료를 찾을 수 없어요")
    request.session["notice"] = "참고 자료 삭제 완료"
    return back("/shop")


@router.get("/posts/new")
def new_view(request: Request, who: Owner):
    return page(request, "new.html", who, presets=styles.PRESETS)


@router.post("/posts")
def create_post(request: Request, who: Owner, form: Form):
    cfg = request.app.state.settings
    item = validated(ProductInput, form)
    photo = form.get("photo")
    if not isinstance(photo, UploadFile):
        raise HTTPException(422, "새로 게시할 사진을 첨부해주세요")
    with connect(cfg) as con:
        if balance(con, who) < 1:
            raise HTTPException(
                402, "제작권을 모두 사용했습니다. 제작권을 구매해주세요"
            )
    normalized = normalize_photo(photo.file.read(UPLOAD_BYTES + 1))
    post_id = uuid.uuid4().hex
    original_file = post_id + ".jpg"
    atomic_write(cfg.data_dir / "originals" / original_file, normalized)
    shop = account(cfg, who)
    caption = "\n".join(
        x
        for x in (
            item.product_name,
            item.facts,
            item.price if item.show_price else "",
            item.period,
        )
        if x
    )
    try:
        with connect(cfg, write=True) as con:
            snapshot = json.dumps(
                styles.resolve(con, who, item.preset_id), ensure_ascii=False
            )
            con.execute(
                """INSERT INTO posts(id, owner_id, product_name, facts, price, period, purpose, original_file,
                title, caption, layout, aspect, color, created_at, style_snapshot, show_price) VALUES 
                (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    post_id,
                    who,
                    item.product_name,
                    item.facts,
                    item.price,
                    item.period,
                    item.purpose,
                    original_file,
                    item.product_name,
                    caption,
                    "split",
                    item.aspect,
                    shop["color"],
                    int(time.time()),
                    snapshot,
                    item.show_price,
                ),
            )
    except BaseException:
        (cfg.data_dir / "originals" / original_file).unlink(missing_ok=True)
        raise
    if cfg.ai_ready:
        with connect(cfg) as con:
            post = owned_post(con, who, post_id)
        try:
            result = request.app.state.ai.draft(who, post_id, shop, post)
            with connect(cfg, write=True) as con:
                con.execute(
                    "UPDATE posts SET title=?, caption=?, hashtags=?, layout=?, source='ai' WHERE id=?",
                    (
                        result.title,
                        result.caption,
                        result.hashtags,
                        "split",
                        post_id,
                    ),
                )
        except HTTPException:
            request.session["notice"] = (
                "초안 저장·AI 연결 확인"
            )
            request.session["warning"] = "초안은 저장했지만 AI 문구를 만들지 못했습니다. 연결 상태와 예산을 확인해주세요."
    return back("/posts/" + post_id)


@router.get("/posts/{post_id}")
def post_view(request: Request, post_id: str, who: Owner):
    with connect(request.app.state.settings) as con:
        post = owned_post(con, who, post_id)
        charged = bool(
            con.execute(
                "SELECT 1 FROM credits WHERE owner_id=? AND reference=?",
                (who, "post:" + post_id),
            ).fetchone()
        )
        attempts = con.execute(
            "SELECT COUNT(*) FROM ai_calls WHERE owner_id=? AND post_id=? AND purpose!='image'",
            (who, post_id),
        ).fetchone()[0]
        images = [
            dict(row)
            for row in con.execute(
                "SELECT * FROM image_generations WHERE owner_id=? AND post_id=? ORDER BY created_at DESC, rowid DESC",
                (who, post_id),
            )
        ]
        image_block = request.app.state.image_ai.block_reason(con, post)
    return page(
        request,
        "post.html",
        who,
        post=post,
        charged=charged,
        attempts=attempts,
        attempt_limit=request.app.state.settings.ai_attempts_per_post,
        images=images,
        presets=styles.PRESETS,
        image_limit=request.app.state.settings.image_attempts_per_post,
        image_block=image_block[1] if image_block else "",
        image_progress=request.app.state.image_ai.progress(who, post_id),
        current_style=styles.post_style(post),
        style_fields=styles.FIELDS,
        image_busy=any(
            row["state"] in {"queued", "running", "unknown"} for row in images
        ),
    )


@router.post("/posts/{post_id}/delete")
def delete_post(request: Request, post_id: str, who: Owner, form: Form):
    """본인의 콘텐츠를 삭제하되 제작권·비용·발행 기록은 보존한다."""
    revision = confirmed(form)
    with connect(request.app.state.settings, write=True) as con:
        post = owned_post(con, who, post_id)
        active = con.execute("SELECT 1 FROM image_generations WHERE post_id=? AND state IN ('queued','running','unknown')", (post_id,)).fetchone()
        if post["revision"] != revision or post["publish_state"] in BUSY_PUBLISH or active:
            raise HTTPException(409, "처리 상태를 확인하세요")
        con.execute("UPDATE posts SET deleted_at=? WHERE id=?", (int(time.time()), post_id))
        con.execute("DELETE FROM media_links WHERE filename GLOB ?", (post_id + "-*.jpg",))
    request.session["notice"] = "콘텐츠 삭제 완료"
    return back("/studio")


@router.post("/posts/{post_id}/style")
def choose_style(request: Request, post_id: str, who: Owner, form: Form):
    cfg = request.app.state.settings
    revision = confirmed(form)
    with connect(cfg, write=True) as con:
        post = owned_post(con, who, post_id)
        if post["revision"] != revision or post["publish_state"] in {
            "creating",
            "container_ready",
            "publishing",
            "unknown",
        }:
            raise HTTPException(409, "내용이 변경되었거나 게시를 처리 중입니다")
        resolved = json.dumps(
            styles.resolve(con, who, str(form.get("preset_id", "keep"))),
            ensure_ascii=False,
        )
        if resolved != post["style_snapshot"]:
            con.execute(
                "UPDATE posts SET style_snapshot=?, selected_image_id=NULL, revision=revision+1 WHERE id=?",
                (resolved, post_id),
            )
    request.session["notice"] = (
        "게시물 스타일 저장 완료"
    )
    return back("/posts/" + post_id)


@router.post("/posts/{post_id}/images")
def generate_image(
    request: Request, post_id: str, tasks: BackgroundTasks, who: Owner, form: Form
):
    revision = confirmed(form)
    mode = str(form.get("edit_mode", "automatic"))
    if mode not in {"automatic", "masked"}:
        raise HTTPException(422, "편집 방식을 확인해주세요")
    points = mask_points(str(form.get("mask_points", ""))) if mode == "masked" else None
    job = request.app.state.image_ai.queue(
        who,
        post_id,
        revision,
        points,
    )
    tasks.add_task(request.app.state.image_ai.run, job)
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"job_id": job}, status_code=202)
    return back("/posts/" + post_id)


@router.get("/posts/{post_id}/image-progress")
def image_progress(request: Request, post_id: str, who: Owner):
    return {"job": request.app.state.image_ai.progress(who, post_id)}


@router.get("/posts/{post_id}/original")
def original_image(request: Request, post_id: str, who: Owner):
    cfg = request.app.state.settings
    with connect(cfg) as con:
        post = owned_post(con, who, post_id)
    return FileResponse(
        cfg.data_dir / "originals" / post["original_file"], media_type="image/jpeg"
    )


@router.get("/posts/{post_id}/images/{image_id}")
def candidate_photo(request: Request, post_id: str, image_id: str, who: Owner):
    cfg = request.app.state.settings
    with connect(cfg) as con:
        owned_post(con, who, post_id)
        row = con.execute(
            "SELECT * FROM image_generations WHERE id=? AND post_id=? AND owner_id=? AND state='completed'",
            (image_id, post_id, who),
        ).fetchone()
    if not row:
        raise HTTPException(404, "이미지 후보를 찾을 수 없습니다")
    return FileResponse(
        cfg.data_dir / "generated" / row["filename"], media_type="image/png"
    )


@router.post("/posts/{post_id}/images/select")
def select_image(request: Request, post_id: str, who: Owner, form: Form):
    cfg, revision = request.app.state.settings, confirmed(form)
    identifier = str(form.get("image_id", ""))
    with connect(cfg, write=True) as con:
        post = owned_post(con, who, post_id)
        if post["revision"] != revision or post["publish_state"] in {
            "creating",
            "container_ready",
            "publishing",
            "unknown",
        }:
            raise HTTPException(409, "내용이 변경되었거나 게시를 처리 중입니다")
        if identifier:
            candidate = con.execute(
                "SELECT * FROM image_generations WHERE id=? AND post_id=? AND owner_id=? AND state='completed'",
                (identifier, post_id, who),
            ).fetchone()
            if not candidate:
                raise HTTPException(404, "선택할 이미지 후보를 찾을 수 없습니다")
            if candidate["style_snapshot"] != post["style_snapshot"]:
                raise HTTPException(
                    409, "현재 스타일과 다른 후보입니다. 스타일을 확인해주세요"
                )
        if (post["selected_image_id"] or "") != identifier:
            con.execute(
                "UPDATE posts SET selected_image_id=?, revision=revision+1 WHERE id=?",
                (identifier or None, post_id),
            )
    return back("/posts/" + post_id)


@router.post("/posts/{post_id}/edit")
def edit_post(request: Request, post_id: str, who: Owner, form: Form):
    fields = validated(EditInput, form)
    cfg = request.app.state.settings
    with connect(cfg, write=True) as con:
        post = owned_post(con, who, post_id)
        if post["revision"] != fields.revision or post["publish_state"] in {
            "creating",
            "container_ready",
            "publishing",
            "unknown",
        }:
            raise HTTPException(
                409,
                "다른 요청으로 인해 내용이 변경되었거나 게시 처리 중입니다. 새로고침 후 다시 시도해주세요",
            )
        post.update(fields.model_dump(exclude={"revision"}))
        # 저장 전에 미리보기 확인
        render(cfg, post, account(cfg, who)["name"])
        con.execute(
            "UPDATE posts SET title=?, caption=?, hashtags=?, price=?, show_price=?, layout=?, aspect=?, promotion=?, revision=revision+1 WHERE id=?",
            (
                fields.title,
                fields.caption,
                fields.hashtags,
                fields.price,
                fields.show_price,
                "split",
                fields.aspect,
                fields.promotion,
                post_id,
            ),
        )
    request.session["notice"] = (
        "수정 저장·재확정 필요"
        if post["finalized_at"]
        else "수정 저장 완료"
    )
    return back("/posts/" + post_id)


@router.post("/posts/{post_id}/generate")
def regenerate(request: Request, post_id: str, who: Owner, form: Form):
    cfg = request.app.state.settings
    with connect(cfg) as con:
        post = owned_post(con, who, post_id)
    if post["publish_state"] in {
        "creating",
        "container_ready",
        "publishing",
        "unknown",
    }:
        raise HTTPException(409, "게시 처리 중에는 문구를 다시 생성할 수 없습니다")
    result = request.app.state.ai.draft(who, post_id, account(cfg, who), post)
    with connect(cfg, write=True) as con:
        current = owned_post(con, who, post_id)
        if current["revision"] != post["revision"] or current["publish_state"] in {
            "creating",
            "container_ready",
            "publishing",
            "unknown",
        }:
            raise HTTPException(
                409, "생성 중 내용이 변경되어 변경이 반영되지 않았습니다"
            )
        con.execute(
            "UPDATE posts SET title=?, caption=?, hashtags=?, layout=?, source='ai', revision=revision+1 WHERE id=?",
            (result.title, result.caption, result.hashtags, "split", post_id),
        )
    return back("/posts/" + post_id)


@router.get("/posts/{post_id}/image")
def preview(request: Request, post_id: str, who: Owner):
    cfg = request.app.state.settings
    with connect(cfg) as con:
        post = owned_post(con, who, post_id)
    if post["finished_file"] and post["rendered_revision"] == post["revision"]:
        return FileResponse(cfg.data_dir / "finished" / post["finished_file"], media_type="image/jpeg", headers={"Cache-Control": "no-store"})
    return Response(
        render(cfg, post, account(cfg, who)["name"]),
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )


@router.post("/posts/{post_id}/finalize")
def finalize_post(request: Request, post_id: str, who: Owner, form: Form):
    revision = confirmed(form)
    finalize(request.app.state.settings, who, post_id, expected_revision=revision)
    request.session["notice"] = (
        "완성본 확정 완료"
    )
    return back("/posts/" + post_id)


@router.get("/posts/{post_id}/download")
def download(request: Request, post_id: str, who: Owner):
    cfg = request.app.state.settings
    with connect(cfg) as con:
        post = owned_post(con, who, post_id)
    if not post["finished_file"] or post["rendered_revision"] != post["revision"]:
        raise HTTPException(409, "현재 내용을 먼저 완성본으로 확정해주세요")
    return FileResponse(
        cfg.data_dir / "finished" / post["finished_file"],
        media_type="image/jpeg",
        filename="instagram-post.jpg",
        headers={"Cache-Control": "no-store"},
    )


@router.post("/posts/{post_id}/publish")
def publish_post(request: Request, post_id: str, who: Owner, form: Form):
    revision = confirmed(form)
    request.app.state.instagram.publish(who, post_id, expected_revision=revision)
    return back("/posts/" + post_id)


@router.post("/posts/{post_id}/check")
def check_post(request: Request, post_id: str, who: Owner, form: Form):
    request.app.state.instagram.publish(who, post_id, check_only=True)
    return back("/posts/" + post_id)


@router.get("/media/{capability}.jpg")
def public_image(request: Request, capability: str):
    cfg = request.app.state.settings
    with connect(cfg) as con:
        row = con.execute(
            "SELECT * FROM media_links WHERE token_hash=? AND expires_at>?",
            (hashlib.sha256(capability.encode()).hexdigest(), int(time.time())),
        ).fetchone()
    if not row:
        raise HTTPException(404, "이미지를 찾을 수 없습니다")
    return FileResponse(
        cfg.data_dir / "finished" / row["filename"],
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/auth/instagram")
def connect_instagram(request: Request):
    return RedirectResponse(authorization_url(request), status_code=302)


@router.get("/auth/instagram/callback")
def instagram_callback(
    request: Request, state: str = "", code: str = "", error: str = ""
):
    preferred = consume_oauth_state(request, state)
    if error or not code:
        raise HTTPException(400, "Instagram 연결이 취소되었거나 인증 코드가 없습니다")
    long, me = request.app.state.instagram.exchange_code(code)
    identifier = request.app.state.instagram.save_connection(preferred, long, me)
    request.session.clear()
    request.session["owner_id"] = identifier
    request.session["notice"] = (
        "Instagram 연결 완료"
    )
    return back("/shop")


@router.post("/instagram/sync")
def sync_instagram(request: Request, who: Owner, form: Form):
    after = str(form.get("after", ""))
    if len(after) > 2000:
        raise HTTPException(422, "조회 커서가 올바르지 않습니다")
    request.session["history_next"] = request.app.state.instagram.sync_history(
        who, after
    )
    request.session["notice"] = "게시물 가져오기 완료"
    return back("/shop")


@router.post("/instagram/disconnect")
def disconnect_instagram(request: Request, who: Owner, form: Form):
    with connect(request.app.state.settings, write=True) as con:
        con.execute(
            "UPDATE accounts SET token=NULL, token_expires_at=NULL, token_issued_at=NULL WHERE id=?",
            (who,),
        )
    request.session["notice"] = (
        "연결 해제 완료"
    )
    return back("/connections")


@router.get("/analytics")
def analytics_view(request: Request, who: Owner):
    cfg = request.app.state.settings
    info = analytics.report(cfg, who)
    with connect(cfg) as con:
        history = [
            dict(row)
            for row in con.execute(
                "SELECT media_id, caption FROM history WHERE owner_id=? ORDER BY published_at DESC LIMIT 50",
                (who,),
            )
        ]
        recommendation = con.execute(
            "SELECT content FROM recommendations WHERE owner_id=?  AND evidence_hash=?",
            (who, info["evidence_hash"]),
        ).fetchone()
    return page(
        request,
        "analytics.html",
        who,
        report=info,
        history=history,
        recommendation=(
            json.loads(recommendation["content"]) if recommendation else None
        ),
        metric_names=analytics.METRICS,
        metric_statuses=analytics.STATUSES,
        comparison_hours=cfg.comparison_hours,
        tolerance_hours=cfg.comparison_tolerance_hours,
    )


@router.post("/analytics/collect")
def collect(request: Request, who: Owner, form: Form):
    media_id = str(form.get("media_id", ""))
    request.app.state.instagram.collect_metrics(who, media_id)
    return back("/analytics")


@router.post("/analytics/recommend")
def recommend(request: Request, who: Owner, form: Form):
    cfg = request.app.state.settings
    info = analytics.report(cfg, who)
    if not info["evidence"]:
        raise HTTPException(
            422, "분석을 위한 비교 자료가 부족합니다. 자료를 더 수집한 뒤 시도해주세요"
        )
    with connect(cfg) as con:
        cached = con.execute(
            "SELECT 1 FROM recommendations WHERE owner_id=? AND evidence_hash=?",
            (who, info["evidence_hash"]),
        ).fetchone()
    if not cached:
        result = request.app.state.ai.generate(
            who,
            "recommendation",
            {
                "observed_comparisons": info["evidence"],
                "limitations": info["limitations"],
            },
            Recommendation,
        )
        with connect(cfg, write=True) as con:
            con.execute(
                "INSERT OR IGNORE INTO recommendations(owner_id, evidence_hash, content, created_at) VALUES (?, ?, ?, ?)",
                (
                    who,
                    info["evidence_hash"],
                    result.model_dump_json(),
                    int(time.time()),
                ),
            )
    return back("/analytics")


@router.get("/connections")
def connections_view(request: Request, who: Owner):
    cfg = request.app.state.settings
    with connect(cfg) as con:
        cost = spent(con)
        pending = con.execute(
            "SELECT COUNT(*) FROM ai_calls WHERE cost_microusd IS NULL"
        ).fetchone()[0]
    return page(
        request,
        "connections.html",
        who,
        cost_usd=f"{cost / 1_000_000:.4f}",
        budget=str(cfg.ai_budget_usd),
        pending_calls=pending,
    )


@router.get("/purchase")
def purchase(request: Request, who: Owner):
    return page(request, "purchase.html", who)


@router.post("/logout")
def logout(request: Request, who: Owner, form: Form):
    request.session.clear()
    return back("/")
