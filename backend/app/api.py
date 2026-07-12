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
    await db.commit()
    await db.refresh(persona)
    logger.info(f"Persona updated: {persona.name}")
    return PersonaResponse(**persona.to_dict())


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


@router.post("/api/scripts/upload-audio", tags=["scripts"])
async def upload_audio(
    script_id: int = Form(...),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Script).where(Script.id == script_id))
    script = result.scalar_one_or_none()
    if script is None:
        raise HTTPException(status_code=404, detail="Script not found")
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    ext = os.path.splitext(file.filename or "audio.wav")[1]
    filename = f"{uuid.uuid4().hex}{ext}"
    filepath = os.path.join(UPLOAD_DIR, filename)
    content = await file.read()
    with open(filepath, "wb") as f:
        f.write(content)
    script.audio_path = filepath
    script.type = "audio"
    await db.commit()
    await db.refresh(script)
    return {"code": 0, "audio_path": filepath}


# ═══════════════════════════════════════════════════════════════════
#  Knowledge Base
# ═══════════════════════════════════════════════════════════════════

from app.config import load_settings
_settings = load_settings()


def _get_llm_service():
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
    ext = os.path.splitext(file.filename or "doc.txt")[1]
    filename = f"{uuid.uuid4().hex}{ext}"
    filepath = os.path.join(_settings.knowledge_docs_path, filename)
    content = await file.read()
    try:
        text_content = content.decode("utf-8")
    except UnicodeDecodeError:
        text_content = content.decode("latin-1", errors="replace")
    with open(filepath, "wb") as f:
        f.write(content)
    doc = KnowledgeDocument(title=title, content=text_content, source_type="file", file_path=filepath)
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    return {"code": 0, "id": doc.id, "file_path": filepath}


@router.delete("/api/knowledge/documents/{doc_id}", tags=["knowledge"])
async def delete_document(doc_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(KnowledgeDocument).where(KnowledgeDocument.id == doc_id))
    doc = result.scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.file_path and os.path.exists(doc.file_path):
        os.remove(doc.file_path)
    await db.delete(doc)
    await db.commit()
    return {"code": 0, "msg": "ok"}


@router.post("/api/knowledge/rebuild", tags=["knowledge"])
async def rebuild_index():
    llm_service = _get_llm_service()
    if llm_service is None:
        raise HTTPException(status_code=400, detail="LLM service not initialized")
    await llm_service.rebuild_knowledge_base()
    return {"code": 0, "msg": "ok"}


# ═══════════════════════════════════════════════════════════════════
#  LiveTalking Sessions
# ═══════════════════════════════════════════════════════════════════

@router.get("/api/livetalking/sessions", tags=["livetalking"])
async def get_livetalking_sessions():
    """获取 LiveTalking 活跃 session 列表（供前端选择）"""
    from app.main import app
    lt_client = app.state.lt_client
    if lt_client is None:
        # 如果还没初始化，临时创建一个只用于查询
        from app.services.livetalking_client import LiveTalkingClient
        lt_client = LiveTalkingClient(base_url=_settings.livetalking_base_url)
    sessions = await lt_client.fetch_sessions()
    return {"code": 0, "sessions": sessions}


# ═══════════════════════════════════════════════════════════════════
#  Livestream Control
# ═══════════════════════════════════════════════════════════════════

from app.services.livetalking_client import LiveTalkingClient
from app.services.play_queue import PlayQueue
from app.services.script_manager import ScriptManager
from app.services.llm_service import LLMService
from app.services.danmaku.manager import MultiPlatformCollector
from app.services.danmaku.base import DanmakuMessage


async def _get_or_create_services():
    """懒初始化全局服务"""
    from app.main import app
    from app.database import async_session

    if app.state.play_queue is None:
        script_mgr = ScriptManager(async_session)
        queue = PlayQueue(script_manager=script_mgr)
        app.state.play_queue = queue
        app.state.script_manager = script_mgr

    if app.state.lt_client is None:
        app.state.lt_client = LiveTalkingClient(base_url=_settings.livetalking_base_url)

    return app.state.play_queue, app.state.lt_client


