import sys
import time
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError

from app.config import get_settings
from app.models import Base

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

# Tests pass their own URL; otherwise use DATABASE_URL.
url = config.attributes.get("database_url") or get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(url=url, target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def _connect(engine, attempts: int = 5):
    """Connect, retrying a few times: on deploy the database may be waking up, or the network
    slow to come up. Each try gives up after 15 seconds, so a deploy never hangs silently."""
    for attempt in range(1, attempts + 1):
        print(f"Migrations: connecting to the database (try {attempt} of {attempts})", file=sys.stderr, flush=True)
        try:
            return engine.connect()
        except OperationalError as e:
            if attempt == attempts:
                raise
            print(f"Migrations: couldn't connect ({str(e).splitlines()[0]}); trying again in 5 seconds", file=sys.stderr, flush=True)
            time.sleep(5)


def run_migrations_online() -> None:
    engine = create_engine(url, connect_args={"connect_timeout": 15})
    with _connect(engine) as connection:
        context.configure(connection=connection, target_metadata=Base.metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
