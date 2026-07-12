###############################################################################
#  FastAPI 主入口
###############################################################################

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import load_settings
from app.database import init_db
from app.utils.logger import logger

settings = load_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期"""
    logger.info("Starting LiveStream Pro backend...")
    await init_db()
    logger.info("Database initialized")

    app.state.play_queue = None
    app.state.lt_client = None
    app.state.llm_service = None
    app.state.collector = None
    app.state.script_manager = None

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


@app.get("/api/health")
async def health_check():
    return {"status": "ok", "version": "0.1.0"}


# 注册所有路由
from app.api import router
app.include_router(router)
