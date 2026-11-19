"""Where uploaded images are kept: clinic photos (public) and doctors' photos (served only by
checked routes, after consent).

STORAGE_BACKEND picks the place:
- "local" writes files under MEDIA_DIR (development).
- "db" keeps them in Postgres (stored_files). For hosts whose disk is wiped on every restart or
  deploy (e.g. Render's free plan), with no extra service. Fine for the photos of a few hundred
  clinics; move to S3 with a CDN (MEDIA_PUBLIC_BASE_URL) when that grows.

Public images are served at /api/media/<key> (keys starting with public/) unless
MEDIA_PUBLIC_BASE_URL points at a CDN.
"""

from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from app.config import get_settings

PUBLIC_PREFIX = "public/"  # public images; anything else (e.g. doctors/) is never served by /api/media


class Storage(Protocol):
    def put(self, key: str, data: bytes, content_type: str) -> None: ...
    def delete(self, key: str) -> None: ...
    def get(self, key: str) -> bytes | None: ...


class LocalStorage:
    def __init__(self, root: str):
        self.root = Path(root).resolve()

    def _file(self, key: str) -> Path | None:
        path = (self.root / key).resolve()
        return path if path.is_relative_to(self.root) and path != self.root else None

    def put(self, key: str, data: bytes, content_type: str) -> None:
        path = self._file(key)
        if path is None:
            raise ValueError(f"Bad storage key {key!r}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def delete(self, key: str) -> None:
        path = self._file(key)
        if path is not None:
            path.unlink(missing_ok=True)

    def get(self, key: str) -> bytes | None:
        path = self._file(key)
        return path.read_bytes() if path is not None and path.is_file() else None


class DbStorage:
    """Files as rows in stored_files. Each call uses its own short transaction."""

    def put(self, key: str, data: bytes, content_type: str) -> None:
        from app.db import SessionLocal
        from app.models import StoredFile

        with SessionLocal() as db:
            stmt = insert(StoredFile).values(key=key, data=data, content_type=content_type, created_at=datetime.now(timezone.utc))
            db.execute(stmt.on_conflict_do_update(index_elements=["key"], set_={"data": data, "content_type": content_type}))
            db.commit()

    def delete(self, key: str) -> None:
        from app.db import SessionLocal
        from app.models import StoredFile

        with SessionLocal() as db:
            db.execute(delete(StoredFile).where(StoredFile.key == key))
            db.commit()

    def get(self, key: str) -> bytes | None:
        from app.db import SessionLocal
        from app.models import StoredFile

        with SessionLocal() as db:
            return db.scalar(select(StoredFile.data).where(StoredFile.key == key))


def get_storage() -> Storage:
    settings = get_settings()
    return _db() if settings.storage_backend == "db" else _local(settings.media_dir)


def get_record_storage() -> Storage:
    """Prescriptions and reports: never public. Locally they live under RECORDS_DIR."""
    settings = get_settings()
    return _db() if settings.storage_backend == "db" else _local(settings.records_dir)


@lru_cache
def _local(root: str) -> LocalStorage:
    return LocalStorage(root)


@lru_cache
def _db() -> DbStorage:
    return DbStorage()


def public_url(key: str | None) -> str | None:
    if not key:
        return None
    base = get_settings().media_public_base_url.rstrip("/")
    return f"{base}/{key}" if base else f"/api/media/{key}"
