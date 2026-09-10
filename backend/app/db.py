"""Async SQLAlchemy engine/session. Works with MySQL (aiomysql) and SQLite (aiosqlite)."""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import get_settings


class Base(DeclarativeBase):
    pass


_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine():
    global _engine
    if _engine is None:
        s = get_settings()
        kwargs: dict = {"pool_pre_ping": True}
        if s.database_url.startswith("sqlite"):
            kwargs = {"connect_args": {"timeout": 30}}
        else:
            kwargs.update({"pool_recycle": 1800, "pool_size": 5, "max_overflow": 10})
        _engine = create_async_engine(s.database_url, **kwargs)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_factory


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency."""
    async with session_scope() as session:
        yield session


def is_sqlite() -> bool:
    return get_settings().database_url.startswith("sqlite")


async def init_db() -> None:
    from . import models  # noqa: F401  (register tables)

    engine = get_engine()
    async with engine.begin() as conn:
        if is_sqlite():
            from sqlalchemy import text

            await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_add_missing_columns)


def _add_missing_columns(conn) -> None:
    """Lightweight forward migration: add columns that exist in the models but not in the DB.

    ALTER TABLE cannot carry the Python-side default, so rows that already exist would keep NULL.
    Backfill the default afterwards, otherwise an upgraded database behaves differently from a fresh
    one (e.g. sessions.lang_chosen)."""
    from sqlalchemy import inspect, text

    insp = inspect(conn)
    for table in Base.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        existing = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in existing:
                continue
            ddl = col.type.compile(dialect=conn.dialect)
            conn.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {col.name} {ddl}"))
            default = col.default
            if default is not None and not default.is_callable and not default.is_clause_element:
                conn.execute(
                    text(f"UPDATE {table.name} SET {col.name} = :v WHERE {col.name} IS NULL"),
                    {"v": default.arg},
                )


async def dispose_db() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
