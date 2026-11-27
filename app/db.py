from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from app.config import settings

connect_args = (
    {"check_same_thread": False} if settings.database_url.startswith("sqlite")
    else {"connect_timeout": 5} if settings.database_url.startswith("postgresql") else {}
)
engine = create_engine(
    settings.database_url, future=True, connect_args=connect_args,
    hide_parameters=True, pool_pre_ping=not settings.database_url.startswith("sqlite"),
    **({"pool_timeout": 5} if settings.database_url.startswith("postgresql") else {}),
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
