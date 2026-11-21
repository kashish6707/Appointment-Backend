from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

# connect_timeout: a database that doesn't answer fails in seconds instead of hanging a request
# (or a deploy) forever. Neon waking from sleep takes about a second.
engine = create_engine(get_settings().database_url, pool_pre_ping=True, connect_args={"connect_timeout": 15})
SessionLocal = sessionmaker(engine, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    with SessionLocal() as db:
        yield db
