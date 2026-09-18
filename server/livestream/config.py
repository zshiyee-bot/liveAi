"""LiveStream 合并版基础设施配置（不依赖 pydantic_settings，纯 os.getenv + 默认值）

业务参数（LLM / Embedding 的 key、base_url、model）仍在「系统配置」页修改，存 DB 的 app_settings 表。
合并进 LiveTalking 后不再独立监听端口 —— 统一走 8010 的 `url_prefix`（默认 /ls）。
"""
import os


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name, "")
        return int(v) if v not in ("", None) else default
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name, "")
        return float(v) if v not in ("", None) else default
    except Exception:
        return default


class Settings:
    """基础设施配置（环境变量或默认值；老 LiveStream 的 .env 变量名保持兼容）"""

    def __init__(self):
        # ── Database ──（与老 LiveStream 同名同结构，直接沿用 data/livestream.db）
        self.database_url: str = os.getenv(
            "DATABASE_URL", "sqlite+aiosqlite:///data/livestream.db")

        # ── Queue ──
        self.queue_min_size: int = _env_int("QUEUE_MIN_SIZE", 2)
        self.queue_auto_fill_interval: float = _env_float("QUEUE_AUTO_FILL_INTERVAL", 3.0)

        # ── Memory ──
        self.memory_window_size: int = _env_int("MEMORY_WINDOW_SIZE", 10)

        # ── Knowledge Base ──
        self.knowledge_docs_path: str = os.getenv("KNOWLEDGE_DOCS_PATH", "data/knowledge")
        self.chroma_persist_path: str = os.getenv("CHROMA_PERSIST_PATH", "data/chroma_db")

        # ── 挂载前缀（前端 baseURL 与 all routes 共用）──
        self.url_prefix: str = os.getenv("LS_URL_PREFIX", "/ls")


_settings: Settings | None = None


def load_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
