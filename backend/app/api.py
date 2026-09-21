###############################################################################
#  API Routes — 所有 REST + WebSocket 路由
###############################################################################

import os, uuid, json, asyncio
from datetime import datetime

from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.models import Persona, Script, KnowledgeDocument
from app.schemas import (
    PersonaUpdate, PersonaResponse,
    ScriptCreate, ScriptUpdate, ScriptResponse,
    KnowledgeCreate, KnowledgeResponse,
    LivestreamStartRequest, LivestreamStatusResponse,
)
from app.utils.logger import logger

router = APIRouter()
UPLOAD_DIR = "data/uploads"

# ═══════════════════════════════════════════════════════════════════
#  Persona
# ═══════════════════════════════════════════════════════════════════

async def _get_or_create_persona(db: AsyncSession) -> Persona:
    result = await db.execute(select(Persona).limit(1))
    persona = result.scalar_one_or_none()
    if persona is None:
        persona = Persona(**Persona.default_dict())
        db.add(persona)
        await db.commit()
        await db.refresh(persona)
        logger.info("Created default persona")
    return persona


@router.get("/api/persona", response_model=PersonaResponse, tags=["persona"])
async def get_persona(db: AsyncSession = Depends(get_db)):
    persona = await _get_or_create_persona(db)
    return PersonaResponse(**persona.to_dict())


@router.put("/api/persona", response_model=PersonaResponse, tags=["persona"])
async def update_persona(data: PersonaUpdate, db: AsyncSession = Depends(get_db)):
    persona = await _get_or_create_persona(db)
    persona.name = data.name
    persona.personality = data.personality
    persona.style = data.style
    persona.knowledge_scope = data.knowledge_scope
    persona.forbidden_topics_list = data.forbidden_topics
    # 弹幕聚合回复配置（可选字段：未传则保持原值）
    if data.danmaku_policy is not None:
        persona.danmaku_policy = data.danmaku_policy
    if data.danmaku_batch_trigger is not None:
        persona.danmaku_batch_trigger = int(data.danmaku_batch_trigger)
    if data.danmaku_batch_wait is not None:
        persona.danmaku_batch_wait = float(data.danmaku_batch_wait)
    if data.danmaku_max_chars is not None:
        persona.danmaku_max_chars = int(data.danmaku_max_chars)
    await db.commit()
    await db.refresh(persona)
    logger.info(f"Persona updated: {persona.name}")
    return PersonaResponse(**persona.to_dict())


# ═══════════════════════════════════════════════════════════════════
#  Settings
# ═══════════════════════════════════════════════════════════════════

from app.models import AppSettings


async def _get_settings(db: AsyncSession) -> AppSettings:
    result = await db.execute(select(AppSettings).limit(1))
    s = result.scalar_one_or_none()
    if s is None:
        s = AppSettings(**AppSettings.defaults())
        db.add(s)
        await db.commit()
        await db.refresh(s)
    return s


@router.get("/api/settings", tags=["settings"])
async def get_settings(db: AsyncSession = Depends(get_db)):
    s = await _get_settings(db)
    return {"code": 0, "data": s.to_dict()}


@router.put("/api/settings", tags=["settings"])
async def update_settings(data: dict, db: AsyncSession = Depends(get_db)):
    s = await _get_settings(db)
    allowed = set(AppSettings.defaults().keys())
    for k, v in data.items():
        if k in allowed and v is not None:
            setattr(s, k, v)
    await db.commit()
    logger.info("Settings updated")
    return {"code": 0, "data": s.to_dict()}


