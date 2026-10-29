"""텍스트와 이미지 요청의 비용의 예산을 하나의 프로젝트 예산에서 관리"""

import time
import uuid
from decimal import Decimal, ROUND_CEILING

from fastapi import HTTPException


def microusd(value):
    """달러 금액 -> 백만분의 일 달러 정수로 변환"""
    return int((Decimal(value) * 1_000_000).to_integral_value(rounding=ROUND_CEILING))


def spent(con):
    """사용량 기반 추정 원가와 비용 미확정 요청의 금액의 합계 반환"""
    return con.execute(
        "SELECT COALESCE(SUM(COALESCE(cost_microusd, reserved_microusd)), 0) FROM ai_calls"
    ).fetchone()[0]


def reserve(con, settings, owner_id, purpose, model, amount, post_id=None):
    """쓰기 트랜잭션 안에서 통합 잔여 예산을 검사하고 호출 ID를 반환

    amount는 microusd 단위의 예상 금액. 이미지 금액은 API 청구 상한을
    알 수 없으므로 실제 사용량과 대조하고 초과 시 후속 호출 정지
    """
    overrun = con.execute(
        "SELECT 1 FROM ai_calls WHERE cost_microusd > reserved_microusd LIMIT 1"
    ).fetchone()
    if overrun or spent(con) + amount > microusd(settings.ai_budget_usd):
        raise HTTPException(
            402,
            "설정한 AI 비용 한도에 도달했거나 예상 금액을 넘는 비용이 확인되었습니다. 설정과 사용량을 확인해주세요",
        )
    identifier = uuid.uuid4().hex
    con.execute(
        "INSERT INTO ai_calls(id, owner_id, post_id, purpose, model, state, reserved_microusd, created_at) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
        (identifier, owner_id, post_id, purpose, model, amount, int(time.time())),
    )
    return identifier


def reconcile(con, call_id, amount, note):
    """쓰기 트랜잭션 안에서 확인한 청구액과 변경 전후 금액·근거를 보관한다."""
    if type(amount) is not int or not 0 <= amount <= 2**63 - 1:
        raise ValueError("정산 금액은 유효한 음수 아닌 microUSD 정수여야 합니다")
    if not isinstance(note, str) or not 1 <= len(note.strip()) <= 2000:
        raise ValueError("청구 확인 근거를 1~2000자로 남기세요")
    call = con.execute("SELECT * FROM ai_calls WHERE id=?", (call_id,)).fetchone()
    if not call:
        raise HTTPException(404, "정산할 요청을 찾을 수 없습니다")
    con.execute(
        "INSERT INTO ai_cost_reconciliations(call_id, reserved_before_microusd, cost_before_microusd, cost_after_microusd, note, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (call_id, call["reserved_microusd"], call["cost_microusd"], amount, note.strip(), int(time.time())),
    )
    con.execute(
        "UPDATE ai_calls SET cost_microusd=?, reserved_microusd=MAX(reserved_microusd, ?) WHERE id=?",
        (amount, amount, call_id),
    )
