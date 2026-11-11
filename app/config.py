from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://clinically:clinically@localhost:5432/clinically"
    cookie_secure: bool = False
    session_ttl_days: int = 30
    # The website's address. Live updates (Socket.IO) only accept connections from it.
    frontend_origin: str = "http://localhost:3000"
    # Where uploaded prescriptions and reports are kept (MedicalRecord.file_key is relative to it).
    records_dir: str = "storage/records"
    # Doctor photos and other public images.
    media_dir: str = "storage/media"

    # How messages reach patients. "log" only writes them to the server log (development);
    # "fake" keeps them in memory (tests). A real SMS/WhatsApp sender plugs in here (app/messaging.py).
    notify_channel: Literal["log", "fake"] = "log"

    # "fake" needs no keys and is for local dev and tests only.
    payment_gateway: Literal["razorpay", "fake"] = "razorpay"
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
