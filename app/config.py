from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://clinically:clinically@localhost:5432/clinically"
    cookie_secure: bool = False
    session_ttl_days: int = 30


@lru_cache
def get_settings() -> Settings:
    return Settings()
