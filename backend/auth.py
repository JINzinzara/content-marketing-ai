"""요청 소유자·CSRF·일회성 OAuth 상태를 확인하고 연결 토큰을 암호화"""

import hashlib
import secrets
import time
from urllib.parse import urlencode

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, Request

from .db import account, connect, ensure_account


def owner(request: Request):
    settings = request.app.state.settings
    identifier = request.session.get("owner_id")
    if settings.environment == "local":
        identifier = "local-owner"
        request.session["owner_id"] = identifier
        with connect(settings, write=True) as con:
            ensure_account(con, identifier)
    if not identifier or not account(settings, identifier):
        raise HTTPException(401, "먼저 Instagram 계정을 연결해주세요")
    return identifier


def csrf_token(request):
    if "csrf" not in request.session:
        request.session["csrf"] = secrets.token_urlsafe(32)
    return request.session["csrf"]


async def form_data(request: Request):
    # 선택 ID는 JSON 필드로 묶고 전체 요청의 기존 10MB 제한을 유지한다.
    part_limit = 10 * 1024 * 1024 if request.url.path == "/shop/analyze" else 16_384
    async with request.form(max_files=1, max_fields=40, max_part_size=part_limit) as form:
        actual = str(form.get("csrf", ""))
        expected = request.session.get("csrf", "")
        if not expected or not secrets.compare_digest(actual, expected):
            raise HTTPException(
                403, "요청을 확인할 수 없습니다. 화면을 새로고침 또는 다시 시도해주세요"
            )
        yield form


def authorization_url(request):
    settings = request.app.state.settings
    if not settings.instagram_ready:
        raise HTTPException(503, "Instagram 앱·HTTPS·콜백 설정을 먼저 준비해주세요")
    state = secrets.token_urlsafe(32)
    request.session["oauth_state"] = state
    with connect(settings, write=True) as con:
        con.execute(
            "DELETE FROM oauth_states WHERE expires_at < ?", (int(time.time()),)
        )
        con.execute(
            "INSERT INTO oauth_states(state_hash, owner_id, expires_at) VALUES (?, ?, ?)",
            (
                hashlib.sha256(state.encode()).hexdigest(),
                request.session.get("owner_id"),
                int(time.time()) + 600,
            ),
        )
    return "https://api.instagram.com/oauth/authorize?" + urlencode(
        {
            "client_id": settings.instagram_app_id,
            "redirect_uri": settings.redirect_url,
            "response_type": "code",
            "state": state,
            "enable_fb_login": "0",
            "scope": "instagram_business_basic,instagram_business_content_publish,instagram_business_manage_insights",
        }
    )


def consume_oauth_state(request, state):
    expected = request.session.pop("oauth_state", "")
    if not state or not expected or not secrets.compare_digest(state, expected):
        raise HTTPException(400, "게정 연결 요청이 만료되었습니다. 다시 연결해주세요")
    with connect(request.app.state.settings, write=True) as con:
        key = hashlib.sha256(state.encode()).hexdigest()
        row = con.execute(
            "SELECT * FROM oauth_states WHERE state_hash=?", (key,)
        ).fetchone()
        con.execute("DELETE FROM oauth_states WHERE state_hash=?", (key,))
        if not row or row["expires_at"] < time.time():
            raise HTTPException(400, "계정 연결 요청이 만료되었습니다")
        return row["owner_id"]


def encrypt(settings, token):
    return Fernet(settings.encryption_key.encode()).encrypt(token.encode())


def decrypt(settings, value):
    try:
        return Fernet(settings.encryption_key.encode()).decrypt(value).decode()
    except (InvalidToken, TypeError):
        raise HTTPException(
            401, "저장된 연결을 읽을 수 없습니다. 계정을 다시 연결해주세요"
        ) from None
