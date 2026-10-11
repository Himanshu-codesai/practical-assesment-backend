from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings

engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    with SessionLocal() as db:
        yield db


def init_db() -> None:
    from app import models  # noqa: F401  (registers the tables on Base)

    Base.metadata.create_all(engine)
    # create_all only creates missing tables; it never alters one that exists. This adds
    # the one column introduced later. A growing schema would use Alembic migrations.
    with engine.begin() as conn:
        conn.execute(text(
            "ALTER TABLE videos ADD COLUMN IF NOT EXISTS "
            "transcript_method VARCHAR(20) NOT NULL DEFAULT 'auto'"))
