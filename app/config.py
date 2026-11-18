from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://clinically:clinically@localhost:5432/clinically"
    cookie_secure: bool = False
    session_ttl_days: int = 30
    # The website's address. Live updates (Socket.IO) only accept connections from it.
    # Several, comma-separated, are allowed.
    frontend_origin: str = "http://localhost:3000"
    # Where uploaded prescriptions and reports are kept (MedicalRecord.file_key is relative to it).
    records_dir: str = "storage/records"
    # Doctor photos and other public images.
    media_dir: str = "storage/media"
    # Where browsers fetch public images (clinic photos). Empty: served by this app at /api/media.
    # In production, the CDN in front of the S3 bucket, e.g. https://cdn.clinically.in
    media_public_base_url: str = ""
    # Where uploaded images are kept: "local" files under MEDIA_DIR, or "db" rows in Postgres
    # (for hosts whose disk is wiped on restart). See app/storage.py.
    storage_backend: Literal["local", "db"] = "local"

    # How messages reach patients. "log" only writes them to the server log (development);
    # "fake" keeps them in memory (tests). A real SMS/WhatsApp sender plugs in here (app/messaging.py).
    notify_channel: Literal["log", "fake"] = "log"

    # "fake" needs no keys and is for local dev and tests only.
    payment_gateway: Literal["razorpay", "fake"] = "razorpay"
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""

    # New doctors appear to patients straight after registering (for the free-trial rollout).
    # False: an admin sets doctors.is_verified after checking their registration number.
    auto_verify_doctors: bool = False
    # Free trial length for a new doctor account (one account covers all its clinics).
    trial_days: int = 90

    @field_validator("database_url")
    @classmethod
    def _psycopg_driver(cls, v: str) -> str:
        # Hosts (Neon, Render) hand out postgres:// or postgresql:// URLs; use the psycopg 3 driver.
        for prefix in ("postgres://", "postgresql://"):
            if v.startswith(prefix):
                return "postgresql+psycopg://" + v[len(prefix):]
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
