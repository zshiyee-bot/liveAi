###############################################################################
#  LiveStream 路由（aiohttp 版）—— 全部挂在 /ls 前缀下
#
#  与 LiveTalking 合并后不再单独监听 8020：
#    · 前端构建产物放 web/ls/（hash 路由，无需服务端 fallback）
#    · 实时通道由 FastAPI 的 /ws 改为 aiohttp WebSocket /ls/ws
#    · 话术播报不再走 HTTP 往返，直接经 adapter 操作 avatar_session
#
#  路由注册顺序要求（aiohttp 按注册顺序匹配）：
#    /ls/api/* 必须先注册，最后才是 /ls/{tail:.*} 静态兜底
###############################################################################

import asyncio
import json
import os
import re
import uuid

from aiohttp import web
from sqlalchemy import select

from utils.logger import logger
from server.livestream.config import load_settings
from server.livestream.db import init_db, async_session, dispose_engine
from server.livestream.models import Persona, Script, AppSettings, KnowledgeDocument
from server.livestream.adapter import adapter
from server.livestream.engine import runtime
from server.livestream.services.play_queue import PlayQueue
from server.livestream.services.script_manager import ScriptManager
from server.livestream.services.llm_service import LLMService
from server.livestream.services.danmaku.manager import MultiPlatformCollector

UPLOAD_DIR = os.path.join('data', 'livestream', 'uploads')
WEB_LS_DIR = os.path.join('web', 'ls')

_AUDIO_EXT = ('.mp3', '.wav', '.m4a', '.aac', '.ogg', '.flac')
_VIDEO_EXT = ('.mp4', '.mov', '.mkv', '.webm', '.avi')


# ── 响应工具 ─────────────────────────────────────────────────────
def json_ok(data=None, **extra):
    body = {"code": 0, "msg": "ok"}
    if data is not None:
        body["data"] = data
    body.update(extra)
    return web.Response(content_type="application/json",
                        text=json.dumps(body, ensure_ascii=False))


def json_error(msg, code=-1):
    return web.Response(content_type="application/json",
                        text=json.dumps({"code": code, "msg": str(msg)}, ensure_ascii=False))


async def _body(request) -> dict:
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# ── 响应契约（必须与上游 Vue 前端逐字对齐）───────────────────────
# 上游前端是「混合风格」，不是统一的信封：
#   persona / scripts / knowledge / livestream-status / queue → **裸对象或裸数组**
#     （api/persona.ts:9 `return data`、api/scripts.ts:6 `return data` …）
#   settings → 信封 {code,msg,data}     （api/settings.ts `return data.data`）
#   livetalking/sessions → {code, sessions:[...]}（不带 data 层）
# 错误统一 {detail: "..."} + 4xx —— 前端 axios 拦截器读的是 error.response.data.detail
def reply(obj=None, **extra):
    body = {} if obj is None else obj
    if extra:
        if isinstance(body, dict):
            body = {**body, **extra}
    return web.Response(content_type="application/json",
                        text=json.dumps(body, ensure_ascii=False))


def reply_envelope(data=None, **extra):
    body = {"code": 0, "msg": "ok"}
    if data is not None:
        body["data"] = data
    body.update(extra)
    return web.Response(content_type="application/json",
                        text=json.dumps(body, ensure_ascii=False))


def fail(msg, status=400):
    return web.Response(status=status, content_type="application/json",
                        text=json.dumps({"detail": str(msg)}, ensure_ascii=False))


def _safe_name(name: str, maxlen: int = 60) -> str:
    name = os.path.basename(name or "")
    # ① 有的客户端（含 aiohttp 自己的 multipart 编码）会把非 ASCII 文件名 percent 编码
    #    （如 %E6%B5%8B%E8%AF%95.mp3）→ 先解回来
    try:
        if '%' in name:
            from urllib.parse import unquote
            name = unquote(name)
    except Exception:
        pass
    # ② 还有的把 UTF-8 文件名按 latin-1 还原成乱码 → 再逆转一次。
    # 不做这两步的话，中文文件名会被下面的白名单规则整段替换成下划线。
    try:
        if any(ord(c) > 127 for c in name):
            fixed = name.encode('latin-1').decode('utf-8')
            if fixed:
                name = fixed
    except Exception:
        pass
    stem, ext = os.path.splitext(name)
    stem = re.sub(r'[^\w\u4e00-\u9fff\-\.]', '_', stem)[:maxlen]
    return (stem or "file") + ext.lower()


