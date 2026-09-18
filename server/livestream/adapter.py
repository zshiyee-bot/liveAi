###############################################################################
#  LiveStream → LiveTalking 进程内适配器
#
#  原来 LiveStream 是独立进程，靠 HTTP 访问 LiveTalking：
#      POST /human           发送文字（TTS）
#      POST /humanaudio      发送音频
#      POST /drop_queued_talk 撤回尚未播出的预送话术
#      GET  /is_speaking     是否正在说话
#      GET  /sse             播放结束事件流
#  合并进同一个进程后，这些调用全部变成**直接操作 avatar_session**：
#      · 少一次网络往返（话术零接缝对延迟敏感）
#      · 不会再有端口冲突 / 误打到另一个实例上的事故
#      · 播放结束事件改为订阅 avatar_session.msgqueues（/sse 的同一数据源）
###############################################################################

import asyncio
import json
import queue

from utils.logger import logger
from server.session_manager import session_manager


class LocalAvatarAdapter:
    """把 LiveTalking 的会话 API 包装成 LiveStream 原来习惯的那几个方法。"""

    def __init__(self):
        self._session_id = ""
        self._running = False
        self._playback_callbacks = []       # 播完一条 -> 回调
        self._msgqueues = {}                # sessionid -> queue.Queue
        self._tasks = {}                    # sessionid -> asyncio.Task
        self._connected = False

    # ── 会话解析 ────────────────────────────────────────────────
    def set_session(self, session_id: str):
        sid = (session_id or "").strip()
        if sid != self._session_id:
            logger.info(f"[ls] 会话切换: {self._session_id or '(未设)'} -> {sid or '(空)'}")
            self._session_id = sid

    def get_session_id(self) -> str:
        return self._session_id

    def resolve(self):
        """返回 (sessionid, avatar_session)。未指定/已销毁时退化为当前唯一存活会话。"""
        sid = self._session_id
        if sid:
            sess = session_manager.get_session(sid)
            if sess is not None:
                return sid, sess
        # 兜底：单会话部署下直接取第一个存活会话（避免用户忘选 session 就点开始直播）
        for k, v in list(session_manager.sessions.items()):
            if v is not None:
                if sid:
                    logger.warning(f"[ls] 会话 {sid[:8]} 不存在，改用存活会话 {k[:8]}")
                self._session_id = k
                return k, v
        return sid, None

    def list_sessions(self) -> list:
        """等价于 GET /api/admin/sessions，供前端下拉框使用。"""
        out = []
        for sid, sess in list(session_manager.sessions.items()):
            if sess is None:
                continue
            opt = getattr(sess, 'opt', None)
            item = {"sessionid": sid}
            if opt is not None:
                for k in ('model', 'transport', 'avatar_id', 'listenport', 'batch_size'):
                    if hasattr(opt, k):
                        item[k] = getattr(opt, k)
            try:
                item["speaking"] = bool(sess.is_speaking())
            except Exception:
                item["speaking"] = False
            out.append(item)
        return out

    # ── 发送 ────────────────────────────────────────────────────
    async def send_text(self, text: str, utt: str = "", priority: bool = False) -> dict:
        sid, sess = self.resolve()
        if sess is None:
            return {"code": -1, "msg": "no live session"}
        text = text or ""
        if not text.strip():
            logger.warning("[ls] 空文本话术已跳过")
            return {"code": -1, "msg": "empty text"}
        datainfo = {'utt': utt} if utt else {}
        sess.put_msg_txt(text, datainfo, priority=bool(priority))
        logger.info(f"[ls] -> /human utt={utt or '-'} priority={bool(priority)} text={text[:40]!r}")
        return {"code": 0, "sessionid": sid}

    async def send_audio(self, path: str, utt: str = "", priority: bool = False) -> dict:
        sid, sess = self.resolve()
        if sess is None:
            return {"code": -1, "msg": "no live session"}
        if not path or not callable(getattr(sess, 'put_audio_file', None)):
            return {"code": -1, "msg": f"audio not supported or missing file: {path}"}
        try:
            with open(path, 'rb') as f:
                data = f.read()
        except Exception as e:
            logger.error(f"[ls] 读取音频话术失败 {path}: {e}")
            return {"code": -1, "msg": f"read audio failed: {e}"}
        datainfo = {'utt': utt} if utt else {}
        sess.put_audio_file(data, datainfo)
        logger.info(f"[ls] -> /humanaudio utt={utt or '-'} bytes={len(data)}")
        return {"code": 0, "sessionid": sid}

    async def drop_queued_talk(self, utt: str) -> dict:
        """撤回「已预送但还没播到」的话术（正在播的那条不动）。"""
        sid, sess = self.resolve()
        if sess is None or not utt:
            return {"code": -1, "msg": "no session or empty utt"}
        fn = getattr(sess, 'drop_queued_talk', None)
        if not callable(fn):
            return {"code": -1, "msg": "drop_queued_talk not supported"}
        try:
            info = fn(utt)
        except Exception as e:
            logger.warning(f"[ls] 撤回 {utt} 失败: {e}")
            return {"code": -1, "msg": str(e)}
        logger.info(f"[ls] 撤回预送话术 {utt}: {info}")
        return {"code": 0, "data": info}

    async def is_speaking(self) -> bool:
        _sid, sess = self.resolve()
        if sess is None:
            return False
        try:
            return bool(sess.is_speaking())
        except Exception:
            return False

    # ── 播放结束事件（替代 GET /sse）─────────────────────────────
    def on_playback_ended(self, cb):
        self._playback_callbacks.append(cb)

    async def connect(self):
        """订阅当前会话的播放状态事件。"""
        self._running = True
        self._connected = True
        await self._ensure_subscribed()
        return True

    async def _ensure_subscribed(self):
        sid, sess = self.resolve()
        if sess is None or sid in self._msgqueues:
            return
        q = queue.Queue()
        try:
            sess.add_msgqueue(q)
        except Exception as e:
            logger.warning(f"[ls] 订阅会话 {sid[:8]} 事件失败: {e}")
            return
        self._msgqueues[sid] = q
        self._tasks[sid] = asyncio.create_task(self._reader(sid, q, sess))
        logger.info(f"[ls] 已订阅会话 {sid[:8]} 的播放事件")

    async def _reader(self, sid: str, q: queue.Queue, sess):
        """轮询 msgqueue（与 /sse 同一数据源），status=='end' 时派发回调。"""
        while self._running:
            try:
                while True:
                    try:
                        msg = q.get_nowait()
                    except queue.Empty:
                        break
                    status = None
                    try:
                        data = json.loads(msg)
                        status = data.get('status')
                    except Exception:
                        continue
                    if status == 'end':
                        logger.info("[ls] 播放结束（事件）")
                        for cb in list(self._playback_callbacks):
                            try:
                                if asyncio.iscoroutinefunction(cb):
                                    await cb()
                                else:
                                    cb()
                            except Exception as exc:
                                logger.warning(f"[ls] 播放结束回调异常: {exc}")
            except Exception as e:
                logger.warning(f"[ls] 事件读取异常: {e}")
            await asyncio.sleep(0.01)

    async def disconnect(self):
        self._running = False
        self._connected = False
        for sid, t in list(self._tasks.items()):
            t.cancel()
        self._tasks.clear()
        for sid, q in list(self._msgqueues.items()):
            sess = session_manager.get_session(sid)
            try:
                if sess is not None and q in getattr(sess, 'msgqueues', []):
                    sess.msgqueues.remove(q)
            except Exception:
                pass
        self._msgqueues.clear()
        self._playback_callbacks.clear()
        logger.info("[ls] 适配器已断开")


# 全局单例：routes / engine 共用
adapter = LocalAvatarAdapter()
