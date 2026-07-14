###############################################################################
#  应用配置 — pydantic-settings，环境变量 + .env 文件
###############################################################################

import os
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """全局配置，从环境变量/.env 加载"""

    # ── LiveTalking ──
    livetalking_base_url: str = "http://127.0.0.1:8010"

    # ── LLM（OpenAI 兼容接口）──
    llm_api_key: str = ""
    llm_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    llm_model: str = "qwen-plus"

    # ── Embedding（通义千问 text-embedding-v4，与 LLM 共用 DASHSCOPE_API_KEY）──
    embedding_api_key: str = ""
    embedding_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    embedding_model: str = "text-embedding-v4"

    # ── Queue ──
    queue_min_size: int = 2
    queue_auto_fill_interval: float = 3.0

    # ── Memory ──
    memory_window_size: int = 10

    # ── Database ──
    database_url: str = "sqlite+aiosqlite:///data/livestream.db"

    # ── Knowledge Base ──
    knowledge_docs_path: str = "data/knowledge"
    chroma_persist_path: str = "data/chroma_db"

    # ── App ──
    # app_host: str = "0.0.0.0"
    # app_port: int = 8020
    cors_origins: str = "*"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


def load_settings() -> Settings:
    """加载配置，嵌入 API key 时同步到环境变量（LangChain 需要从环境变量读取）"""
    settings = Settings()
    # 同步 LLM key 到环境变量
    if settings.llm_api_key:
        os.environ.setdefault("OPENAI_API_KEY", settings.llm_api_key)
    if settings.embedding_api_key:
        os.environ.setdefault("EMBEDDING_API_KEY", settings.embedding_api_key)
    return settings