@router.post("/api/settings/reload", tags=["settings"])
async def reload_settings(db: AsyncSession = Depends(get_db)):
    """用 DB 中的最新配置重建 LLM 和 Embedding"""
    s = await _get_settings(db)
    # 同步到环境变量
    import os
    if s.llm_api_key:
        os.environ["OPENAI_API_KEY"] = s.llm_api_key
    if s.embedding_api_key:
        os.environ["EMBEDDING_API_KEY"] = s.embedding_api_key

    from app.main import app
    from app.services.llm_service import LLMService
    from app.services.livetalking_client import LiveTalkingClient

    # 重建 LLM service（传入 DB 中的最新配置）
    persona = await _get_or_create_persona(db)
    new_llm = LLMService(
        persona.to_dict(),
        llm_api_key=s.llm_api_key,
        llm_base_url=s.llm_base_url,
        llm_model=s.llm_model,
        embedding_api_key=s.embedding_api_key,
        embedding_base_url=s.embedding_base_url,
        embedding_model=s.embedding_model,
    )
    try:
        await new_llm.init_knowledge_base()
    except Exception as e:
        logger.warning(f"Knowledge base re-init skipped: {e}")
    app.state.llm_service = new_llm

    # 重建 LiveTalking client
    app.state.lt_client = LiveTalkingClient(base_url=s.livetalking_base_url)

    logger.info("Settings reloaded")
    return {"code": 0, "msg": "ok"}


# ═══════════════════════════════════════════════════════════════════
#  Scripts
# ═══════════════════════════════════════════════════════════════════

@router.get("/api/scripts", response_model=list[ScriptResponse], tags=["scripts"])
async def list_scripts(
    enabled_only: bool = False,
    type_filter: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Script)
    if enabled_only:
        stmt = stmt.where(Script.enabled == True)
    if type_filter:
        stmt = stmt.where(Script.type == type_filter)
    stmt = stmt.order_by(Script.created_at.desc())
    result = await db.execute(stmt)
    return [ScriptResponse(**s.to_dict()) for s in result.scalars().all()]


@router.post("/api/scripts", response_model=ScriptResponse, tags=["scripts"])
async def create_script(data: ScriptCreate, db: AsyncSession = Depends(get_db)):
    script = Script(title=data.title, type=data.type, content=data.content, tags_list=data.tags)
    db.add(script)
    await db.commit()
    await db.refresh(script)
    logger.info(f"Script created: {script.title}")
    return ScriptResponse(**script.to_dict())


@router.put("/api/scripts/{script_id}", response_model=ScriptResponse, tags=["scripts"])
async def update_script(script_id: int, data: ScriptUpdate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Script).where(Script.id == script_id))
    script = result.scalar_one_or_none()
    if script is None:
        raise HTTPException(status_code=404, detail="Script not found")
    if data.title is not None:
        script.title = data.title
    if data.content is not None:
        script.content = data.content
    if data.tags is not None:
        script.tags_list = data.tags
    if data.enabled is not None:
        script.enabled = data.enabled
    await db.commit()
    await db.refresh(script)
    return ScriptResponse(**script.to_dict())


@router.delete("/api/scripts/{script_id}", tags=["scripts"])
async def delete_script(script_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Script).where(Script.id == script_id))
    script = result.scalar_one_or_none()
    if script is None:
        raise HTTPException(status_code=404, detail="Script not found")
    # 删除关联文件
    if script.file_path and os.path.isfile(script.file_path):
        try:
            os.remove(script.file_path)
        except Exception as e:
            logger.warning(f"Failed to delete script file: {e}")
    await db.delete(script)
    await db.commit()
    return {"code": 0, "msg": "ok"}


@router.post("/api/scripts/{script_id}/toggle", tags=["scripts"])
async def toggle_script(script_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Script).where(Script.id == script_id))
    script = result.scalar_one_or_none()
    if script is None:
        raise HTTPException(status_code=404, detail="Script not found")
    script.enabled = not script.enabled
    await db.commit()
    return {"code": 0, "enabled": script.enabled}


