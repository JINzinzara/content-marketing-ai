"""선택형 스타일과 기존 게시물을 통해 확인된 가게 프로필을 항목별로 조합하고 스타일 보존"""

import base64
import json
import time

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

FIELDS = {
    "palette": "색감",
    "lighting": "조명",
    "background": "배경",
    "composition": "구도·여백",
    "typography": "글자 배치",
    "voice": "말투",
}
PRESET_VERSION = 1
# 선택지와 속성은 사용자 검증 전의 초기 디자인을 가정함
PRESETS = {
    "keep": {
        "label": "우리 가게 분위기·스타일",
        "description": "기존 분위기 유지",
        "attributes": {},
    },
    "natural": {
        "label": "내추럴",
        "description": "따뜻한 배경과 부드러운 조명",
        "attributes": {
            "palette": "따뜻한 중성색과 베이지 배경",
            "lighting": "부드러운 자연광과 약한 그림자",
            "background": "목재·종이 소재, 적은 소품",
            "composition": "상품 중심의 편안한 여백",
        },
    },
    "minimal": {
        "label": "미니멀",
        "description": "심플한 배경과 여백의 미",
        "attributes": {
            "palette": "밝은 중성색 배경",
            "lighting": "균일하고 부드러운 조명",
            "background": "단순한 배경, 소품 최소화",
            "composition": "상품 중앙, 넓은 여백",
        },
    },
    "modern": {
        "label": "모던",
        "description": "무채색과 정돈된 구도",
        "attributes": {
            "palette": "차분한 무채색 배경",
            "lighting": "정돈된 대비와 부드러운 그림자",
            "background": "단정한 석재·단색 배경",
            "composition": "정돈된 선과 분명한 상품 중심",
        },
    },
    "fresh": {
        "label": "브리즈",
        "description": "밝은 배경과 가벼운 포인트",
        "attributes": {
            "palette": "밝은 배경과 가벼운 포인트색",
            "lighting": "밝고 확산된 조명",
            "background": "깔끔하고 가벼운 배경",
            "composition": "넓은 여백과 선명한 상품 중심",
        },
    },
}