# ── 运行态 ───────────────────────────────────────────────────────
class _LS:
    def __init__(self):
        self.ready = False
        self.lock = asyncio.Lock()
        self.settings = None
        self.llm = None
        self.queue = None
        self.collector = None
        self.ws_clients = set()
        self.init_error = ""


LS = _LS()


async def _broadcast(payload: dict):
    """把事件推给所有 /ls/ws 客户端（前端用来实时刷新队列/弹幕/状态）。"""
    if not LS.ws_clients:
        return
    text = json.dumps(payload, ensure_ascii=False)
    dead = []
    for ws in list(LS.ws_clients):
        try:
            await ws.send_str(text)
        except Exception:
            dead.append(ws)
    for ws in dead:
        LS.ws_clients.discard(ws)


async def _broadcast_queue():
    """队列一变就推给前端。前端 stores/queue.ts 没有轮询兜底，队列显示全靠这个事件。"""
    if not LS.ws_clients or LS.queue is None:
        return
    try:
        snap = await LS.queue.snapshot()
    except Exception:
        return
    await _broadcast({"type": "queue_update", "data": snap})


def _persona_norm(p) -> dict:
    d = p.to_dict() if p is not None else dict(Persona.default_dict())
    ft = d.get('forbidden_topics')
    if isinstance(ft, str):
        try:
            d['forbidden_topics'] = json.loads(ft)
        except Exception:
            d['forbidden_topics'] = []
    d.setdefault('id', 0)
    return d


async def get_persona() -> dict:
    async with async_session() as s:
        p = (await s.execute(select(Persona).limit(1))).scalar_one_or_none()
        if p is None:
            p = Persona(**Persona.default_dict())
            s.add(p)
            await s.commit()
            await s.refresh(p)
        return _persona_norm(p)


async def get_settings_dict() -> dict:
    async with async_session() as s:
        st = (await s.execute(select(AppSettings).limit(1))).scalar_one_or_none()
        if st is None:
            st = AppSettings(**AppSettings.defaults())
            s.add(st)
            await s.commit()
            await s.refresh(st)
        return st.to_dict()


async def rebuild_deps(reason: str = "startup"):
    """(重)构造 LLM/知识库/队列/采集器。系统配置改了之后必须走这里才生效。"""
    persona = await get_persona()
    cfg = await get_settings_dict()
    llm = LLMService(
        persona=persona,
        llm_api_key=cfg.get('llm_api_key', ''),
        llm_base_url=cfg.get('llm_base_url', ''),
        llm_model=cfg.get('llm_model', ''),
        embedding_api_key=cfg.get('embedding_api_key', ''),
        embedding_base_url=cfg.get('embedding_base_url', ''),
        embedding_model=cfg.get('embedding_model', ''),
    )
    await llm.init_knowledge_base()
    queue = PlayQueue(script_manager=ScriptManager(async_session))
    queue.set_on_change(_broadcast_queue)
    LS.llm = llm
    LS.queue = queue
    LS.collector = MultiPlatformCollector()
    runtime.llm = llm
    runtime.queue = queue
    runtime.collector = LS.collector
    logger.info(f"[ls] 依赖已重建（{reason}）: model={cfg.get('llm_model')} "
                f"KB={'on' if llm._knowledge_base else 'off'}")
    return {"llm_model": cfg.get('llm_model', ''), "knowledge_base": bool(llm._knowledge_base)}


async def ensure_ready():
    """惰性初始化（首次请求或 app.on_startup 触发）。"""
    if LS.ready:
        return
    async with LS.lock:
        if LS.ready:
            return
        LS.settings = load_settings()
        try:
            await init_db()
            await rebuild_deps("startup")
            LS.ready = True
            logger.info("[ls] LiveStream 模块已就绪（进程内合并版）")
        except Exception as e:
            LS.init_error = str(e)
            logger.exception(f"[ls] 初始化失败: {e}")
            raise


# ── 健康检查 ─────────────────────────────────────────────────────
async def api_health(request):
    try:
        await ensure_ready()
    except Exception as e:
        return json_error(f"init failed: {e}")
    return json_ok({"status": "ok", "version": "0.1.0", "in_process": True,
                    "running": runtime.running,
                    "knowledge_base": bool(LS.llm and LS.llm._knowledge_base)})