@router.post("/api/scripts/upload-file", tags=["scripts"])
async def upload_file(
    script_id: int = Form(...),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Script).where(Script.id == script_id))
    script = result.scalar_one_or_none()
    if script is None:
        raise HTTPException(status_code=404, detail="Script not found")

    content = await file.read()
    os.makedirs(UPLOAD_DIR, exist_ok=True)

    if script.type == "video":
        # 视频：先存本地，再调 LiveTalking convert_custom_media 转换
        ext = os.path.splitext(file.filename or "video.mp4")[1]
        local_path = os.path.join(UPLOAD_DIR, f"{uuid.uuid4().hex}{ext}")
        with open(local_path, "wb") as f:
            f.write(content)

        from app.services.livetalking_client import LiveTalkingClient
        from app.main import app as main_app
        lt = main_app.state.lt_client or LiveTalkingClient(base_url=_settings.livetalking_base_url)
        result_data = await lt.convert_media(local_path)
        script.file_path = result_data["media_path"]  # 存 media_path，不是本地路径
        # 转换完成后删除本地临时视频文件
        try:
            os.remove(local_path)
        except Exception as e:
            logger.warning(f"Failed to remove temp video: {e}")
    else:
        ext = os.path.splitext(file.filename or "audio.wav")[1]
        local_path = os.path.join(UPLOAD_DIR, f"{uuid.uuid4().hex}{ext}")
        with open(local_path, "wb") as f:
            f.write(content)
        script.file_path = local_path

    await db.commit()
    await db.refresh(script)
    return {"code": 0, "file_path": script.file_path}


# ═══════════════════════════════════════════════════════════════════
#  Knowledge Base
# ═══════════════════════════════════════════════════════════════════

from app.config import load_settings
_settings = load_settings()


def _get_llm_service():
    """获取 LLM service（启动时已初始化，永远非空）"""
    from app.main import app
    return app.state.llm_service


@router.get("/api/knowledge/documents", response_model=list[KnowledgeResponse], tags=["knowledge"])
async def list_documents(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(KnowledgeDocument).order_by(KnowledgeDocument.created_at.desc()))
    docs = result.scalars().all()
    return [
        KnowledgeResponse(
            id=d.id, title=d.title,
            content_preview=d.content[:200] + "..." if len(d.content) > 200 else d.content,
            source_type=d.source_type, file_path=d.file_path,
            chunk_count=d.chunk_count,
            created_at=d.created_at.isoformat() if d.created_at else None,
        ) for d in docs
    ]


@router.post("/api/knowledge/documents", response_model=KnowledgeResponse, tags=["knowledge"])
async def create_document(data: KnowledgeCreate, db: AsyncSession = Depends(get_db)):
    doc = KnowledgeDocument(title=data.title, content=data.content, source_type=data.source_type)
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    # 增量同步到向量库
    llm = _get_llm_service()
    if llm and llm._knowledge_base:
        n = await llm._knowledge_base.add_document(doc.id, doc.title, doc.content)
        doc.chunk_count = n
        await db.commit()

    return KnowledgeResponse(
        id=doc.id, title=doc.title,
        content_preview=doc.content[:200] + "..." if len(doc.content) > 200 else doc.content,
        source_type=doc.source_type, chunk_count=doc.chunk_count,
        created_at=doc.created_at.isoformat() if doc.created_at else None,
    )


