from functools import lru_cache
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

    environment: str = "development"
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