# ── 人设 ─────────────────────────────────────────────────────────
async def api_persona_get(request):
    await ensure_ready()
    return reply(await get_persona())


async def api_persona_put(request):
    await ensure_ready()
    body = await _body(request)
    async with async_session() as s:
        p = (await s.execute(select(Persona).limit(1))).scalar_one_or_none()
        if p is None:
            p = Persona(**Persona.default_dict())
            s.add(p)
        for k in ('name', 'personality', 'style', 'knowledge_scope'):
            if body.get(k) is not None:
                setattr(p, k, str(body[k]))
        if body.get('forbidden_topics') is not None:
            ft = body['forbidden_topics']
            if isinstance(ft, str):
                ft = [x.strip() for x in ft.replace('，', ',').split(',') if x.strip()]
            p.forbidden_topics = json.dumps(ft, ensure_ascii=False)
        if body.get('danmaku_policy') is not None:
            p.danmaku_policy = str(body['danmaku_policy'])
        for k, lo, hi in (('danmaku_batch_trigger', 1, 50), ('danmaku_max_chars', 10, 200)):
            if body.get(k) is not None:
                try:
                    setattr(p, k, max(lo, min(hi, int(body[k]))))
                except Exception:
                    pass
        if body.get('danmaku_batch_wait') is not None:
            try:
                p.danmaku_batch_wait = max(0.5, min(30.0, float(body['danmaku_batch_wait'])))
            except Exception:
                pass
        await s.commit()
        await s.refresh(p)
        d = _persona_norm(p)
    # 人设改了要立刻反映到 LLM（否则要等重启）
    if LS.llm is not None:
        LS.llm.persona = d
    if runtime.running:
        runtime._policy = d.get('danmaku_policy') or ""
        runtime._batch_trigger = max(1, int(d.get('danmaku_batch_trigger') or 3))
        runtime._batch_wait = float(d.get('danmaku_batch_wait') or 6.0)
        runtime._max_chars = int(d.get('danmaku_max_chars') or 60)
    return reply(d)


# ── 系统配置 ─────────────────────────────────────────────────────
async def api_settings_get(request):
    await ensure_ready()
    return reply_envelope(await get_settings_dict())


async def api_settings_put(request):
    await ensure_ready()
    body = await _body(request)
    async with async_session() as s:
        st = (await s.execute(select(AppSettings).limit(1))).scalar_one_or_none()
        if st is None:
            st = AppSettings(**AppSettings.defaults())
            s.add(st)
        for k in AppSettings.defaults().keys():
            if body.get(k) is not None:
                setattr(st, k, str(body[k]))
        await s.commit()
        await s.refresh(st)
        d = st.to_dict()
    return reply_envelope(d)


async def api_settings_reload(request):
    await ensure_ready()
    info = await rebuild_deps("settings/reload")
    return reply_envelope(info)


# ── 语音合成（豆包 TTS）───────────────────────────────────────────
# 与素材页「本链音色」共用**同一份存储** data/tts_config.json（唯一真源），
# 不写 LiveStream 的 DB。原因：start.bat 在服务启动前就要读它决定用 doubao 还是
# 回退 edgetts，而启动脚本读不到 SQLite；两边写同一文件就不会出现"两处不一致"。
async def api_tts_config_get(request):
    await ensure_ready()
    from server import tts_routes as tr
    key = tr._doubao_api_key()
    cfg = tr.read_tts_config()
    opt = request.app.get("opt")
    return reply_envelope({
        "has_key": bool(key),
        "key_masked": tr._mask(key),
        "resource_id": cfg.get("doubao_resource_id") or "seed-tts-2.0",
        "engine": getattr(opt, "tts", "") if opt else "",
        "ref_file": getattr(opt, "REF_FILE", "") if opt else "",
    })