@router.post("/api/knowledge/documents/upload", tags=["knowledge"])
async def upload_document(
    title: str = Form(...),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    os.makedirs(_settings.knowledge_docs_path, exist_ok=True)
    ext = os.path.splitext(file.filename or "doc.txt")[1].lower()
    filename = f"{uuid.uuid4().hex}{ext}"
    filepath = os.path.join(_settings.knowledge_docs_path, filename)
    content = await file.read()
    with open(filepath, "wb") as f:
        f.write(content)

    # 根据文件类型解析文本
    if ext == ".pdf":
        try:
            from pypdf import PdfReader
            from io import BytesIO
            reader = PdfReader(BytesIO(content))
            text_content = "\n".join(
                page.extract_text() or "" for page in reader.pages
            )
        except Exception as e:
            logger.warning(f"PDF text extraction failed: {e}")
            text_content = f"[PDF 解析失败: {e}]"
    else:
        try:
            text_content = content.decode("utf-8")
        except UnicodeDecodeError:
            text_content = content.decode("latin-1", errors="replace")

    doc = KnowledgeDocument(title=title, content=text_content, source_type="file", file_path=filepath)
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    # 增量同步到向量库
    llm = _get_llm_service()
    if llm and llm._knowledge_base:
        n = await llm._knowledge_base.add_document(doc.id, doc.title, doc.content)
        doc.chunk_count = n
        await db.commit()

    return {"code": 0, "id": doc.id, "file_path": filepath, "chunk_count": doc.chunk_count}


@router.delete("/api/knowledge/documents/{doc_id}", tags=["knowledge"])
async def delete_document(doc_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(KnowledgeDocument).where(KnowledgeDocument.id == doc_id))
    doc = result.scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.file_path and os.path.exists(doc.file_path):
        os.remove(doc.file_path)

    # 先从向量库删除
    llm = _get_llm_service()
    if llm and llm._knowledge_base:
        await llm._knowledge_base.delete_document(doc_id)

    await db.delete(doc)
    await db.commit()
    return {"code": 0, "msg": "ok"}


@router.post("/api/knowledge/rebuild", tags=["knowledge"])
async def rebuild_index():
    await _get_llm_service().rebuild_knowledge_base()
    return {"code": 0, "msg": "ok"}


# ═══════════════════════════════════════════════════════════════════
#  LiveTalking Sessions
# ═══════════════════════════════════════════════════════════════════

@router.get("/api/livetalking/sessions", tags=["livetalking"])
async def get_livetalking_sessions():
    """获取 LiveTalking 活跃 session 列表（供前端选择）"""
    from app.main import app
    sessions = await app.state.lt_client.fetch_sessions()
    return {"code": 0, "sessions": sessions}


# ═══════════════════════════════════════════════════════════════════
#  Livestream Control
# ═══════════════════════════════════════════════════════════════════

from app.services.play_queue import PlayQueue
from app.services.script_manager import ScriptManager
from app.services.danmaku.manager import MultiPlatformCollector
from app.services.danmaku.base import DanmakuMessage


async def _get_or_create_queue():
    """懒初始化播放队列（直播启动时）"""
    from app.main import app
    from app.database import async_session

    if app.state.play_queue is None:
        script_mgr = ScriptManager(async_session)
        queue = PlayQueue(script_manager=script_mgr)
        app.state.play_queue = queue
        app.state.script_manager = script_mgr

    return app.state.play_queue


def _is_withdrawable(item) -> bool:
    """该项在 LiveTalking 侧是否可撤回。

    只有 /human 的文字项会被服务端用 utt 打标；/humanaudio 与
    /load_custom_media 不收 utt，也就撤不掉。do_send 会写入这个标记。
    """
    return bool(item.metadata.get("withdrawable"))


def _pick_withdrawable(inflight: list):
    """从「已预送」的项里挑出可撤回的那一条（即 inflight[1]），否则返回 None。

    inflight[0] 是正在播的（不动它）；只考虑 inflight[1]。
    音视频项不可撤回时返回 None —— 调用方据此「原位保留」，
    避免出现「撤不干净却照样重发 → 同一条播两遍」。
    """
    if len(inflight) < 2:
        return None
    cand = inflight[1]
    return cand if _is_withdrawable(cand) else None


async def _teardown_livestream(reason: str = ""):
    """彻底结束当前直播会话（start 的重启保护 与 stop 接口共用）。

    顺序有讲究：先把代号 +1 并清掉运行标志（旧循环下一轮自然失效），
    再取消任务（确定性停止，不等循环醒来），最后才断连接 / 停队列 / 清队列。
    """
    from app.main import app

    # ① 这一代作废：旧 watchdog / 聚合器的 _session_alive() 立刻变 False
    app.state._livestream_gen = getattr(app.state, "_livestream_gen", 0) + 1
    app.state._livestream_running = False

    # ② 确定性取消（不依赖循环自己醒来看标志位）
    tasks = getattr(app.state, "_livestream_tasks", None) or []
    for t in tasks:
        t.cancel()
    for t in tasks:
        try:
            await t
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"后台任务退出异常: {e}")
    app.state._livestream_tasks = []

    # ③ 断开弹幕平台
    if getattr(app.state, "collector", None):
        try:
            await app.state.collector.disconnect()
        except Exception as e:
            logger.warning(f"Disconnect collector error: {e}")
        app.state.collector = None

    # ④ 停自动补位 + 清空队列
    if getattr(app.state, "play_queue", None):
        await app.state.play_queue.stop_auto_fill()
        await app.state.play_queue.clear()

    # ⑤ 断开 LiveTalking（保留 session）
    if getattr(app.state, "lt_client", None):
        await app.state.lt_client.disconnect()

    if reason:
        logger.info(f"Livestream torn down ({reason})")