@router.post("/api/livestream/start", tags=["livestream"])
async def start_livestream(req: LivestreamStartRequest):
    """启动直播：连接弹幕 + 连接数字人 + 启动队列"""
    from app.main import app
    from app.database import async_session

    queue, lt_client = await _get_or_create_services()

    # 1. 加载人设 → 初始化 LLM
    async with async_session() as db:
        persona = await _get_or_create_persona(db)
        persona_dict = persona.to_dict()

    if app.state.llm_service is None:
        app.state.llm_service = LLMService(persona_dict)
        try:
            await app.state.llm_service.init_knowledge_base()
        except Exception as e:
            logger.warning(f"Knowledge base init skipped: {e}")

    # 2. 连接 LiveTalking
    lt_client.set_session(req.session_id)
    await lt_client.connect()

    # 注册播放结束回调 → 出队播放
    async def on_playback_end():
        item = await queue.get_next()
        if item is None:
            return
        await _broadcast_queue_update(queue)
        if item.type == "audio" and item.content:
            await lt_client.send_audio(item.content)
        else:
            await lt_client.send_text(item.content)
        await _broadcast_status({"type": "playback_started", "item_id": item.id, "source": item.source})

    lt_client.on_playback_ended(on_playback_end)

    # 3. 连接弹幕平台
    app.state.collector = MultiPlatformCollector()

    async def on_danmaku(msg: DanmakuMessage):
        await _broadcast_event(msg)
        app.state._danmaku_count = getattr(app.state, '_danmaku_count', 0) + 1

        llm = app.state.llm_service
        if msg.msg_type == "danmaku" and llm:
            reply = await llm.generate_reply(msg.content, msg.sender)
            if reply:
                from app.services.play_queue import QueueItem
                await queue.put_high(QueueItem(
                    type="text", content=reply, source="danmaku",
                    metadata={"sender": msg.sender, "original": msg.content},
                ))
                await _broadcast_queue_update(queue)
        elif msg.msg_type == "gift":
            from app.services.play_queue import QueueItem
            thanks = f"谢谢{msg.sender}的{msg.content}！"
            await queue.put_high(QueueItem(
                type="text", content=thanks, source="gift",
                metadata={"sender": msg.sender},
            ))
            await _broadcast_queue_update(queue)
        elif msg.msg_type == "follow":
            from app.services.play_queue import QueueItem
            greeting = f"欢迎{msg.sender}关注直播间！"
            await queue.put_high(QueueItem(
                type="text", content=greeting, source="follow",
                metadata={"sender": msg.sender},
            ))
            await _broadcast_queue_update(queue)

    app.state.collector.on_message(on_danmaku)
    await app.state.collector.connect(req.room_id)

    # 4. 启动队列自动补位
    await queue.start_auto_fill(min_size=_settings.queue_min_size, interval=_settings.queue_auto_fill_interval)

    # 5. 记录直播状态
    app.state._livestream_running = True
    app.state._room_id = req.room_id
    app.state._session_id = req.session_id
    app.state._danmaku_count = 0

    await _broadcast_status({"type": "status_change", "running": True, "room_id": req.room_id})
    logger.info(f"Livestream started: room={req.room_id}, session={req.session_id}")
    return {"code": 0, "msg": "ok"}


@router.post("/api/livestream/stop", tags=["livestream"])
async def stop_livestream():
    """停止直播"""
    from app.main import app

    # 停止弹幕
    if app.state.collector:
        try:
            await app.state.collector.disconnect()
        except Exception as e:
            logger.warning(f"Disconnect collector error: {e}")
        app.state.collector = None

    # 停止队列
    if app.state.play_queue:
        await app.state.play_queue.stop_auto_fill()
        await app.state.play_queue.clear()

    # 断开 LiveTalking（保留 session）
    if app.state.lt_client:
        await app.state.lt_client.disconnect()

    app.state._livestream_running = False
    await _broadcast_status({"type": "status_change", "running": False})
    logger.info("Livestream stopped")
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
        # 发送当前状态快照
        from app.main import app
        queue = app.state.play_queue
        if queue:
            await _broadcast_queue_update(queue)
        while True:
            # 保持连接，接收客户端消息（心跳等）
            try:
                data = await asyncio.wait_for(ws.receive_text(), timeout=30)
                if data == "ping":
                    await ws.send_text("pong")
            except asyncio.TimeoutError:
                await ws.send_text("ping")  # 服务端心跳
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning(f"WS error: {e}")
    finally:
        _ws_clients.discard(ws)
        logger.info(f"Frontend WS disconnected (total={len(_ws_clients)})")