async def api_tts_config_put(request):
    await ensure_ready()
    body = await _body(request)
    from server import tts_routes as tr
    patch = {}
    if body.get("doubao_api_key") is not None:
        patch["doubao_api_key"] = str(body.get("doubao_api_key") or "").strip()
    if body.get("doubao_resource_id") is not None:
        rid = str(body.get("doubao_resource_id") or "").strip()
        if rid not in tr.RESOURCE_IDS:
            return fail("resource_id 只能是 " + " 或 ".join(tr.RESOURCE_IDS))
        patch["doubao_resource_id"] = rid
    if not patch:
        return fail("没有要保存的字段")
    cur = tr._write_tts_config(patch)          # 同一份存储 + 同步 tts_key_ok.flag
    key = str(cur.get("doubao_api_key") or tr._doubao_api_key() or "").strip()
    logger.info("[ls] 豆包 TTS 配置已保存：resource_id=%s, key=%s",
                cur.get("doubao_resource_id"), tr._mask(key))
    opt = request.app.get("opt")
    return reply_envelope({
        "has_key": bool(key),
        "key_masked": tr._mask(key),
        "resource_id": cur.get("doubao_resource_id") or "seed-tts-2.0",
        "engine": getattr(opt, "tts", "") if opt else "",
        "ref_file": getattr(opt, "REF_FILE", "") if opt else "",
        "note": "已立即生效（无需重启）；双击 start.bat 会自动用豆包复刻音色",
    })


# ── 话术 ─────────────────────────────────────────────────────────
async def api_scripts_list(request):
    await ensure_ready()
    async with async_session() as s:
        rows = (await s.execute(select(Script).order_by(Script.id))).scalars().all()
        return reply([r.to_dict() for r in rows])


async def api_scripts_create(request):
    await ensure_ready()
    body = await _body(request)
    title = (body.get('title') or '').strip()
    stype = (body.get('type') or 'text').strip()
    if not title:
        return fail("title 不能为空")
    if stype not in ('text', 'audio', 'video'):
        return fail(f"不支持的 type: {stype}")
    async with async_session() as s:
        r = Script(title=title[:200], type=stype, content=body.get('content') or '')
        tags = body.get('tags')
        if isinstance(tags, list):
            r.tags = json.dumps(tags, ensure_ascii=False)
        s.add(r)
        await s.commit()
        await s.refresh(r)
        return reply(r.to_dict())


async def api_scripts_update(request):
    await ensure_ready()
    sid = request.match_info['sid']
    body = await _body(request)
    async with async_session() as s:
        r = await s.get(Script, int(sid))
        if r is None:
            return fail("话术不存在", 404)
        if body.get('title') is not None:
            r.title = str(body['title'])[:200]
        if body.get('content') is not None:
            r.content = str(body['content'])
        if body.get('enabled') is not None:
            r.enabled = bool(body['enabled'])
        if isinstance(body.get('tags'), list):
            r.tags = json.dumps(body['tags'], ensure_ascii=False)
        await s.commit()
        await s.refresh(r)
        return reply(r.to_dict())


async def api_scripts_delete(request):
    await ensure_ready()
    sid = request.match_info['sid']
    async with async_session() as s:
        r = await s.get(Script, int(sid))
        if r is None:
            return fail("话术不存在", 404)
        await s.delete(r)
        await s.commit()
    return reply({"code": 0, "msg": "ok"})


async def api_scripts_toggle(request):
    await ensure_ready()
    sid = request.match_info['sid']
    async with async_session() as s:
        r = await s.get(Script, int(sid))
        if r is None:
            return fail("话术不存在", 404)
        r.enabled = not bool(r.enabled)
        await s.commit()
        await s.refresh(r)
        return reply(r.to_dict())


async def api_scripts_upload(request):
    """上传音频/视频话术文件。multipart：file（必填）+ script_id（可选）+ title/type（可选）"""
    await ensure_ready()
    reader = await request.multipart()
    fields, saved = {}, None
    while True:
        part = await reader.next()
        if part is None:
            break
        if part.name == 'file':
            fn = part.filename or 'upload.bin'
            os.makedirs(UPLOAD_DIR, exist_ok=True)
            path = os.path.join(UPLOAD_DIR, f"{uuid.uuid4().hex[:8]}_{_safe_name(fn)}")
            size = 0
            with open(path, 'wb') as f:
                while True:
                    chunk = await part.read_chunk()
                    if not chunk:
                        break
                    size += len(chunk)
                    f.write(chunk)
            saved = (path, fn, size)
        else:
            try:
                fields[part.name] = await part.text()
            except Exception:
                pass
    if saved is None:
        return fail("缺少 file 字段")
    path, fn, size = saved
    ext = os.path.splitext(fn)[1].lower()
    stype = fields.get('type') or ('audio' if ext in _AUDIO_EXT else
                                   'video' if ext in _VIDEO_EXT else 'text')
    async with async_session() as s:
        r = None
        if fields.get('script_id'):
            try:
                r = await s.get(Script, int(fields['script_id']))
            except Exception:
                r = None
        if r is None:
            title = (fields.get('title') or os.path.splitext(fn)[0])[:200]
            r = Script(title=title or fn, type=stype, content='')
            s.add(r)
        r.type = stype
        r.file_path = os.path.abspath(path)
        await s.commit()
        await s.refresh(r)
        d = r.to_dict()
    logger.info(f"[ls] 话术文件已上传: {fn} ({size} B) -> {path} type={stype}")
    return reply(d)


