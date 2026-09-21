###############################################################################
#  数据库 — SQLAlchemy async engine + session (SQLite)
###############################################################################

import os
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from app.config import load_settings

settings = load_settings()

# 确保 data 目录存在
_db_path = settings.database_url.replace("sqlite+aiosqlite:///", "")
_db_dir = os.path.dirname(_db_path)
if _db_dir and not os.path.exists(_db_dir):
    os.makedirs(_db_dir, exist_ok=True)

engine = create_async_engine(settings.database_url, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    """ORM 基类"""
    pass


async def init_db():
    """创建所有表 + 轻量补列迁移（SQLite 的 create_all 不会给已有表加列）"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_migrate_add_columns)


def _migrate_add_columns(conn):
    """幂等补列：先 PRAGMA table_info 看缺哪列，再 ALTER TABLE ADD COLUMN。

    为什么需要：Persona 新增了「弹幕聚合回复」4 个字段，老库（persona 表已存在）
    不会被 create_all 改结构 → 不补列会在 SELECT 时报 no such column。
    """
    from sqlalchemy import text
    from app.utils.logger import logger

    wanted = {
        "persona": [
            ("danmaku_policy", "TEXT DEFAULT ''"),
            ("danmaku_batch_trigger", "INTEGER DEFAULT 3"),
            ("danmaku_batch_wait", "FLOAT DEFAULT 3.0"),
            ("danmaku_max_chars", "INTEGER DEFAULT 60"),
        ],
    }
    for table, cols in wanted.items():
        try:
            rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
            have = {r[1] for r in rows}
            if not have:
                continue  # 表还不存在 → create_all 已经建好了完整结构
            for name, ddl in cols:
                if name not in have:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
                    logger.info(f"[migrate] {table} 补列: {name} {ddl}")
        except Exception as e:
            logger.warning(f"[migrate] {table} 补列失败: {e}")


async def get_db() -> AsyncSession:
    """FastAPI 依赖注入：获取数据库 session"""
    async with async_session() as session:
        try:
            yield session
        finally:
            await session.close()
