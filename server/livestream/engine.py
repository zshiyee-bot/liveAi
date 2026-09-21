###############################################################################
#  LiveStream 播放引擎（进程内版）—— 行为对齐上游 lipku/LiveStream
#
#  上游把直播逻辑写在 FastAPI 的 /api/livestream/start 闭包 + app.state 里
#  （LiveStream/backend/app/api.py:415-521），这里抽成一个类，逻辑保持一致：
#
#    · 弹幕：逐条交给 LLM 生成回复 → put_high 进高优队列（同优先级 FIFO）
#    · 礼物：直接「谢谢X的Y！」→ put_high
#    · 关注：直接「欢迎X关注直播间！」→ put_high
#    · 播放：播完一条 → 取队列下一条（on_playback_end 事件驱动 + 出队锁）
#    · 补位：PlayQueue.start_auto_fill（低优队列不足 min_size 时补一条话术）
#
#  与上游唯一的差别：原来 LiveStream 是独立进程、通过 HTTP 访问 LiveTalking
#  （/human、/humanaudio、/sse …）；现在合并进同一进程，改由
#  LocalAvatarAdapter 直接操作 avatar_session，其余保持不变。
###############################################################################

import asyncio

from utils.logger import logger


class LiveStreamRuntime:
    def __init__(self):
        self.queue = None                    # PlayQueue
        self.llm = None                      # LLMService
        self.collector = None                # MultiPlatformCollector
        self.adapter = None                  # LocalAvatarAdapter（make_adapter() 注入）

        self.running = False
        self.room_id = ""
        self.session_id = ""
        self.platform = ""
        self.danmaku_count = 0

        self._playback_lock = asyncio.Lock()  # 防止重复出队（上游同名逻辑）
        self._play_task = None
        self._status_cbs: list = []

    # ── 状态广播（routes 注入：推给 /ls/ws 的客户端）─────────────
    def on_status(self, cb):
        self._status_cbs.append(cb)

    async def _emit(self, payload: dict):
        for cb in list(self._status_cbs):
            try:
                r = cb(payload)
                if asyncio.iscoroutine(r):
                    await r
            except Exception as e:
                logger.warning(f"[ls] 状态广播失败: {e}")

    # ── 对外状态 ────────────────────────────────────────────────
    async def snapshot(self):
        if self.queue is None:
            return {"high": [], "low": []}
        return await self.queue.snapshot()

    async def status(self):
        speaking = False
        if self.adapter is not None:
            try:
                speaking = await self.adapter.is_speaking()
            except Exception:
                speaking = False
        return {
            "running": self.running,
            "room_id": self.room_id,
            "session_id": self.session_id,
            "platform": self.platform,
            # 注意：PlayQueue 的 high_length/low_length/total_length 是 property，不是方法
            "queue_high_count": self.queue.high_length if self.queue else 0,
            "queue_low_count": self.queue.low_length if self.queue else 0,
            "danmaku_count": self.danmaku_count,
            "is_speaking": speaking,
        }

    # ── 启动 / 停止 ─────────────────────────────────────────────
    async def start(self, queue, llm, collector, adapter, session_id: str,
                    room_id: str, platform: str, persona: dict,
                    min_size: int = 2, interval: float = 3.0):
        if self.running:
            await self.stop()

        self.queue = queue
        self.llm = llm
        self.collector = collector
        self.adapter = adapter
        self.session_id = session_id or ""
        self.room_id = room_id or ""
        self.platform = platform or "bilibili"
        self.danmaku_count = 0

        # 1. 重新加载最新人设（前端可能刚改过 persona）
        if llm is not None and persona:
            llm.persona = persona

        # 2. 绑定会话 + 订阅播放结束事件（替代原来的 /sse 连接）
        self.adapter.set_session(self.session_id)
        if self.adapter.resolve()[1] is None:
            raise RuntimeError(f"LiveTalking 会话不存在（sessionid={self.session_id or '(空)'}）"
                               f"，请先在 index.html 点「开始连接」建立会话")
        await self.adapter.connect()
        self.adapter.on_playback_ended(self.on_playback_end)

        # 3. 队列自动补位
        await queue.start_auto_fill(min_size=min_size, interval=interval)

        # 4. 连接弹幕平台
        self.collector.on_message(self._on_danmaku)
        try:
            await self.collector.connect(self.platform, self.room_id)
        except Exception as e:
            # 采集器连不上不应该让整场直播起不来（原来会直接 500 且状态半途不一致）
            logger.error(f"[ls] 弹幕采集器连接失败（{self.platform}/{self.room_id}）: {e}；"
                         f"直播继续，仅无弹幕")

        self.running = True
        # 5. 首次触发：手动启动第一次出队播放
        self._play_task = asyncio.create_task(self._try_play_next())
        await self._emit({"type": "status_change", "running": True, "room_id": self.room_id})
        logger.info(f"[ls] 直播已启动 room={self.room_id} session={self.session_id[:8]} "
                    f"platform={self.platform}")
        return {"code": 0, "msg": "ok"}

    async def stop(self):
        was = self.running
        self.running = False
        # 合并版特有：一个进程会反复 start/stop，必须把等待出队的循环收掉
        if self._play_task is not None:
            self._play_task.cancel()
            try:
                await self._play_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning(f"[ls] 收尾出队任务异常: {e}")
            self._play_task = None
        if self.collector is not None:
            try:
                await self.collector.disconnect()
            except Exception as e:
                logger.warning(f"[ls] 采集器断开异常: {e}")
        if self.queue is not None:
            try:
                await self.queue.stop_auto_fill()
            except Exception as e:
                logger.warning(f"[ls] 停止自动补位异常: {e}")
            try:
                await self.queue.clear()
            except Exception as e:
                logger.warning(f"[ls] 清空队列异常: {e}")
        # 合并版特有：点了「停止直播」不能还在继续说
        if self.adapter is not None:
            _sid, sess = self.adapter.resolve()
            if sess is not None and callable(getattr(sess, 'flush_talk', None)):
                try:
                    sess.flush_talk()
                except Exception:
                    pass
            await self.adapter.disconnect()
        if was:
            await self._emit({"type": "status_change", "running": False})
        logger.info("[ls] 直播已停止")
        return {"code": 0, "msg": "ok"}

    async def interrupt(self):
        """打断当前播报（上游：POST /api/livestream/interrupt → lt_client.interrupt()）。"""
        if self.adapter is None:
            return {"code": 0, "msg": "ok"}
        _sid, sess = self.adapter.resolve()
        if sess is not None and callable(getattr(sess, 'flush_talk', None)):
            try:
                sess.flush_talk()
                logger.info("[ls] 已打断当前播报")
            except Exception as e:
                logger.warning(f"[ls] 打断失败: {e}")
        return {"code": 0, "msg": "ok"}

    # ── 播放调度（与上游一致：出队锁 + 播完取一条）───────────────
    async def _do_send(self, item):
        """把一条交给 LiveTalking（上游 do_send）。"""
        if item.type == "video" and item.content:
            # LiveTalking 侧没有 convert_custom_media / load_media（上游预留未实现）
            raise RuntimeError("video 类型话术暂不支持（LiveTalking 无 load_media 接口）")
        if item.type == "audio" and item.content:
            res = await self.adapter.send_audio(item.content)
        else:
            res = await self.adapter.send_text(item.content)
        if isinstance(res, dict) and res.get("code") not in (0, None):
            raise RuntimeError(f"发送失败: {res.get('msg')}")
        await self._emit({
            "type": "playback_started",
            "item_id": item.id,
            "source": item.source,
            "content_preview": (item.content or "")[:80],
        })
        logger.info(f"[ls] → 会话 [{item.source}] {(item.content or '')[:40]}")

    async def _try_play_next(self):
        """出队并发送（带锁，同一时刻只执行一次）—— 上游 try_play_next。

        比上游多一个 self.running 判断：合并版同一个进程会反复 start/stop，
        循环必须能退出，否则旧队列的话术会被发到下一个会话上。
        """
        if self._playback_lock.locked():
            return
        async with self._playback_lock:
            item = None
            while self.running:
                item = await self.queue.get_next()
                if item is not None:
                    break
                await asyncio.sleep(0.3)
            if item is None:
                return
            await self._do_send(item)

    async def on_playback_end(self):
        """收到播放结束事件：出队下一条（上游同名回调）。"""
        await self._try_play_next()

    # ── 弹幕（与上游一致：逐条回复 / 礼物 / 关注 → 高优队列）──────
    async def _on_danmaku(self, msg):
        await self._emit({
            "type": getattr(msg, "msg_type", "danmaku"),
            "platform": getattr(msg, "platform", ""),
            "sender": getattr(msg, "sender", ""),
            "content": getattr(msg, "content", ""),
            "timestamp": getattr(msg, "timestamp", 0),
        })
        self.danmaku_count += 1

        from server.livestream.services.play_queue import QueueItem
        msg_type = getattr(msg, "msg_type", "danmaku")
        if msg_type == "danmaku" and self.llm:
            try:
                reply = await self.llm.generate_reply(msg.content, msg.sender)
            except Exception as e:
                logger.error(f"[ls] 弹幕回复生成失败: {e}")
                reply = ""
            if reply:
                logger.info(f"[ls] 弹幕回复 [{msg.sender}]: {reply[:60]}")
                await self.queue.put_high(QueueItem(
                    type="text", content=reply, source="danmaku",
                    metadata={"sender": msg.sender, "original": msg.content},
                ))
        elif msg_type == "gift":
            await self.queue.put_high(QueueItem(
                type="text", content=f"谢谢{msg.sender}的{msg.content}！", source="gift",
                metadata={"sender": msg.sender},
            ))
        elif msg_type == "follow":
            await self.queue.put_high(QueueItem(
                type="text", content=f"欢迎{msg.sender}关注直播间！", source="follow",
                metadata={"sender": msg.sender},
            ))

    async def ingest(self, msg):
        """外部来源的弹幕塞进来（Windows 侧转发的抖音弹幕走这里）。

        走的路径和采集器回调完全一致：广播事件 → 逐条生成回复 → 高优队列。
        """
        await self._on_danmaku(msg)

    async def mock_danmaku(self, msg_type: str, sender: str, content: str):
        """测试面板用：绕过采集器直接走 on_message 路径。"""
        from server.livestream.services.danmaku.base import DanmakuMessage
        # 注意 DanmakuMessage 的 platform 是必填首参（无默认值）
        msg = DanmakuMessage(platform=self.platform or "mock", sender=sender or "测试观众",
                             content=content or "", msg_type=msg_type or "danmaku")
        await self._on_danmaku(msg)
        return {"code": 0, "msg": "ok"}


# 全局单例
runtime = LiveStreamRuntime()
