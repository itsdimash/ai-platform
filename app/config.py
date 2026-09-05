from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # API-ключи провайдеров
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    gemini_api_key: str = ""

    # БД сервиса (логи, история чатов)
    database_url: str = "postgresql+asyncpg://ai_platform:password@localhost:5432/ai_platform"

    # Read-only доступ к БД ERP
    erp_readonly_database_url: str = ""

    # JWT — общий секрет с ERP-бэкендом
    jwt_secret: str = "change-me"
    jwt_algorithm: str = "HS256"

    # CORS — список разрешённых origin'ов через запятую, напр.
    # "https://erp.kerneu.local,https://erp.kerneu.kz"
    cors_allowed_origins: str = ""

    environment: str = "development"
    log_level: str = "INFO"

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_allowed_origins.split(",") if origin.strip()]

    @model_validator(mode="after")
    def _reject_insecure_defaults_outside_dev(self) -> "Settings":
        """Небезопасные дефолты допустимы только в development. Если
        ENVIRONMENT выставлен во что-то ещё, а JWT_SECRET не задан в .env —
        падаем при старте, а не отдаём сервис, который принимает
        самоподписанные токены."""

        if self.environment != "development" and (not self.jwt_secret or self.jwt_secret == "change-me"):
            raise ValueError(
                "JWT_SECRET не задан или оставлен пустым/дефолтным ('change-me') "
                f"при ENVIRONMENT={self.environment!r}. Задайте реальный секрет в .env "
                "перед деплоем вне development."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
