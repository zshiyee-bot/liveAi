###############################################################################
#  Pydantic Schemas — Persona / Script / Knowledge / Events
###############################################################################

from pydantic import BaseModel, Field


# ── Persona ───────────────────────────────────────────────────────

class PersonaUpdate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    personality: str = Field(..., min_length=1)
    style: str = Field(..., min_length=1)
    knowledge_scope: str = Field(..., min_length=1)
    forbidden_topics: list[str] = Field(default_factory=list)
    # 弹幕回复方式（可选：不传则保持原值）
    #   trigger=1 → 逐条回复；≥2 → 攒够 N 条合并成一句
    danmaku_policy: str | None = None
    danmaku_batch_trigger: int | None = Field(default=None, ge=1, le=50)
    danmaku_batch_wait: float | None = Field(default=None, ge=0.5, le=30.0)
    danmaku_max_chars: int | None = Field(default=None, ge=10, le=200)
    # 弹幕安全（确定性执行，见 services/danmaku_filter.py）
    danmaku_block_words: str | None = None
    danmaku_block_mode: str | None = Field(default=None, pattern="^(exact|contains)$")
    danmaku_block_noise: int | None = Field(default=None, ge=0, le=1)
    danmaku_inject_filter: int | None = Field(default=None, ge=0, le=1)
    danmaku_max_len: int | None = Field(default=None, ge=10, le=500)
    danmaku_rate_limit: int | None = Field(default=None, ge=0, le=50)
    danmaku_fallback: str | None = None
    # 弹幕口播方式（称呼 / 复述原文）
    danmaku_call_name: int | None = Field(default=None, ge=0, le=1)
    danmaku_read_msg: int | None = Field(default=None, ge=0, le=1)
    danmaku_name_max: int | None = Field(default=None, ge=1, le=20)
    danmaku_read_msg_max: int | None = Field(default=None, ge=2, le=100)
    danmaku_reply_templates: str | None = None


class PersonaResponse(BaseModel):
    id: int
    name: str
    personality: str
    style: str
    knowledge_scope: str
    forbidden_topics: list[str]
    danmaku_policy: str = ""
    danmaku_batch_trigger: int = 3
    danmaku_batch_wait: float = 3.0
    danmaku_max_chars: int = 60
    danmaku_block_words: str = "1"
    danmaku_block_mode: str = "exact"
    danmaku_block_noise: int = 1
    danmaku_inject_filter: int = 1
    danmaku_max_len: int = 60
    danmaku_rate_limit: int = 3
    danmaku_fallback: str = ""
    danmaku_call_name: int = 0
    danmaku_read_msg: int = 0
    danmaku_name_max: int = 6
    danmaku_read_msg_max: int = 24
    danmaku_reply_templates: str = ""
    updated_at: str | None = None


# ── Script ────────────────────────────────────────────────────────

class ScriptCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    type: str = Field(default="text", pattern="^(text|audio|video)$")
    content: str = Field(default="")
    tags: list[str] = Field(default_factory=list)
    # 分割符：填了（如 "。"）就把 content 切成多句，播放时一句一句送
    split_sep: str = Field(default="", max_length=8)
    # AI 循环话术配置（一般由 /api/scripts/ai_loop 生成，不手填）
    ai_loop: dict | None = None


class ScriptUpdate(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=200)
    content: str | None = None
    tags: list[str] | None = None
    enabled: bool | None = None
    split_sep: str | None = Field(None, max_length=8)
    ai_loop: dict | None = None


class ScriptResponse(BaseModel):
    id: int
    title: str
    type: str
    content: str
    split_sep: str = ""
    ai_loop: dict | None = None
    file_path: str | None = None
    tags: list[str]
    enabled: bool
    play_count: int
    last_used_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


# ── Knowledge ─────────────────────────────────────────────────────

class KnowledgeCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=300)
    content: str = Field(..., min_length=1)
    source_type: str = Field(default="text", pattern="^(text|file)$")


class KnowledgeResponse(BaseModel):
    id: int
    title: str
    content_preview: str
    source_type: str
    file_path: str | None = None
    chunk_count: int
    created_at: str | None = None


# ── Events / Livestream ───────────────────────────────────────────

class DanmakuEvent(BaseModel):
    platform: str
    sender: str
    content: str
    timestamp: float
    msg_type: str = "danmaku"


class LivestreamStartRequest(BaseModel):
    room_id: str = Field(default="")
    session_id: str = Field(..., min_length=1)
    platform: str = Field(default="bilibili")


class LivestreamStatusResponse(BaseModel):
    running: bool = False
    room_id: str = ""
    session_id: str = ""
    queue_high_count: int = 0
    queue_low_count: int = 0
    danmaku_count: int = 0
    is_speaking: bool = False


class LivetalkingSession(BaseModel):
    sessionid: str
    model: str = ""
    speaking: bool = False
    avatar_id: str = ""
    transport: str = ""


class LivetalkingSessionsResponse(BaseModel):
    sessions: list[LivetalkingSession] = Field(default_factory=list)
