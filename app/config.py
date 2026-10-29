"""문구·스타일 분석·이미지 모델 설정과 로컬 저장·인증·통합 예산 검증"""

import os
import secrets
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse

from cryptography.fernet import Fernet
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: ROOT / "data")
    environment: str = "local"
    public_url: str = ""
    session_secret: str = field(default="", repr=False)
    encryption_key: str = field(default="", repr=False)
    instagram_app_id: str = ""
    instagram_app_secret: str = field(default="", repr=False)
    instagram_redirect_uri: str = ""
    instagram_version: str = "v25.0"
    openai_enabled: bool = False
    openai_key: str = field(default="", repr=False)
    openai_model: str = "gpt-5.4-mini"
    style_input_ceiling_rate: Decimal = Decimal("0.75")
    style_output_rate: Decimal = Decimal("4.50")
    recommendation_model: str = "gpt-5.4"
    draft_model: str = "gpt-5.4-mini"
    draft_input_ceiling_rate: Decimal = Decimal("0.75")
    draft_output_rate: Decimal = Decimal("4.50")
    image_enabled: bool = False
    image_model: str = "gpt-image-2"
    image_quality: str = "low"
    image_attempts_per_post: int = 2
    image_calls_per_account: int = 10
    image_reserve_usd: Decimal = Decimal("0")
    image_text_rate: Decimal = Decimal("0")
    image_input_rate: Decimal = Decimal("0")
    image_output_rate: Decimal = Decimal("0")
    ai_budget_usd: Decimal = Decimal("0")
    input_ceiling_rate: Decimal = Decimal("0")
    output_rate: Decimal = Decimal("0")
    max_output_tokens: int = 2048
    ai_attempts_per_post: int = 3
    ai_calls_per_account: int = 20
    history_page_size: int = 20
    comparison_hours: int = 24
    comparison_tolerance_hours: int = 2

    @classmethod
    def from_env(cls):
        """설정을 담은 Setting 객체 생성"""
        load_dotenv(ROOT / ".env", override=False)
        path = Path(os.getenv("DATA_DIR", str(ROOT / "data")))
        if not path.is_absolute():
            path = ROOT / path
        return cls(
            data_dir=path,
            environment=os.getenv("APP_ENV", "local"),
            public_url=os.getenv("PUBLIC_BASE_URL", "").rstrip("/"),
            session_secret=os.getenv("SESSION_SECRET", ""),
            encryption_key=os.getenv("TOKEN_ENCRYPTION_KEY", ""),
            instagram_app_id=os.getenv("INSTAGRAM_APP_ID", ""),
            instagram_app_secret=os.getenv("INSTAGRAM_APP_SECRET", ""),
            instagram_redirect_uri=os.getenv("INSTAGRAM_REDIRECT_URI", ""),
            instagram_version=os.getenv("INSTAGRAM_API_VERSION", "v25.0"),
            openai_enabled=os.getenv("OPENAI_ENABLED", "false").lower() == "true",
            openai_key=os.getenv("OPENAI_API_KEY", ""),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-5.4-mini"),
            style_input_ceiling_rate=Decimal(os.getenv("OPENAI_STYLE_INPUT_CEILING_USD_PER_MILLION", "0.75")),
            style_output_rate=Decimal(os.getenv("OPENAI_STYLE_OUTPUT_USD_PER_MILLION", "4.50")),
            recommendation_model=os.getenv("OPENAI_RECOMMENDATION_MODEL", "gpt-5.4"),
            draft_model=os.getenv("OPENAI_DRAFT_MODEL", "gpt-5.4-mini"),
            draft_input_ceiling_rate=Decimal(os.getenv("OPENAI_DRAFT_INPUT_CEILING_USD_PER_MILLION", "0.75")),
            draft_output_rate=Decimal(os.getenv("OPENAI_DRAFT_OUTPUT_USD_PER_MILLION", "4.50")),
            image_enabled=os.getenv("OPENAI_IMAGE_ENABLED", "false").lower() == "true",
            image_model=os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-2"),
            image_quality=os.getenv("OPENAI_IMAGE_QUALITY", "low"),
            image_attempts_per_post=int(os.getenv("IMAGE_ATTEMPTS_PER_POST", "2")),
            image_calls_per_account=int(os.getenv("IMAGE_CALLS_PER_ACCOUNT", "10")),
            image_reserve_usd=Decimal(os.getenv("IMAGE_CALL_RESERVE_USD", "0")),
            image_text_rate=Decimal(os.getenv("IMAGE_TEXT_INPUT_USD_PER_MILLION", "0")),
            image_input_rate=Decimal(os.getenv("IMAGE_INPUT_USD_PER_MILLION", "0")),
            image_output_rate=Decimal(os.getenv("IMAGE_OUTPUT_USD_PER_MILLION", "0")),
            ai_budget_usd=Decimal(os.getenv("OPENAI_BUDGET_USD", "0")),
            input_ceiling_rate=Decimal(
                os.getenv("OPENAI_INPUT_CEILING_USD_PER_MILLION", "0")
            ),
            output_rate=Decimal(os.getenv("OPENAI_OUTPUT_USD_PER_MILLION", "0")),
            max_output_tokens=int(os.getenv("OPENAI_MAX_OUTPUT_TOKENS", "2048")),
            ai_attempts_per_post=int(os.getenv("AI_ATTEMPTS_PER_POST", "3")),
            ai_calls_per_account=int(os.getenv("AI_CALLS_PER_ACCOUNT", "20")),
            history_page_size=int(os.getenv("HISTORY_PAGE_SIZE", "20")),
            comparison_hours=int(os.getenv("COMPARISON_HOURS", "24")),
            comparison_tolerance_hours=int(
                os.getenv("COMPARISON_TOLERANCE_HOURS", "2")
            ),
        )

    @property
    def redirect_url(self):
        return (
            self.instagram_redirect_uri or self.public_url + "/auth/instagram/callback"
        )

    @property
    def instagram_ready(self):
        return bool(
            self.instagram_app_id
            and self.instagram_app_secret
            and self.public_url.startswith("https://")
            and self.redirect_url == self.public_url + "/auth/instagram/callback"
        )

    def text_parameters(self, purpose):
        return {
            "style": (self.openai_model, self.style_input_ceiling_rate, self.style_output_rate),
            "draft": (self.draft_model, self.draft_input_ceiling_rate, self.draft_output_rate),
            "recommendation": (self.recommendation_model, self.input_ceiling_rate, self.output_rate),
        }[purpose]

    def text_missing(self, purpose):
        model, incoming, outgoing = self.text_parameters(purpose)
        prefix = {"style": "OPENAI_STYLE_", "draft": "OPENAI_DRAFT_", "recommendation": "OPENAI_"}[purpose]
        model_key = {"style": "OPENAI_MODEL", "draft": "OPENAI_DRAFT_MODEL", "recommendation": "OPENAI_RECOMMENDATION_MODEL"}[purpose]
        checks = {
            "OPENAI_ENABLED=true": self.openai_enabled,
            "OPENAI_API_KEY": bool(self.openai_key),
            "OPENAI_BUDGET_USD > 0": self.ai_budget_usd > 0,
            model_key: bool(model),
            prefix + "INPUT_CEILING_USD_PER_MILLION > 0": incoming > 0,
            prefix + "OUTPUT_USD_PER_MILLION > 0": outgoing > 0,
        }
        return [key for key, ready in checks.items() if not ready]

    @property
    def ai_missing(self):
        return list(dict.fromkeys(self.text_missing("style") + self.text_missing("draft")))

    @property
    def ai_ready(self):
        return not self.ai_missing

    @property
    def image_missing(self):
        """비밀값 없이 이미지 AI에 부족한 설정 이름만 반환한다."""
        checks = {
            "OPENAI_IMAGE_ENABLED=true": self.image_enabled,
            "OPENAI_API_KEY": bool(self.openai_key),
            "OPENAI_IMAGE_MODEL=gpt-image-2": self.image_model == "gpt-image-2",
            "OPENAI_BUDGET_USD > 0": self.ai_budget_usd > 0,
            "IMAGE_CALL_RESERVE_USD > 0": self.image_reserve_usd > 0,
            "IMAGE_TEXT_INPUT_USD_PER_MILLION > 0": self.image_text_rate > 0,
            "IMAGE_INPUT_USD_PER_MILLION > 0": self.image_input_rate > 0,
            "IMAGE_OUTPUT_USD_PER_MILLION > 0": self.image_output_rate > 0,
        }
        return [name for name, ready in checks.items() if not ready]

    @property
    def image_ready(self):
        return not self.image_missing

    def prepare(self):
        """저장 폴더와 필요한 로컬 키 준비"""
        if self.environment not in {"local", "production"}:
            raise ValueError("APP_ENV는 local 또는 production이어야 합니다")
        for value in (
            self.ai_budget_usd,
            self.input_ceiling_rate,
            self.output_rate,
            self.style_input_ceiling_rate,
            self.style_output_rate,
            self.draft_input_ceiling_rate,
            self.draft_output_rate,
            self.image_reserve_usd,
            self.image_text_rate,
            self.image_input_rate,
            self.image_output_rate,
        ):
            if not value.is_finite() or value < 0:
                raise ValueError("비용과 단가는 유한한 음수 아닌 값이어야 합니다")
        if self.image_model != "gpt-image-2" or self.image_quality not in {
            "low",
            "medium",
            "high",
        }:
            raise ValueError(
                "이미지 모델은 gpt-image-2, 품질은 low·medium·high 중에서 선택해주세요"
            )
        if min(self.image_attempts_per_post, self.image_calls_per_account) < 1:
            raise ValueError("이미지 호출 한도는 양수여야 합니다.")
        if not 256 <= self.max_output_tokens <= 8192:
            raise ValueError("출력 토큰 한도는 256~8192 범위로 설정하세요")
        if not 1 <= self.history_page_size <= 50:
            raise ValueError("게시물 조회 수는 1~50 범위로 설정하세요")
        if (
            min(
                self.ai_attempts_per_post,
                self.ai_calls_per_account,
                self.comparison_hours,
            )
            < 1
        ):
            raise ValueError("호출·비교 설정은 양수여야 합니다")
        if not 0 <= self.comparison_tolerance_hours < self.comparison_hours:
            raise ValueError("비교 허용 오차가 비교 시점보다 작아야 합니다")
        if self.environment == "production":
            if (
                len(self.session_secret) < 32
                or not self.encryption_key
                or not self.instagram_ready
            ):
                raise ValueError(
                    "공개 모드에는 HTTPS, Instagram 설정, 세션·암호화 키가 필요합니다"
                )
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        for attr, filename, generator in (
            ("session_secret", ".session-secret", lambda: secrets.token_urlsafe(48)),
            ("encryption_key", ".token-key", lambda: Fernet.generate_key().decode()),
        ):
            if not getattr(self, attr):
                path = self.data_dir / filename
                try:
                    with open(
                        path, "x", opener=lambda p, flags: os.open(p, flags, 0o600)
                    ) as f:
                        f.write(generator())
                except FileExistsError:
                    pass
                setattr(self, attr, path.read_text().strip())
        Fernet(self.encryption_key.encode())
        for name in ("originals", "finished", "references", "generated"):
            (self.data_dir / name).mkdir(exist_ok=True, mode=0o700)

    @property
    def allowed_hosts(self):
        if self.environment == "local":
            return ["localhost", "127.0.0.1", "[::1]", "testserver"]
        return [urlparse(self.public_url).hostname]