async def api_scripts_rm_file(request):
    """删除话术文件（附件），并把该话术 type 降级为 text。"""
    await ensure_ready()
    sid = request.match_info['sid']
    async with async_session() as s:
        r = await s.get(Script, int(sid))
        if r is None:
            return fail("话术不存在", 404)
        p = r.file_path
        r.file_path = None
        r.type = "text"
        await s.commit()
        await s.refresh(r)
        d = r.to_dict()
    if p and os.path.isfile(p):
        try:
            os.remove(p)
        except Exception as e:
            logger.warning(f"[ls] 删除附件失败 {p}: {e}")
    return reply(d)


# ── 知识库 ───────────────────────────────────────────────────────
async def api_kb_list(request):
    await ensure_ready()
    async with async_session() as s:
        rows = (await s.execute(select(KnowledgeDocument).order_by(KnowledgeDocument.id))).scalars().all()
        return reply([r.to_dict() for r in rows])


async def api_kb_create(request):
    await ensure_ready()
    body = await _body(request)
    title = (body.get('title') or '').strip()
    content = body.get('content') or ''
    if not title or not content.strip():
        return fail("title / content 不能为空")
    async with async_session() as s:
        r = KnowledgeDocument(title=title[:300], content=content,
                              source_type='text', chunk_count=0)
        s.add(r)
        await s.commit()
        await s.refresh(r)
        d = r.to_dict()
        rid = r.id
    _dump_kb_file(rid, title, content)
    await _kb_rebuild_bg()
    d['chunk_count'] = _count_chunks(content)
    return reply(d)


async def api_kb_upload(request):
    """上传 .txt/.md 文档进知识库。"""
    await ensure_ready()
    reader = await request.multipart()
    fields, saved = {}, None
    while True:
        part = await reader.next()
        if part is None:
            break
        if part.name == 'file':
            fn = part.filename or 'doc.txt'
            raw = await part.read()
            saved = (fn, raw)
        else:
            try:
                fields[part.name] = await part.text()
            except Exception:
                pass
    if saved is None:
        return fail("缺少 file 字段")
    fn, raw = saved
    try:
        content = raw.decode('utf-8')
    except UnicodeDecodeError:
        try:
            content = raw.decode('gbk')
        except Exception:
            return fail("只支持 UTF-8 / GBK 文本文件（pdf 请先转文本）")
    title = (fields.get('title') or os.path.splitext(fn)[0])[:300]
    async with async_session() as s:
        r = KnowledgeDocument(title=title, content=content, source_type='file',
                              file_path=fn, chunk_count=_count_chunks(content))
        s.add(r)
        await s.commit()
        await s.refresh(r)
        d = r.to_dict()
        rid = r.id
    _dump_kb_file(rid, title, content)
    await _kb_rebuild_bg()
    return reply(d)


async def api_kb_delete(request):
    await ensure_ready()
    did = request.match_info['did']
    async with async_session() as s:
        r = await s.get(KnowledgeDocument, int(did))
        if r is None:
            return fail("文档不存在", 404)
        await s.delete(r)
        await s.commit()
    _remove_kb_file(int(did))
    await _kb_rebuild_bg()
    return reply({"code": 0, "msg": "ok"})


async def api_kb_rebuild(request):
    await ensure_ready()
    if LS.llm is None or LS.llm._knowledge_base is None:
        # 通常是没配 embedding key / 没装 chromadb
        await LS.llm.init_knowledge_base() if LS.llm else None
    if LS.llm is None or LS.llm._knowledge_base is None:
        return fail("知识库不可用：请在「系统配置」填好 embedding 的 API Key / Base URL / 模型")
    try:
        await LS.llm.rebuild_knowledge_base()
    except Exception as e:
        logger.exception(f"[ls] 知识库重建失败: {e}")
        return fail(f"重建失败: {e}", 500)
    async with async_session() as s:
        rows = (await s.execute(select(KnowledgeDocument))).scalars().all()
        for r in rows:
            r.chunk_count = _count_chunks(r.content or "")
        await s.commit()
    return reply({"code": 0, "msg": "ok", "rebuilt": True})