class Attributes(BaseModel):
    """추측을 빈 문자열로 남길 수 있는 관찰 항목"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    palette: str = Field(max_length=250)
    lighting: str = Field(max_length=250)
    background: str = Field(max_length=250)
    composition: str = Field(max_length=250)
    typography: str = Field(max_length=250)
    voice: str = Field(max_length=250)


class Style(BaseModel):
    """모델이 제안하고 사용자가 확인할 구조화된 가게 스타일"""

    model_config = ConfigDict(extra="forbid")
    summary: str = Field(min_length=1, max_length=1800)
    attributes: Attributes
    evidence: list[str] = Field(max_length=12)
    uncertainties: list[str] = Field(max_length=12)


def latest_profile(con, owner_id, status="confirmed"):
    row = con.execute(
        "SELECT * FROM style_profiles WHERE owner_id=? AND status=? ORDER BY version DESC LIMIT 1",
        (owner_id, status),
    ).fetchone()
    return (
        {
            **dict(row),
            "content": json.loads(row["content"]),
            "reference_ids": json.loads(row["reference_ids"]),
        }
        if row
        else None
    )


def reference_list(con, owner_id):
    """사용자가 업로드한 사진과 연결된 본인 게시물의 선택 목록을 반환"""
    local = [
        {
            "id": "local:" + r["id"],
            "caption": r["caption"],
            "kind": "업로드한 참고 사진",
            "preview": "/shop/references/" + r["id"],
        }
        for r in con.execute(
            "SELECT * FROM style_references WHERE owner_id=? AND deleted_at IS NULL ORDER BY created_at DESC, rowid DESC",
            (owner_id,),
        )
    ]
    history = [
        {
            "id": "ig:" + r["media_id"],
            "caption": r["caption"],
            "kind": "Instagram · " + str(r["media_type"]),
            "preview": (
                r["media_url"]
                if r["media_type"] == "IMAGE"
                and str(r["media_url"]).startswith("https://")
                else ""
            ),
        }
        for r in con.execute(
            "SELECT * FROM history WHERE owner_id=? AND deleted_at IS NULL ORDER BY published_at DESC",
            (owner_id,),
        )
    ]
    return history + local


def reference_data(settings, con, owner_id, reference_id, *, include_image=True, skip_deleted=False):
    """소유권을 검사한 참조 본문과 API에 전달할 이미지 주소 반환"""
    kind, _, identifier = reference_id.partition(":")
    if kind == "local":
        row = con.execute(
            "SELECT * FROM style_references WHERE id=? AND owner_id=?",
            (identifier, owner_id),
        ).fetchone()
        if row and row["deleted_at"] is not None:
            if skip_deleted:
                return None
            row = None
        if row:
            image = ""
            if include_image:
                raw = (settings.data_dir / "references" / row["filename"]).read_bytes()
                image = "data:image/jpeg;base64," + base64.b64encode(raw).decode()
            return {
                "id": reference_id,
                "caption": row["caption"],
                "image": image,
            }
    elif kind == "ig":
        row = con.execute(
            "SELECT * FROM history WHERE media_id=? AND owner_id=?",
            (identifier, owner_id),
        ).fetchone()
        if row and row["deleted_at"] is not None:
            if skip_deleted:
                return None
            row = None
        if row:
            image = (
                row["media_url"]
                if include_image and row["media_type"] == "IMAGE"
                and str(row["media_url"]).startswith("https://")
                else ""
            )
            return {"id": reference_id, "caption": row["caption"], "image": image}
    raise HTTPException(404, "본인 계정의 참고 게시물을 선택해주세요")


def save_profile(con, owner_id, content, reference_ids, status):
    """기존 프로필을 덮어쓰지 않고 새로운 버전 ID를 반환"""
    version = con.execute(
        "SELECT COALESCE(MAX(version), 0)+1 FROM style_profiles WHERE owner_id=?",
        (owner_id,),
    ).fetchone()[0]
    con.execute(
        "INSERT INTO style_profiles(owner_id, version, status, content, reference_ids, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (
            owner_id,
            version,
            status,
            json.dumps(content, ensure_ascii=False),
            json.dumps(reference_ids),
            int(time.time()),
        ),
    )
    return version


def resolve(con, owner_id, preset_id):
    """고정 규칙 -> 선택 -> 기존 특징 -> 기본값 순서대로 스타일 조합

    Returns:
        dict: 프리셋·프로필 버전, 최종 항목, 출처, 충돌, 브랜드와 참고 ID
    """
    if preset_id not in PRESETS:
        raise HTTPException(422, "스타일을 선택해주세요")
    shop = dict(
        con.execute("SELECT * FROM accounts WHERE id=?", (owner_id,)).fetchone()
    )
    profile = latest_profile(con, owner_id)
    content = (
        profile["content"]
        if profile
        else {"summary": shop["style"], "attributes": {}, "locked_fields": []}
    )
    existing, locked = content.get("attributes", {}), content.get("locked_fields", [])
    selected = PRESETS[preset_id]["attributes"]
    attributes, sources, conflicts = {}, {}, []
    for field in FIELDS:
        old, new = existing.get(field, ""), selected.get(field, "")
        if field in locked and old:
            attributes[field], sources[field] = old, "사용자 고정"
            if new and new != old:
                conflicts.append({"field": FIELDS[field], "selected": new, "kept": old})
        elif new:
            attributes[field], sources[field] = new, "새로운 스타일 선택"
        else:
            attributes[field], sources[field] = old or "별도의 스타일 없음", (
                "가게 프로필" if old else "기본값"
            )
    return {
        "preset_id": preset_id,
        "preset_version": PRESET_VERSION,
        "preset_label": PRESETS[preset_id]["label"],
        "profile_version": profile["version"] if profile else 0,
        "summary": content.get("summary", ""),
        "attributes": attributes,
        "sources": sources,
        "conflicts": conflicts,
        "reference_ids": profile["reference_ids"] if profile else [],
        "brand": {
            "color": shop["color"],
            "name": shop["name"],
        },
    }


def post_style(post):
    return json.loads(post.get("style_snapshot") or "{}")