@router.post("/api/livestream/start", tags=["livestream"])
async def start_livestream(req: LivestreamStartRequest):
    """启动直播：连接弹幕 + 连接数字人 + 启动队列"""
    from app.main import app
    from app.database import async_session

    # 幂等启动：已在直播则先彻底停掉旧会话。
    # 否则旧会话的 watchdog / 弹幕聚合器还在跑，两套消费者抢同一个队列 → 双份回复。
    if getattr(app.state, "_livestream_running", False):
        logger.warning("检测到直播已在运行 → 先停止旧会话再启动")
        await _teardown_livestream("restart")

    llm = app.state.llm_service
    lt_client = app.state.lt_client
    queue = await _get_or_create_queue()

    # 每次队列变更自动广播到前端（put_high/put_low/get_next 内部触发）
    queue.set_on_change(lambda: _broadcast_queue_update(queue))

    # 1. 重新加载最新人设（前端可能刚改了 persona）
    async with async_session() as db:
        persona = await _get_or_create_persona(db)
        llm.persona = persona.to_dict()

    # 2. 连接 LiveTalking
    lt_client.set_session(req.session_id)
    await lt_client.connect()

    # ── 播放调度：预送流水线（零接缝）+ 弹幕插队（不打断当前这句）──
    #
    # 为什么必须预送：LiveTalking 的 TTS 是「整段联网合成完成 → 一次性把音频帧灌进播放队列」，
    # 而 edge TTS 每段要 1.0~2.8 秒。若「等这条播完才去要下一条」，中间必然出现 1~3 秒空白。
    # 所以始终保持「1 条在播 + 1 条已预送」：下一条在上一条播完前就已合成好、帧已排队 → 无缝。
    _playback_lock = asyncio.Lock()   # 只保护状态变更（不用来等队列 → 不会丢推进）
    _inflight: list = []              # 已发给 LiveTalking、还没播完的项（[0] = 正在播）
    _PREFETCH = 1                     # 预送窗口
    _IDLE_TICKS = 0                   # 看门狗：连续多少次探测到「没在说话」

    # 会话代号。旧会话的 watchdog / 弹幕聚合器只认自己那一代，
    # 重启时代号 +1，旧任务立刻失效退出（光靠 _livestream_running 挡不住：
    # 重启会把它立刻置回 True，旧任务就"复活"并与新任务抢同一个队列）。
    app.state._livestream_gen = getattr(app.state, "_livestream_gen", 0) + 1
    _gen = app.state._livestream_gen

    def _session_alive() -> bool:
        return (getattr(app.state, "_livestream_running", False)
                and getattr(app.state, "_livestream_gen", 0) == _gen)

    async def do_send(item, priority: bool = False):
        """把一条交给 LiveTalking（用 item.id 当 utt 编号，便于撤回未开播的那条）

        只有走 /human 的文字项能被打标撤回：服务端是拿 utt 去标记 TTS 音频帧的。
        /humanaudio（音频）和 /load_custom_media（视频）都不接受 utt，
        服务端无法定位它们的帧 → 明确记为不可撤回，
        否则「撤回无效却照样重发」会让这一条播两遍。
        """
        utt = item.id
        if item.type == "video" and item.content:
            await lt_client.load_media(item.content)
            item.metadata["withdrawable"] = False
        elif item.type == "audio" and item.content:
            await lt_client.send_audio(item.content)
            item.metadata["withdrawable"] = False
        else:
            await lt_client.send_text(item.content, utt=utt, priority=priority)
            item.metadata["utt"] = utt
            item.metadata["withdrawable"] = True
        _inflight.append(item)
        await _broadcast_status({
            "type": "playback_started",
            "item_id": item.id,
            "source": item.source,
            "priority": priority,
            "inflight": len(_inflight),
            "content_preview": item.content[:80],
        })
        logger.info(f"→ LiveTalking utt={utt} priority={priority} inflight={len(_inflight)} "
                    f"[{item.source}] {item.content[:40]}")

    async def prefill():
        """把预送窗口填满（不阻塞等待：队列空就返回，靠 auto-fill / 弹幕再触发）"""
        async with _playback_lock:
            while len(_inflight) <= _PREFETCH:
                item = await queue.get_next()
                if item is None:
                    break
                try:
                    await do_send(item)
                except Exception as e:
                    logger.error(f"Send to LiveTalking failed: {e}")
                    break

    async def withdraw_pending():
        """撤回「已预送、还没开播」的那条 → 给弹幕回复腾出紧邻位置（正在播的不动）

        仅撤回文字项（服务端有 utt 标记）。音频/视频项撤不掉，就原地保留不动，
        让弹幕回复顺延到它之后 —— 总好过撤不干净还重发一遍、导致重复播放。
        """
        async with _playback_lock:
            if len(_inflight) < 2:
                return None
            cand = _pick_withdrawable(_inflight)
            if cand is None:
                logger.info(f"预送项 [{_inflight[1].source}] type={_inflight[1].type} 不可撤回 → 原位保留")
                return None
            del _inflight[1]
        res = await lt_client.drop_queued_talk(cand.metadata.get("utt", ""))
        logger.info(f"撤回预送 utt={cand.metadata.get('utt')} -> {res}")
        return cand

    async def push_priority(text: str, source: str = "danmaku", metadata: dict = None):
        """插队播报：当前这句一定说完，回复紧跟其后（priority=True 插到 TTS 未合成队列最前）"""
        if not text:
            return
        from app.services.play_queue import QueueItem
        item = QueueItem(type="text", content=text, source=source, metadata=metadata or {})
        pending = await withdraw_pending()      # ① 把已预送的那条抽回来
        await do_send(item, priority=True)      # ② 回复插到最前（紧跟当前这句）
        if pending is not None:                 # ③ 抽回来的那条补在后面
            try:
                await do_send(pending)
            except Exception as e:
                logger.error(f"补发撤回项失败: {e}")

    async def on_playback_end():
        """SSE status=end：这条播完了 → 腾出窗口并立刻补位（保证下一条早已在队列里）"""
        done = None
        async with _playback_lock:
            if _inflight:
                done = _inflight.pop(0)
        if done is not None:
            logger.info(f"播完 [{done.source}] utt={done.metadata.get('utt')}")
        await prefill()

    # SSE 回调（主力）
    lt_client.on_playback_ended(on_playback_end)

    async def _pump_watchdog():
        """兜底：SSE 丢失 / 启动瞬间也能推进（绝不能因为没有 end 事件就停摆）"""
        nonlocal _IDLE_TICKS
        while _session_alive():
            await asyncio.sleep(1.0)
            try:
                async with _playback_lock:
                    n = len(_inflight)
                if n == 0:
                    await prefill()
                    continue
                if await lt_client.is_speaking():
                    _IDLE_TICKS = 0
                    continue
                _IDLE_TICKS += 1
                if n == 1 and _IDLE_TICKS >= 3:
                    logger.warning("watchdog: 连续 3 秒未收到 end 且未在说话 → 手动推进")
                    _IDLE_TICKS = 0
                    await on_playback_end()
            except Exception as e:
                logger.warning(f"pump watchdog error: {e}")

    # 3. 连接弹幕平台
    app.state.collector = MultiPlatformCollector()

    # ── 弹幕聚合：积压 >2 条（或等 3 秒）→ 一次 LLM 调用 → 合并成「一句」话术 ──
    # 多条弹幕不逐条回，而是合成一句话，例如：
    #   A「在哪」B「多少钱」→「刚刚有观众问我们位置和价格，我们是在苏州，价格也不贵，两百块」
    _danmaku_buf: list = []
    _persona = llm.persona or {}
    _BATCH_TRIGGER = int(_persona.get("danmaku_batch_trigger") or 3)   # 积压 >2 条即触发
    _BATCH_WAIT = float(_persona.get("danmaku_batch_wait") or 3.0)     # 不足时兜底等待（秒）
    _MAX_CHARS = int(_persona.get("danmaku_max_chars") or 60)          # 一句话上限字数
    _POLICY = _persona.get("danmaku_policy") or ""                     # 用户自定义回复策略

    async def _danmaku_aggregator():
        while _session_alive():
            await asyncio.sleep(0.2)
            if not _danmaku_buf:
                continue
            waited = 0.0
            while len(_danmaku_buf) < _BATCH_TRIGGER and waited < _BATCH_WAIT:
                await asyncio.sleep(0.15)
                waited += 0.15
            batch = list(_danmaku_buf)
            _danmaku_buf.clear()
            try:
                merged = await llm.generate_merged_reply(batch, policy=_POLICY,
                                                         max_chars=_MAX_CHARS)
            except Exception as e:
                logger.error(f"弹幕聚合失败: {e}")
                merged = ""
            await _broadcast_status({
                "type": "danmaku_batch",
                "count": len(batch),
                "merged": merged,
                "items": [{"kind": b.get("kind"), "sender": b.get("sender"),
                           "content": (b.get("content") or "")[:60]} for b in batch],
            })
            if merged:
                logger.info(f"弹幕合并成一句（{len(batch)} 条）: {merged}")
                await push_priority(merged, source="danmaku", metadata={
                    "batch_count": len(batch), "merged": True,
                    "senders": [b.get("sender") for b in batch],
                })
            else:
                logger.info(f"这 {len(batch)} 条弹幕判定为无需回应（跳过，不占播放队列）")

    async def on_danmaku(msg: DanmakuMessage):
        await _broadcast_event(msg)
        app.state._danmaku_count = getattr(app.state, '_danmaku_count', 0) + 1
        # 不立刻回复：先入缓冲，由 _danmaku_aggregator 攒批后「合并成一句」再插队
        _danmaku_buf.append({
            "kind": msg.msg_type,        # danmaku / gift / follow
            "sender": msg.sender,
            "content": msg.content,
        })
        logger.info(f"弹幕入缓冲（{len(_danmaku_buf)}/{_BATCH_TRIGGER}）"
                    f"[{msg.msg_type}] {msg.sender}: {msg.content[:40]}")

    app.state.collector.on_message(on_danmaku)
    # 保存 handler 引用供 mock 接口使用
    app.state._danmaku_handler = on_danmaku
    await app.state.collector.connect(req.platform, req.room_id)

    # 4. 启动队列自动补位
    await queue.start_auto_fill(min_size=_settings.queue_min_size, interval=_settings.queue_auto_fill_interval)

    # 5. 记录直播状态
    app.state._livestream_running = True
    app.state._room_id = req.room_id
    app.state._session_id = req.session_id
    app.state._danmaku_count = 0

    # 6. 启动调度：先预送第一条（零接缝的起点），再挂兜底看门狗 + 弹幕聚合器
    #    任务句柄存下来，停止/重启时能确定性地取消（而不是等循环自己发现标志位）
    app.state._livestream_tasks = [
        asyncio.create_task(_pump_watchdog()),
        asyncio.create_task(_danmaku_aggregator()),
    ]
    await prefill()

    await _broadcast_status({"type": "status_change", "running": True, "room_id": req.room_id})
    logger.info(f"Livestream started: room={req.room_id}, session={req.session_id}")
    return {"code": 0, "msg": "ok"}


