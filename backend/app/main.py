###############################################################################
#  FastAPI 主入口
###############################################################################

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import load_settings
from app.database import init_db, async_session
from app.utils.logger import logger

settings = load_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时初始化所有服务，关闭时清理资源"""
    logger.info("Starting LiveStream Pro backend...")
    await init_db()
    logger.info("Database initialized")

    # 1. 加载或创建默认 Persona
    from app.models import Persona
    from sqlalchemy import select

    async with async_session() as db:
        result = await db.execute(select(Persona).limit(1))
        persona = result.scalar_one_or_none()
        if persona is None:
            persona = Persona(**Persona.default_dict())
            db.add(persona)
            await db.commit()
            await db.refresh(persona)
            logger.info("Created default persona")
        persona_dict = persona.to_dict()

    # 2. 初始化 LLMService（含知识库）
    from app.services.llm_service import LLMService

    app.state.llm_service = LLMService(persona_dict)
    try:
        await app.state.llm_service.init_knowledge_base()
    except Exception as e:
        logger.warning(f"Knowledge base init skipped: {e}")
    logger.info(f"LLM service initialized: model={settings.llm_model}")

    # 3. 初始化 LiveTalking 客户端（供 session 查询）
    from app.services.livetalking_client import LiveTalkingClient

    app.state.lt_client = LiveTalkingClient(base_url=settings.livetalking_base_url)
    logger.info(f"LiveTalking client initialized: {settings.livetalking_base_url}")

    # 4. 播放队列和弹幕采集器在直播启动时懒初始化
    app.state.play_queue = None
    app.state.collector = None

    yield

    logger.info("Shutting down LiveStream Pro backend...")
    if app.state.collector:
        try:
            await app.state.collector.disconnect()
        except Exception:
            pass


app = FastAPI(
    title="LiveStream Pro",
    description="数字人直播控制系统",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins.split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 设置默认 state（lifespan 启动后会被覆盖）
app.state.llm_service = None
app.state.lt_client = None
app.state.play_queue = None
app.state.collector = None


@app.get("/api/health")
async def health_check():
    return {"status": "ok", "version": "0.1.0"}


# 注册所有路由
from app.api import router
app.include_router(router)
