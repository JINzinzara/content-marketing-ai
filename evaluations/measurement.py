"""평가 실행에만 계측·대조군 지시문을 적용한다. 서비스 TextAI는 수정하지 않는다."""

import time

from app.styles import FIELDS
from app.text_ai import TextAI


class MeasuredTextAI(TextAI):
    def __init__(self, settings, client, *, instructions=None, timing=None, trace=None):
        super().__init__(settings, client)
        self.instructions = instructions
        self.timing = timing if timing is not None else {}
        self.trace = trace if trace is not None else {}

    def _json(self, path, payload):
        if self.instructions is not None:
            payload = {**payload, "instructions": self.instructions}
        if path == "responses":
            self.trace["request"] = payload
        start = time.perf_counter()
        try:
            response = super()._json(path, payload)
            if path == "responses":
                self.trace["response"] = response
                self.timing["returned_model"] = response.get("model")
            return response
        finally:
            key = "generation_api_ms" if path == "responses" else "preflight_api_ms"
            self.timing[key] = (time.perf_counter() - start) * 1000


def fill_blanks(existing, proposed):
    """자동 스타일 입력 후보의 빈칸 병합 규칙을 평가한다."""
    return {key: str(existing.get(key) or "").strip() or str(proposed.get(key) or "").strip()
            for key in FIELDS}
