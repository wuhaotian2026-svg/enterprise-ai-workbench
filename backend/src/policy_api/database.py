from __future__ import annotations

from urllib.parse import urlparse

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


def assert_test_database_url(database_url: str) -> None:
    database_name = urlparse(database_url.replace("postgresql+psycopg", "postgresql")).path.lstrip("/")
    if not database_name.endswith("_test"):
        raise ValueError("Integration tests may only use a database whose name ends with '_test'.")


def create_database_engine(database_url: str, *, echo: bool = False) -> Engine:
    return create_engine(database_url, echo=echo, pool_pre_ping=True)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
