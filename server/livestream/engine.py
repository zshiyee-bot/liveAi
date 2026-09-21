###############################################################################
#  LiveStream 播放引擎（进程内版）
#
#  弹幕侧行为 = 上游 lipku/LiveStream 原版：每条弹幕单独交给 LLM 生成一句回复，
#  进高优队列；礼物「谢谢X的Y！」、关注「欢迎X关注直播间！」。
#
#  播放调度比上游多一层「预送流水线」，目的是**消除两条话术之间的空白**：
#    · 始终维持「1 条在播 + 1 条已预送」——播完一条立刻就有下一条在合成，
#      不用等 TTS 首包（豆包首包实测 0.9~2.0s，那正是那段空白）
#    · 弹幕/礼物/关注来了 → 先抽回「还没开播的低优话术」，把回复排到当前这句
#      之后，再把抽回的话术补发 → 弹幕优先，且弹幕之间保持 FIFO 不乱序
#    · 看门狗兜底：end 事件丢了也能推进，绝不因为没有事件就停摆
#
#  与上游唯一的差别：原来 LiveStream 是独立进程、通过 HTTP 访问 LiveTalking
#  （/human、/humanaudio、/sse …）；现在合并进同一进程，改由
#  LocalAvatarAdapter 直接操作 avatar_session。
###############################################################################

import asyncio

from utils.logger import logger