def _count_chunks(content: str) -> int:
    """与 knowledge_base.py 的分块口径粗对齐（前端只用来显示）。"""
    content = content or ""
    try:
        from langchain_text_splitters import RecursiveCharacterTextSplitter
        return len(RecursiveCharacterTextSplitter(chunk_size=500, chunk_overlap=50)
                   .split_text(content)) if content else 0
    except Exception:
        return max(1, len(content) // 500) if content else 0


def _kb_path(doc_id: int) -> str:
    cfg = LS.settings or load_settings()
    return os.path.join(cfg.knowledge_docs_path, f"doc_{doc_id}.txt")


def _dump_kb_file(doc_id: int, title: str, content: str):
    try:
        p = _kb_path(doc_id)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'w', encoding='utf-8') as f:
            f.write(f"# {title}\n\n{content}")
    except Exception as e:
        logger.warning(f"[ls] 写知识库文件失败 doc={doc_id}: {e}")


def _remove_kb_file(doc_id: int):
    p = _kb_path(doc_id)
    if os.path.isfile(p):
        try:
            os.remove(p)
        except Exception as e:
            logger.warning(f"[ls] 删除知识库文件失败 {p}: {e}")


async def _kb_rebuild_bg():
    """后台重建索引（不阻塞接口返回；失败只记日志）。"""
    async def _run():
        try:
            if LS.llm and LS.llm._knowledge_base:
                await LS.llm.rebuild_knowledge_base()
                logger.info("[ls] 知识库已自动重建")
            else:
                logger.warning("[ls] 知识库未配置（缺 embedding key）→ 文档只入库、未建索引")
        except Exception as e:
            logger.warning(f"[ls] 知识库自动重建失败: {e}")
    asyncio.create_task(_run())


# ── LiveTalking 会话（进程内查询，不再代理 HTTP）──────────────────
async def api_lt_sessions(request):
    await ensure_ready()
    try:
        sessions = adapter.list_sessions()
    except Exception as e:
        logger.warning(f"[ls] 查询会话失败: {e}")
        sessions = []
    # 前端 api/livestream.ts 直接取 result.sessions（不带 data 层）
    return reply({"code": 0, "sessions": sessions})


# ── 直播控制 ─────────────────────────────────────────────────────
async def api_livestream_start(request):
    await ensure_ready()
    body = await _body(request)
    session_id = (body.get('session_id') or '').strip()
    room_id = (body.get('room_id') or '').strip()
    platform = (body.get('platform') or 'bilibili').strip()
    if not session_id:
        return fail("session_id 必填（请先在 index.html 建立会话并点刷新）")
    cfg = await get_settings_dict()
    persona = await get_persona()
    try:
        res = await runtime.start(
            queue=LS.queue, llm=LS.llm, collector=LS.collector, adapter=adapter,
            session_id=session_id, room_id=room_id, platform=platform,
            persona=persona,
            min_size=int(cfg.get('queue_min_size') or 2) if str(cfg.get('queue_min_size', '')).isdigit() else 2,
            interval=(LS.settings.queue_auto_fill_interval if LS.settings else 3.0),
        )
    except Exception as e:
        logger.exception(f"[ls] 启动直播失败: {e}")
        return fail(f"启动失败: {e}", 500)
    return reply({"code": 0, "msg": "ok"})


async def api_livestream_stop(request):
    await ensure_ready()
    try:
        await runtime.stop()
    except Exception as e:
        logger.exception(f"[ls] 停止直播失败: {e}")
        return fail(str(e), 500)
    return reply({"code": 0, "msg": "ok"})


async def api_livestream_interrupt(request):
    await ensure_ready()
    try:
        await runtime.interrupt()
    except Exception as e:
        return fail(str(e), 500)
    return reply({"code": 0, "msg": "ok"})


async def api_livestream_status(request):
    await ensure_ready()
    # 前端 api/livestream.ts 直接当 LivestreamStatus 用（裸对象）
    return reply(await runtime.status())


async def api_queue(request):
    await ensure_ready()
    # 前端 stores/queue.ts 期望 {high, low}
    return reply(await runtime.snapshot())


