import os

from sqlalchemy.engine import make_url

# Point the app at the test database before anything imports app.db.
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://clinically:clinically@localhost:5432/clinically_test",
)
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["PAYMENT_GATEWAY"] = "fake"

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from datetime import time  # noqa: E402

from sqlalchemy import create_engine, text  # noqa: E402

from app.db import engine  # noqa: E402
from app.main import app  # noqa: E402
from app import events, security  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import WEEKDAYS, Base, Clinic, Doctor, DoctorClinic  # noqa: E402


def _create_test_database() -> None:
    url = make_url(TEST_DATABASE_URL)
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        exists = conn.scalar(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": url.database})
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    admin.dispose()


@pytest.fixture(scope="session", autouse=True)
def database():
    _create_test_database()
    cfg = Config("alembic.ini")
    cfg.attributes["database_url"] = TEST_DATABASE_URL
    cfg.attributes["configure_logger"] = False
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield


@pytest.fixture(autouse=True)
def clean_tables():
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    yield


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def db():
    with engine.connect() as conn:
        yield conn


@pytest.fixture
def make_slot():
    """Create a doctor sitting at a clinic. OPD runs all day, every day, unless overridden."""
    counter = iter(range(1, 1000))

    def make(
        *, verified=True, days=WEEKDAYS, start=time(0, 0), end=time(23, 59), token_amount=0, **doctor_fields
    ):
        n = next(counter)
        with SessionLocal() as s:
            doctor = Doctor(
                full_name=doctor_fields.pop("full_name", f"Dr Test {n}"),
                phone=doctor_fields.pop("phone", f"90000000{n:02d}"),
                password_hash=security.hash_password("secret123"),
                department=doctor_fields.pop("department", "Orthopaedics"),
                is_verified=verified,
                **doctor_fields,
            )
            clinic = Clinic(name=f"Test Clinic {n}", area="Boring Road", address=f"{n} Test Lane, Patna")
            slot = DoctorClinic(
                doctor=doctor,
                clinic=clinic,
                fee=400,
                token_amount=token_amount,
                opd_days=list(days),
                opd_start=start,
                opd_end=end,
            )
            s.add_all([doctor, clinic, slot])
            s.commit()
            return slot

    return make


@pytest.fixture
def emitted():
    """Every real-time event emitted during the test, as (room, event, payload)."""
    log = []
    unsubscribe = events.subscribe(lambda room, event, payload: log.append((room, event, payload)))
    yield log
    unsubscribe()


@pytest.fixture
def gateway():
    """The fake payment gateway, reset for each test."""
    from app.payments import _fake

    _fake.refunds.clear()
    _fake.fail_refunds = False
    yield _fake
    _fake.fail_refunds = False