@router.get("/api/queue", tags=["queue"])
async def get_queue():
    """获取当前播放队列快照（HTTP 轮询降级方案）"""
    from app.main import app
    queue = app.state.play_queue
    if queue is None:
        return {"high": [], "low": []}
    return await queue.snapshot()


@router.post("/api/livestream/stop", tags=["livestream"])
async def stop_livestream():
    """停止直播（与 start 的重启保护共用同一套 teardown）"""
    await _teardown_livestream("stop api")
    await _broadcast_status({"type": "status_change", "running": False})
    logger.info("Livestream stopped")
    return {"code": 0, "msg": "ok"}


@router.post("/api/mock/danmaku", tags=["mock"])
async def mock_danmaku(
    content: str = Form(default="主播好厉害！"),
    sender: str = Form(default="测试观众"),
    msg_type: str = Form(default="danmaku"),
):
    """模拟弹幕/礼物/关注事件（仅测试用）"""
    from app.main import app
    handler = getattr(app.state, '_danmaku_handler', None)
    if handler is None:
        raise HTTPException(status_code=400, detail="直播未启动，无法模拟弹幕")

    from app.services.danmaku.base import DanmakuMessage
    msg = DanmakuMessage(
        platform="mock",
        sender=sender,
        content=content,
        msg_type=msg_type,
    )
    await handler(msg)
    logger.info(f"Mock danmaku: [{msg_type}] {sender}: {content}")
    return {"code": 0, "msg": "ok"}


