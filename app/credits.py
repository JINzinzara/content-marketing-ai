"""완성본을 저장하고 최초 확정에만 제작권 1권을 차감"""

import time

from fastapi import HTTPException

from .db import balance, connect, owned_post
from .rendering import atomic_write, render


def finalize(settings, owner_id, post_id, *, expected_revision=None):
    """소유권·버전을 검사하고 파일 저장과 차감 후 최신 업로드된 게시물 dict을 반환"""
    with connect(settings, write=True) as con:
        post = owned_post(con, owner_id, post_id)
        if expected_revision is not None and post["revision"] != expected_revision:
            raise HTTPException(
                409,
                "최근 확정 후 내용이 변경됐습니다. 다시 미리보기를 확인하고 확정해주세요",
            )
        charged = con.execute(
            "SELECT 1 FROM credits WHERE owner_id=? AND reference=?",
            (owner_id, "post:" + post_id),
        ).fetchone()
        if not charged and balance(con, owner_id) < 1:
            raise HTTPException(
                402,
                "무료 체험 5건을 모두 사용하였습니다. 이후 작업에는 제작권 구매가 필요합니다",
            )
        if post["rendered_revision"] != post["revision"]:
            if post["publish_state"] in {
                "creating",
                "container_ready",
                "publishing",
                "unknown",
            }:
                raise HTTPException(
                    409, "게시 상태를 확인 주에는 완성본을 변경할 수 없습니다"
                )
            shop = con.execute(
                "SELECT name FROM accounts WHERE id=?", (owner_id,)
            ).fetchone()
            filename = f"{post_id}-{post['revision']}.jpg"
            atomic_write(
                settings.data_dir / "finished" / filename,
                render(settings, post, shop["name"]),
            )
            con.execute(
                "UPDATE posts SET finished_file=?, rendered_revision=?, finalized_at=COALESCE(finalized_at, ?) WHERE id=?",
                (filename, post["revision"], int(time.time()), post_id),
            )
        if not charged:
            con.execute(
                "INSERT INTO credits(owner_id, reference, amount, created_at) VALUES (?, ?, -1, ?)",
                (owner_id, "post:" + post_id, int(time.time())),
            )
        return owned_post(con, owner_id, post_id)
