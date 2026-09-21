###############################################################################
#  LiveStream 播放引擎（进程内版）
#
#  原实现散在 FastAPI 的 POST /api/livestream/start 闭包与 app.state 里
#  （backend/app/api.py:446-629）。合并进 LiveTalking 后抽成一个类：
#    · 预送流水线：始终「1 条在播 + 1 条已预送」→ 话术之间零空白
#    · 弹幕插队：当前这句一定说完，回复紧跟其后；已预送的那条抽回后补发
#    · 弹幕聚合：攒满 3 条 → 立刻收回最前 3 条合并成「一句话」；
#                 不足 3 条 → 等下一条弹幕来凑批，或等满超时秒数后单独回一句
#    · 兜底看门狗：事件丢了也能推进，绝不因为没有 end 事件停摆
#  与 HTTP 无关，便于单测；routes.py 只做参数解析与状态查询。
###############################################################################

import asyncio
import time
import uuid

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

        # 预送流水线
        self._lock = asyncio.Lock()
        self._inflight: list = []            # 已发出、未播完（[0] 正在播）
        self._prefetch = 1
        self._idle_ticks = 0

        # 弹幕聚合
        self._danmaku_buf: list = []
        self._batch_trigger = 3              # 未回复弹幕攒满 3 条 → 立即合并回一句
        self._batch_wait = 6.0               # 不足 3 条：等下一条来凑批，或等满 6 秒单独回
        self._max_chars = 60
        self._policy = ""

        self._tasks: list = []
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
            "inflight": len(self._inflight),
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
        self._idle_ticks = 0
        self._inflight = []
        self._danmaku_buf = []

        persona = persona or {}
        self._policy = persona.get("danmaku_policy") or ""
        self._batch_trigger = max(1, int(persona.get("danmaku_batch_trigger") or 3))
        self._batch_wait = float(persona.get("danmaku_batch_wait") or 6.0)
        self._max_chars = int(persona.get("danmaku_max_chars") or 60)
        logger.info(f"[ls] 弹幕聚合设定: 触发条数={self._batch_trigger} 等待={self._batch_wait}s "
                    f"上限={self._max_chars}字 策略={'有' if self._policy else '无'}")

        # 1. 绑定会话 + 订阅播放结束事件（替代原来的 /sse 连接）
        self.adapter.set_session(self.session_id)
        if self.adapter.resolve()[1] is None:
            raise RuntimeError(f"LiveTalking 会话不存在（sessionid={self.session_id or '(空)'}）"
                               f"，请先在 index.html 点「开始连接」建立会话")
        await self.adapter.connect()
        self.adapter.on_playback_ended(self.on_playback_end)

        # 2. 队列自动补位
        await queue.start_auto_fill(min_size=min_size, interval=interval)

        # 3. 连接弹幕平台
        self.collector.on_message(self._on_danmaku)
        try:
            await self.collector.connect(self.platform, self.room_id)
        except Exception as e:
            # 采集器连不上不应该让整场直播起不来（原来会直接 500 且状态半途不一致）
            logger.error(f"[ls] 弹幕采集器连接失败（{self.platform}/{self.room_id}）: {e}；"
                         f"直播继续，仅无弹幕")

        self.running = True
        self._tasks = [
            asyncio.create_task(self._pump_watchdog()),
            asyncio.create_task(self._danmaku_aggregator()),
        ]
        await self._prefill()                # 起点：第一条立刻送出去
        await self._emit({"type": "status_change", "running": True, "room_id": self.room_id})
        logger.info(f"[ls] 直播已启动 room={self.room_id} session={self.session_id[:8]} "
                    f"platform={self.platform}")
        return {"code": 0, "msg": "ok"}

    async def stop(self):
        was = self.running
        self.running = False
        for t in self._tasks:
            t.cancel()
        self._tasks = []
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
        # 清掉还没播的预送话术 + 打断正在合成的那条
        if self.adapter is not None:
            try:
                await self._withdraw_pending()
            except Exception:
                pass
        sid, sess = self.adapter.resolve() if self.adapter else ("", None)
        if sess is not None and callable(getattr(sess, 'flush_talk', None)):
            try:
                sess.flush_talk()
            except Exception:
                pass
        self._inflight = []
        self._danmaku_buf = []
        if self.adapter is not None:
            await self.adapter.disconnect()
        if was:
            await self._emit({"type": "status_change", "running": False})
        logger.info("[ls] 直播已停止")
        return {"code": 0, "msg": "ok"}

    async def interrupt(self):
        """打断当前播报：清掉未播话术 + 停掉正在合成的 TTS。未开播时安全返回。"""
        n = len(self._inflight)
        if self.adapter is None:
            self._inflight = []
            self._danmaku_buf = []
            return {"code": 0, "msg": "ok", "dropped": 0}
        await self._withdraw_pending()
        sid, sess = self.adapter.resolve()
        if sess is not None and callable(getattr(sess, 'flush_talk', None)):
            try:
                sess.flush_talk()
            except Exception:
                pass
        async with self._lock:
            self._inflight = []
        self._danmaku_buf = []
        logger.info(f"[ls] 已打断（撤回 {n} 条）")
        return {"code": 0, "msg": "ok", "dropped": n}

    # ── 预送流水线 ──────────────────────────────────────────────
    async def _do_send(self, item, priority: bool = False):
        """把一条交给 LiveTalking。用 item.id 当 utt，便于撤回未开播的那条。"""
        utt = item.id
        if item.metadata is None:
            item.metadata = {}
        item.metadata["utt"] = utt
        if item.type == "audio":
            res = await self.adapter.send_audio(item.content, utt=utt, priority=priority)
        elif item.type == "video":
            # LiveTalking 侧没有 convert_custom_media / load_custom_media（上游预留未实现）
            raise RuntimeError("video 类型话术暂不支持（LiveTalking 无 convert_custom_media 接口）")
        else:
            res = await self.adapter.send_text(item.content, utt=utt, priority=priority)
        if isinstance(res, dict) and res.get("code") not in (0, None):
            raise RuntimeError(f"发送失败: {res.get('msg')}")
        self._inflight.append(item)
        await self._emit({
            "type": "playback_started",
            "item_id": item.id,
            "source": item.source,
            "priority": priority,
            "inflight": len(self._inflight),
            "content_preview": (item.content or "")[:80],
        })
        logger.info(f"[ls] → 会话 utt={utt} priority={priority} inflight={len(self._inflight)} "
                    f"[{item.source}] {(item.content or '')[:40]}")
        return True

    async def _prefill(self):
        """把预送窗口填满。不阻塞等待：队列空就返回，靠 auto-fill / 弹幕再触发。"""
        async with self._lock:
            while len(self._inflight) <= self._prefetch:
                item = await self.queue.get_next()
                if item is None:
                    # 队列干涸：auto-fill 每 3 秒才补一条，等它就是「话术之间出现空白」。
                    # 这里直接向话术库要一条（random_pick 内部已做权重 + 最近 N 条不重复）。
                    filler = getattr(self.queue, '_script_manager', None)
                    if filler is not None:
                        try:
                            item = await filler.random_pick()
                        except Exception as e:
                            logger.warning(f"[ls] 话术库补位失败: {e}")
                            item = None
                    if item is None:
                        break
                try:
                    await self._do_send(item)
                except Exception as e:
                    logger.error(f"[ls] 发送失败: {e}")
                    break

    async def _withdraw_pending(self):
        """撤回「已预送、还没开播」的那条（正在播的不动），给它腾出紧邻位置。"""
        async with self._lock:
            pending = self._inflight[1:]
            del self._inflight[1:]
        for it in pending:
            try:
                await self.adapter.drop_queued_talk(it.metadata.get("utt", ""))
            except Exception as e:
                logger.warning(f"[ls] 撤回失败: {e}")
        return pending

    async def push_priority(self, text: str, source: str = "danmaku", metadata: dict = None):
        """插队播报：当前这句一定说完，回复紧跟其后。"""
        if not text or not text.strip():
            return
        from server.livestream.services.play_queue import QueueItem
        item = QueueItem(type="text", content=text, source=source, metadata=metadata or {})
        pending = await self._withdraw_pending()      # ① 抽回已预送的那条
        await self._do_send(item, priority=True)      # ② 回复紧跟在当前这句之后
        for p in pending:                            # ③ 抽回来的补在后面
            try:
                await self._do_send(p)
            except Exception as e:
                logger.error(f"[ls] 补发撤回项失败: {e}")

    async def on_playback_end(self):
        """收到播放结束事件：腾出窗口并立刻补位（保证下一条早已在队列里）。"""
        done = None
        async with self._lock:
            if self._inflight:
                done = self._inflight.pop(0)
        if done is not None:
            logger.info(f"[ls] 播完 [{done.source}] utt={done.metadata.get('utt')}")
        if self.running:
            await self._prefill()

    async def _pump_watchdog(self):
        """兜底：事件丢失 / 启动瞬间也能推进（绝不能因为没有 end 事件就停摆）。"""
        while self.running:
            # 窗口空时用更密的节拍（0.5s）补位，缩小「无话可说」的空窗
            async with self._lock:
                n0 = len(self._inflight)
            await asyncio.sleep(0.5 if n0 == 0 else 1.0)
            try:
                async with self._lock:
                    n = len(self._inflight)
                if n == 0:
                    await self._prefill()
                    continue
                if await self.adapter.is_speaking():
                    self._idle_ticks = 0
                    continue
                self._idle_ticks += 1
                if n == 1 and self._idle_ticks >= 3:
                    logger.warning("[ls] watchdog: 连续 3 秒未收到 end 且未在说话 → 手动推进")
                    self._idle_ticks = 0
                    await self.on_playback_end()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[ls] watchdog 异常: {e}")

    # ── 弹幕：入缓冲 → 聚合 → 合并成一句 → 插队 ─────────────────
    async def _on_danmaku(self, msg):
        await self._emit({
            "type": "danmaku",
            "msg_type": getattr(msg, "msg_type", ""),
            "sender": getattr(msg, "sender", ""),
            "content": getattr(msg, "content", ""),
        })
        self.danmaku_count += 1
        self._danmaku_buf.append({
            "kind": getattr(msg, "msg_type", "danmaku"),
            "sender": getattr(msg, "sender", ""),
            "content": getattr(msg, "content", ""),
        })
        logger.info(f"[ls] 弹幕入缓冲（{len(self._danmaku_buf)}/{self._batch_trigger}）"
                    f"[{getattr(msg, 'msg_type', '')}] {getattr(msg, 'sender', '')}: "
                    f"{(getattr(msg, 'content', '') or '')[:40]}")

    async def mock_danmaku(self, msg_type: str, sender: str, content: str):
        """测试面板用：绕过采集器直接走 on_message 路径。"""
        from server.livestream.services.danmaku.base import DanmakuMessage
        # 注意 DanmakuMessage 的 platform 是必填首参（无默认值）
        msg = DanmakuMessage(platform=self.platform or "mock", sender=sender or "观众",
                             content=content or "", msg_type=msg_type or "danmaku")
        await self._on_danmaku(msg)
        return {"code": 0, "msg": "ok"}

    async def _danmaku_aggregator(self):
        """弹幕聚合（实时排队版）：
           · 未回复弹幕攒满 batch_trigger 条 → 立刻「收回」最前这几条 → 一次 LLM 调用 → 合成一句话
           · 不足 trigger 条 → 等下一条弹幕来凑批；等满 batch_wait 秒仍不足 → 这几条单独回一句
           · 回复走 push_priority 插队：当前正在播的那句一定说完，回复紧跟其后
        """
        batch_start = None           # 当前这批「第一条」弹幕的入缓冲时刻
        while self.running:
            await asyncio.sleep(0.1)
            try:
                if not self._danmaku_buf:
                    batch_start = None
                    continue
                now = time.time()
                if batch_start is None:
                    batch_start = now
                n = len(self._danmaku_buf)
                if n >= self._batch_trigger:
                    take = self._batch_trigger        # 攒满：只收回 trigger 条，多出来的留给下一批
                elif now - batch_start >= self._batch_wait:
                    take = n                          # 超时：1~2 条也单独回一句，不让用户干等
                else:
                    continue
                batch = self._danmaku_buf[:take]
                del self._danmaku_buf[:take]
                batch_start = None                    # 剩余弹幕重新起算等待窗口
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[ls] 弹幕聚合读取异常: {e}")
                continue
            try:
                merged = await self.llm.generate_merged_reply(
                    batch, policy=self._policy, max_chars=self._max_chars)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[ls] 弹幕聚合失败: {e}")
                merged = ""
            await self._emit({
                "type": "danmaku_batch",
                "count": len(batch),
                "merged": merged,
                "items": [{"kind": b.get("kind"), "sender": b.get("sender"),
                           "content": (b.get("content") or "")[:60]} for b in batch],
            })
            if merged:
                logger.info(f"[ls] 弹幕合并成一句（{len(batch)} 条）: {merged}")
                try:
                    await self.push_priority(merged, source="danmaku", metadata={
                        "batch_count": len(batch), "merged": True,
                        "senders": [b.get("sender") for b in batch],
                    })
                except Exception as e:
                    logger.error(f"[ls] 弹幕插队失败: {e}")
            else:
                logger.info(f"[ls] 这 {len(batch)} 条弹幕判定为无需回应（跳过，不占队列）: "
                            + " | ".join((b.get("content") or "")[:24] for b in batch))


# 全局单例
runtime = LiveStreamRuntime()


def new_item_id() -> str:
    return uuid.uuid4().hex[:12]
