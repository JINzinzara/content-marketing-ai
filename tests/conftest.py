"""테스트용 임시 저장소·웹 클라이언트·사진·모의 외부 응답을 제공"""

import io
import re

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.config import Settings
from app.main import create_app


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path / "data")


@pytest.fixture
def app(settings):
    def unexpected(request):
        raise AssertionError("테스트 중 예상치 못한 외부 요청: " + request.url.path)

    with httpx.Client(transport=httpx.MockTransport(unexpected)) as external:
        yield create_app(settings, external)


@pytest.fixture
def client(app):
    with TestClient(app) as instance:
        yield instance


@pytest.fixture
def csrf(client):
    html = client.get("/posts/new").text
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


@pytest.fixture
def photo():
    out = io.BytesIO()
    Image.new("RGB", (640, 480), (32, 80, 120)).save(out, "PNG")
    return out.getvalue()


@pytest.fixture
def new_post(client, csrf, photo):
    def create(**change):
        data = {
            "csrf": csrf,
            "product_name": "몽블랑 라떼",
            "facts": "직접 조리한 몽블랑 크림",
            "price": "6,500원",
            "period": "10월 한정",
            "purpose": "신메뉴 안내",
            "layout": "frame",
            "aspect": "portrait",
            **change,
        }
        response = client.post(
            "/posts",
            data=data,
            files={"photo": ("cafenew2.jpg", photo, "image/png")},
            follow_redirects=False,
        )
        assert response.status_code == 303, response.text
        return response.headers["location"].split("/")[-1]

    return create