async def api_mock_danmaku(request):
    await ensure_ready()
    body = await _body(request)
    kind = (body.get('msg_type') or body.get('type') or 'danmaku').strip()
    sender = (body.get('sender') or '测试观众').strip()
    # 兼容前端可能用的各种字段名。实测：网页上点「模拟弹幕」会在两台机器上都返回
    # 400 Bad Request（/ls/api/mock/danmaku），就是这里 content 取不到值导致的。
    # 模拟弹幕本来就是测试功能，没给内容时给个默认值即可，不该报错拦住用户。
    content = (body.get('content') or body.get('text') or body.get('message')
               or body.get('msg') or '').strip()
    if not content:
        # 默认文案必须是一句「值得回应」的话 —— 之前是「这是一条模拟弹幕」，
        # LLM 按人设判定为无意义测试内容 → 回 SKIP → 用户看到「发弹幕没反应」。
        content = '主播你好，今天播到几点呀？'
    await runtime.mock_danmaku(kind, sender, content)
    return reply({"code": 0, "msg": "ok"})


# ── WebSocket（替代 FastAPI 的 /ws）──────────────────────────────
async def ls_ws(request):
    await ensure_ready()
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    LS.ws_clients.add(ws)
    logger.info(f"[ls] ws 客户端接入（共 {len(LS.ws_clients)}）")
    try:
        await ws.send_str(json.dumps(await runtime.status(), ensure_ascii=False))
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT and msg.data == 'ping':
                await ws.send_str('pong')
    except Exception:
        pass
    finally:
        LS.ws_clients.discard(ws)
        logger.info(f"[ls] ws 客户端断开（剩 {len(LS.ws_clients)}）")
    return ws


# ── 静态前端（web/ls/，hash 路由）────────────────────────────────
async def ls_static(request):
    tail = request.match_info.get('tail', '') or 'index.html'
    base = os.path.abspath(WEB_LS_DIR)
    p = os.path.abspath(os.path.join(base, tail))
    if not p.startswith(base):
        raise web.HTTPNotFound()
    if os.path.isdir(p):
        p = os.path.join(p, 'index.html')
    if not os.path.isfile(p):
        p = os.path.join(base, 'index.html')      # hash 路由：未知路径回退首页
    if not os.path.isfile(p):
        return web.Response(status=404, text="LiveStream 前端未构建（缺少 web/ls/index.html）")
    return web.FileResponse(p)


# ── 生命周期 ─────────────────────────────────────────────────────
async def ls_startup(app):
    try:
        await ensure_ready()
    except Exception as e:
        logger.warning(f"[ls] 启动初始化未完成（首个请求会重试）: {e}")


async def ls_cleanup(app):
    try:
        if runtime.running:
            await runtime.stop()
    except Exception:
        pass
    try:
        await dispose_engine()
    except Exception:
        pass


async def api_ls_status_page(request):
    """诊断页：一眼看出合并版是否就绪（浏览器直接打开 /ls/api/diag）。"""
    info = {
        "ready": LS.ready,
        "init_error": LS.init_error,
        "db": (LS.settings.database_url if LS.settings else ""),
        "llm_model": (LS.llm.llm_model if LS.llm else ""),
        "llm_ready": bool(LS.llm and LS.llm.client),
        "knowledge_base": bool(LS.llm and LS.llm._knowledge_base),
        "queue": {"high": LS.queue.high_length if LS.queue else 0,
                  "low": LS.queue.low_length if LS.queue else 0},
        "queue_on_change_wired": bool(LS.queue is not None
                                      and getattr(LS.queue, '_on_change', None) is not None),
        "sessions": len(adapter.list_sessions()),
        "running": runtime.running,
        "inflight": len(runtime._inflight),
        "ws_clients": len(LS.ws_clients),
        "frontend_built": os.path.isfile(os.path.join(WEB_LS_DIR, 'index.html')),
        "url_prefix": "/ls",
    }
    return json_ok(info)


