"""SQLAlchemy async（SQLite）—— 与原 LiveStream 同库结构，含轻量补列迁移

合并进 LiveTalking 后仍用同一个 `data/livestream.db`（沿用已配好人设/话术/设置），
只是不再由独立的 FastAPI 进程持有。
"""
import os

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from server.livestream.config import load_settings
from utils.logger import logger

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
    logger.info("[ls] 数据库就绪: %s", _db_path)


def _migrate_add_columns(conn):
    """幂等补列：先 PRAGMA table_info 看缺哪列，再 ALTER TABLE ADD COLUMN。"""
    from sqlalchemy import text

    # 合并版当初给 persona 加过 4 个「弹幕聚合」字段，现已按上游恢复、不再使用。
    # （SQLite 里留着那几列不影响 ORM：select 只取模型里映射的列。）
    wanted = {}
    for table, cols in wanted.items():
        try:
            rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
            have = {r[1] for r in rows}
            if not have:
                continue  # 表还不存在 → create_all 已按完整结构建好
            for name, ddl in cols:
                if name not in have:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
                    logger.info("[ls][migrate] %s 补列: %s %s", table, name, ddl)
        except Exception as e:
            logger.warning("[ls][migrate] %s 补列失败: %s", table, e)


async def get_db_session() -> AsyncSession:
    """拿一个 session（调用方负责 await close）"""
    return async_session()


async def dispose_engine():
    """进程退出时释放连接池"""
    try:
        await engine.dispose()
    except Exception:
        pass
