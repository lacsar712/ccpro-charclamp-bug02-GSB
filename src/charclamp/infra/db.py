from __future__ import annotations

import os
from collections.abc import AsyncGenerator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker

from charclamp.domain.models import Base

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://charclamp:charclamp@127.0.0.1:6150/charclamp",
)
DATABASE_URL_SYNC = os.environ.get(
    "DATABASE_URL_SYNC",
    "postgresql+psycopg2://charclamp:charclamp@127.0.0.1:6150/charclamp",
)

engine = create_async_engine(DATABASE_URL, echo=False)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

sync_engine = create_engine(DATABASE_URL_SYNC, echo=False)
SyncSessionLocal = sessionmaker(sync_engine, expire_on_commit=False, class_=Session)


def sync_create_all() -> None:
    Base.metadata.create_all(sync_engine)
    sync_ensure_columns()


def sync_ensure_columns() -> None:
    """对已存在的表做幂等补列（create_all 不会给旧表加列）。

    存量 burn_shifts 没有乐观锁版本戳，启动时补齐并把既有行初始化为 1。
    """
    inspector = inspect(sync_engine)
    table_names = set(inspector.get_table_names())
    if "burn_shifts" not in table_names:
        return
    columns = {column["name"] for column in inspector.get_columns("burn_shifts")}
    if "row_version" not in columns:
        with sync_engine.begin() as conn:
            conn.execute(
                text(
                    "ALTER TABLE burn_shifts "
                    "ADD COLUMN row_version INTEGER NOT NULL DEFAULT 1"
                )
            )


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session