# ── 注册 ─────────────────────────────────────────────────────────
def setup_livestream_routes(app):
    p = "/ls"

    app.router.add_get(f"{p}/api/health", api_health)
    app.router.add_get(f"{p}/api/diag", api_ls_status_page)

    app.router.add_get(f"{p}/api/persona", api_persona_get)
    app.router.add_put(f"{p}/api/persona", api_persona_put)

    app.router.add_get(f"{p}/api/settings", api_settings_get)
    app.router.add_put(f"{p}/api/settings", api_settings_put)
    app.router.add_post(f"{p}/api/settings/reload", api_settings_reload)

    # 语音合成（豆包 TTS）：与素材页「本链音色」共用 data/tts_config.json
    app.router.add_get(f"{p}/api/tts/config", api_tts_config_get)
    app.router.add_put(f"{p}/api/tts/config", api_tts_config_put)

    app.router.add_get(f"{p}/api/scripts", api_scripts_list)
    app.router.add_post(f"{p}/api/scripts", api_scripts_create)
    app.router.add_post(f"{p}/api/scripts/upload-file", api_scripts_upload)
    app.router.add_put(f"{p}/api/scripts/{{sid}}", api_scripts_update)
    app.router.add_delete(f"{p}/api/scripts/{{sid}}", api_scripts_delete)
    app.router.add_post(f"{p}/api/scripts/{{sid}}/toggle", api_scripts_toggle)
    app.router.add_delete(f"{p}/api/scripts/{{sid}}/file", api_scripts_rm_file)

    app.router.add_get(f"{p}/api/knowledge/documents", api_kb_list)
    app.router.add_post(f"{p}/api/knowledge/documents", api_kb_create)
    app.router.add_post(f"{p}/api/knowledge/upload", api_kb_upload)
    # 上游前端用的是 /api/knowledge/documents/upload（api/knowledge.ts:18），两个都留
    app.router.add_post(f"{p}/api/knowledge/documents/upload", api_kb_upload)
    app.router.add_post(f"{p}/api/knowledge/rebuild", api_kb_rebuild)
    app.router.add_delete(f"{p}/api/knowledge/documents/{{did}}", api_kb_delete)

    app.router.add_get(f"{p}/api/livetalking/sessions", api_lt_sessions)

    app.router.add_post(f"{p}/api/livestream/start", api_livestream_start)
    app.router.add_post(f"{p}/api/livestream/stop", api_livestream_stop)
    app.router.add_post(f"{p}/api/livestream/interrupt", api_livestream_interrupt)
    app.router.add_get(f"{p}/api/livestream/status", api_livestream_status)

    app.router.add_get(f"{p}/api/queue", api_queue)
    app.router.add_post(f"{p}/api/mock/danmaku", api_mock_danmaku)

    app.router.add_get(f"{p}/ws", ls_ws)

    # 未知的 /ls/api/* 请求：明确返回 JSON 404，而不是被下面的 SPA 兜底伪装成 HTML 200。
    # 背景：静态兜底只注册了 GET，于是「进程比代码旧、新接口没注册」时表现为
    #   GET  -> 200 + index.html（前端拿到 HTML，报解析/undefined 类错误）
    #   PUT  -> 405 Allow=GET,HEAD,OPTIONS（浏览器控制台里最难查的一种症状）
    # 加这一条后，同样的场景会得到 404 + 明确中文提示。
    async def api_not_found(request):
        tail = request.match_info.get('tail', '')
        logger.warning("[ls] 未注册的接口请求：%s %s（若刚更新过代码，请重启服务）",
                       request.method, request.path)
        return fail(f"接口不存在：{request.method} /ls/api/{tail}"
                    f"（若刚更新过代码，说明服务进程还是旧版本，请重启）", status=404)

    # 注意：不能用 add_route('*', ...) —— app.py 的 aiohttp_cors 会对每个资源调
    # add_preflight_handler，遇到 '*' 通配处理器会直接抛
    #   ValueError: <DynamicResource /ls/api/{tail}> already has a '*' handler for all methods
    # 导致服务起不来。所以逐个方法显式注册。
    for _m in ('GET', 'POST', 'PUT', 'DELETE', 'PATCH'):
        app.router.add_route(_m, f"{p}/api/{{tail:.*}}", api_not_found)

    # 静态兜底必须最后注册
    app.router.add_get(p, ls_static)
    app.router.add_get(f"{p}/{{tail:.*}}", ls_static)

    runtime.on_status(_broadcast)

    try:
        app.on_startup.append(ls_startup)
        app.on_cleanup.append(ls_cleanup)
    except Exception:
        pass

    logger.info("[ls] LiveStream 路由已注册（前缀 /ls，共 20 个接口 + 1 个 WebSocket）")
