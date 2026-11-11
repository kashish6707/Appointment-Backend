from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://clinically:clinically@localhost:5432/clinically"
    cookie_secure: bool = False
    session_ttl_days: int = 30

    # "fake" needs no keys and is for local dev and tests only.
    payment_gateway: Literal["razorpay", "fake"] = "razorpay"
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