@router.get("/api/livestream/status", response_model=LivestreamStatusResponse, tags=["livestream"])
async def get_livestream_status():
    """获取直播状态"""
    from app.main import app
    queue = app.state.play_queue
    running = getattr(app.state, '_livestream_running', False)
    return LivestreamStatusResponse(
        running=running,
        room_id=getattr(app.state, '_room_id', ''),
        session_id=getattr(app.state, '_session_id', ''),
        queue_high_count=queue.high_length if queue else 0,
        queue_low_count=queue.low_length if queue else 0,
        danmaku_count=getattr(app.state, '_danmaku_count', 0),
        is_speaking=False,
    )


@router.post("/api/livestream/interrupt", tags=["livestream"])
async def interrupt_livestream():
    """打断当前播放"""
    from app.main import app
    if app.state.lt_client:
        await app.state.lt_client.interrupt()
    return {"code": 0, "msg": "ok"}


# ═══════════════════════════════════════════════════════════════════
#  WebSocket（前端实时事件推送）
# ═══════════════════════════════════════════════════════════════════

_ws_clients: set[WebSocket] = set()


async def _broadcast_event(msg: DanmakuMessage):
    """广播弹幕事件给所有前端 WebSocket 客户端"""
    data = {
        "type": msg.msg_type,
        "platform": msg.platform,
        "sender": msg.sender,
        "content": msg.content,
        "timestamp": msg.timestamp,
    }
    await _broadcast(data)


async def _broadcast_queue_update(queue: PlayQueue):
    """广播队列更新"""
    snapshot = await queue.snapshot()
    await _broadcast({"type": "queue_update", "data": snapshot})


async def _broadcast_status(data: dict):
    """广播状态变更"""
    await _broadcast(data)


async def _broadcast(data: dict):
    """向所有已连接的 WebSocket 客户端发送消息"""
    dead: set[WebSocket] = set()
    for ws in _ws_clients:
        try:
            await ws.send_json(data)
        except Exception:
            dead.add(ws)
    _ws_clients.difference_update(dead)


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """前端 WebSocket 连接"""
    await ws.accept()
    _ws_clients.add(ws)
    logger.info(f"Frontend WS connected (total={len(_ws_clients)})")
    try:
        while True:
            # 仅保持连接存活，实际数据通过 _broadcast 发送
            data = await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning(f"WS error: {e}")
    finally:
        _ws_clients.discard(ws)
        logger.info(f"Frontend WS disconnected (total={len(_ws_clients)})")
