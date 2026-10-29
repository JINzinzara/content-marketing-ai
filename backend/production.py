"""브라우저 실측과 사용자가 기록한 수정 회차·품질을 버전별로 보관한다."""

from .db import connect, owned_post

LABELS = {"initial_preview": "문구와 원본 미리보기", "image_candidate": "이미지 생성 버튼부터 후보 준비",
          "creation_to_ai_candidate": "제작 시작부터 첫 AI 후보 준비(사용자 조작 포함)", "edit_preview": "수정 요청부터 미리보기"}


def report(settings, owner_id, post_id):
    with connect(settings) as con:
        post = owned_post(con, owner_id, post_id)
        timings = [dict(r) for r in con.execute("SELECT * FROM production_timings WHERE post_id=? AND owner_id=? ORDER BY created_at", (post_id, owner_id))]
        reviews = [dict(r) for r in con.execute("SELECT * FROM production_reviews WHERE post_id=? AND owner_id=? ORDER BY revision", (post_id, owner_id))]
        usage = [dict(r) for r in con.execute("SELECT model,purpose,state,cost_microusd,reserved_microusd FROM ai_calls WHERE post_id=? AND owner_id=?", (post_id, owner_id))]
    return {"revision": post["revision"], "timings": timings, "reviews": reviews, "ai_usage": usage,
            "known_cost_microusd": sum(r["cost_microusd"] for r in usage if r["cost_microusd"] is not None),
            "unknown_cost_count": sum(r["cost_microusd"] is None for r in usage)}