class LiveStreamRuntime:
    def __init__(self):
        self.queue = None                    # PlayQueue
        self.llm = None                      # LLMService
        self.collector = None                # MultiPlatformCollector
        self.adapter = None                  # LocalAvatarAdapter（每房间一个）

        self.running = False
        self.room_id = ""
        self.session_id = ""
        self.platform = ""
        self.danmaku_count = 0

        # ── 预送流水线 ──
        self._lock = asyncio.Lock()
        self._inflight: list = []            # 已发出、未播完（[0] 正在播）
        self._prefetch = 1                   # 除正在播的那条外，再预送几条
        self._idle_ticks = 0
        self._tasks: list = []               # 看门狗等后台任务

        self._status_cbs: list = []

    @property
    def inflight_count(self) -> int:
        """已发出未播完的条数（[0] 正在播）—— 诊断页/状态接口用。"""
        return len(self._inflight)

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
        """播放队列快照（含「已预送、马上播」的那些）。

        零接缝会把下一条提前送进 TTS 的待播队列，于是它已经不在 PlayQueue 里了 ——
        运营页就会看不到「弹幕回复正在等着播」。所以这里把预送窗口里的条目按来源
        并回高优/低优列表：
          · _inflight[0] 是"正在播"，前端用 playback_started 事件单独显示，不重复列
          · 其余（已预送）按 source 摆回 high/low，前端队列面板就能看到弹幕回复
        注意：这里故意不加 self._lock —— 本函数会被 _do_send 在 _prefill 持锁时调用，
        再取同一把锁会死锁。列表切片不会让出事件循环，读到的视图足够一致。
        """
        if self.queue is None:
            return {"high": [], "low": []}
        snap = await self.queue.snapshot()
        pending = list(self._inflight[1:])
        for it in pending:
            row = {
                "id": it.id,
                "type": it.type,
                "source": it.source,
                "content_preview": (it.content or "")[:80],
                "level": it.level,
                "presend": True,          # 标记：已预送（前端不认识也无害）
            }
            if it.source == "script":
                snap.setdefault("low", []).append(row)
            else:
                snap.setdefault("high", []).append(row)
        return snap

    async def _emit_playing(self):
        """广播「现在在播哪条」（前端「正在播放」靠它）。

        _inflight[0] 才是当前在播的那条：上一条收到 end 事件后它才轮到。
        没有它就别报 —— 否则前端会一直显示上一条已经播完的内容。
        """
        if not self._inflight:
            return
        cur = self._inflight[0]
        await self._emit({
            "type": "playback_started",
            "item_id": cur.id,
            "source": cur.source,
            "inflight": len(self._inflight),
            "content_preview": (cur.content or "")[:80],
        })

    async def _notify_queue(self):
        """让运营页刷新播放队列。

        预送窗口变化（送出一条 / 播完一条）不会经过 PlayQueue，也就不会触发
        PlayQueue 的 on_change 回调 —— 不主动推一次的话，队列面板上就看不见
        弹幕回复进进出出。
        """
        try:
            await self._emit({"type": "queue_update", "data": await self.snapshot()})
        except Exception as e:
            logger.warning(f"[ls] 队列广播失败: {e}")

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
        # 5. 看门狗 + 把预送窗口填满（起点：第一条立刻送出去，后面才有"无缝"可言）
        self._tasks = [asyncio.create_task(self._pump_watchdog())]
        await self._prefill()
        await self._emit_playing()          # 第一条开始播
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
            try:
                await self.queue.clear()
            except Exception as e:
                logger.warning(f"[ls] 清空队列异常: {e}")
        # 打断正在播/正在合成的那条（flush_talk 会清掉 TTS 的待播队列）
        if self.adapter is not None:
            _sid, sess = self.adapter.resolve()
            if sess is not None and callable(getattr(sess, 'flush_talk', None)):
                try:
                    sess.flush_talk()
                except Exception:
                    pass
        async with self._lock:
            self._inflight = []
        if self.adapter is not None:
            await self.adapter.disconnect()
        if was:
            await self._emit({"type": "status_change", "running": False})
        logger.info("[ls] 直播已停止")
        return {"code": 0, "msg": "ok"}

    async def interrupt(self):
        """打断当前播报：清掉未播的内容 + 停掉正在合成的 TTS。未开播时安全返回。"""
        n = len(self._inflight)
        if self.adapter is None:
            async with self._lock:
                self._inflight = []
            return {"code": 0, "msg": "ok", "dropped": n}
        _sid, sess = self.adapter.resolve()
        if sess is not None and callable(getattr(sess, 'flush_talk', None)):
            try:
                sess.flush_talk()
                logger.info(f"[ls] 已打断（清掉 {n} 条未播内容）")
            except Exception as e:
                logger.warning(f"[ls] 打断失败: {e}")
        async with self._lock:
            self._inflight = []
        return {"code": 0, "msg": "ok", "dropped": n}

    # ── 预送流水线（消除话术之间的空白）──────────────────────────
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
        # 这里**故意不报 playback_started**：预送模式下"发出去"≠"开始播"
        # （发出去的下一条还在 TTS 队列里等着）。真正的开始播边界是播放结束事件，
        # 见 _emit_playing()。原来在发送时就报，会让「正在播放」显示成预送的那条，
        # 单条弹幕的回复会"闪一下就没了"。
        logger.info(f"[ls] → 会话 utt={utt} priority={priority} inflight={len(self._inflight)} "
                    f"[{item.source}] {(item.content or '')[:40]}")
        await self._notify_queue()
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

    async def _withdraw_scripts(self):
        """抽回「已预送、还没开播」的**低优话术**（正在播的那条不动）。

        只抽话术、不动已经排上的弹幕/礼物回复，原因：
          · 目的只是别让话术挡住弹幕的位置；
          · TTS 的 priority=True 是 appendleft（后到的排最前），如果连之前的弹幕
            回复也一起抽回来重发，连发弹幕时回复顺序会颠倒。
        """
        async with self._lock:
            keep, drop = [], []
            for i, it in enumerate(self._inflight):
                if i > 0 and it.source == "script":
                    drop.append(it)
                else:
                    keep.append(it)
            self._inflight = keep
        for it in drop:
            try:
                await self.adapter.drop_queued_talk(it.metadata.get("utt", ""))
            except Exception as e:
                logger.warning(f"[ls] 撤回预送话术失败: {e}")
        return drop

    async def push_priority(self, text: str, source: str = "danmaku", metadata: dict = None):
        """插队播报：当前这句一定说完，这条紧跟其后（不让预送话术挡在前面）。"""
        if not text or not text.strip():
            return
        from server.livestream.services.play_queue import QueueItem
        item = QueueItem(type="text", content=text, source=source, metadata=metadata or {})
        pending = await self._withdraw_scripts()      # ① 抽回预送的话术，腾出紧邻位置
        try:
            await self._do_send(item)                 # ② 追加（FIFO → 弹幕之间不乱序）
        except Exception as e:
            logger.error(f"[ls] 插队发送失败: {e}")
        for p in pending:                             # ③ 抽回的话术补在后面
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
        # 上一条播完了 → 轮到的这条才是"正在播"，在这里报才准
        await self._emit_playing()
        await self._notify_queue()

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

    # ── 弹幕（与上游一致：逐条回复 / 礼物 / 关注）──────────────────
    async def _on_danmaku(self, msg):
        await self._emit({
            "type": getattr(msg, "msg_type", "danmaku"),
            "platform": getattr(msg, "platform", ""),
            "sender": getattr(msg, "sender", ""),
            "content": getattr(msg, "content", ""),
            "timestamp": getattr(msg, "timestamp", 0),
        })
        self.danmaku_count += 1

        msg_type = getattr(msg, "msg_type", "danmaku")
        if msg_type == "danmaku" and self.llm:
            try:
                reply = await self.llm.generate_reply(msg.content, msg.sender)
            except Exception as e:
                logger.error(f"[ls] 弹幕回复生成失败: {e}")
                reply = ""
            if reply:
                logger.info(f"[ls] 弹幕回复 [{msg.sender}]: {reply[:60]}")
                # 插队播报：当前这句说完就播，不排在预送的话术后面
                await self.push_priority(reply, source="danmaku", metadata={
                    "sender": msg.sender, "original": msg.content,
                })
        elif msg_type == "gift":
            await self.push_priority(f"谢谢{msg.sender}的{msg.content}！", source="gift",
                                     metadata={"sender": msg.sender})
        elif msg_type == "follow":
            await self.push_priority(f"欢迎{msg.sender}关注直播间！", source="follow",
                                     metadata={"sender": msg.sender})

    async def ingest(self, msg):
        """外部来源的弹幕塞进来（Windows 侧转发的抖音弹幕走这里）。

        走的路径和采集器回调完全一致：广播事件 → 逐条生成回复 → 插队播报。
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


# 全局单例（仅兼容旧引用；多房间模式下每个房间各自 new 一个）
runtime = LiveStreamRuntime()
