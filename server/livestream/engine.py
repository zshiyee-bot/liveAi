###############################################################################
#  LiveStream 播放引擎（进程内版）
#
#  弹幕回复方式由人设决定（见 apply_persona / mode_desc）：
#    · danmaku_batch_trigger == 1 → 逐条回复：每条弹幕单独交给 LLM 生成一句
#    · danmaku_batch_trigger >= 2 → 聚合回复：攒够 N 条（或等满 batch_wait 秒）
#      合并成【一句】；LLM 判定"全都不值得回应"就跳过
#    礼物「谢谢X的Y！」、关注「欢迎X关注直播间！」；进场/分享等只广播不回复
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
import collections
import random
import re
import time

from server.livestream.services.play_queue import QueueItem, PlayQueue
from server.livestream.services.tts_style import strip_all_and_rate, effective_rate
from server.livestream.services.danmaku_filter import (
    DEFAULT_FALLBACK, clean_msg, clean_name, clean_paraphrase, hit_block_word, is_noise,
    looks_like_injection, parse_templates, parse_words, pick_template,
    render_reply, sanitize_reply, screen, ungrounded_mentions,
)

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
        self._tasks: list = []               # 看门狗 / 弹幕聚合 等后台任务

        # ── 弹幕回复方式（人设里配，见 apply_persona）──
        #   _batch_trigger == 1 → 逐条回复；>= 2 → 攒够 N 条（或等满 _batch_wait 秒）合并成一句
        self._danmaku_buf: list = []
        self._batch_trigger = 3
        self._batch_wait = 3.0
        self._max_chars = 60
        self._policy = ""

        # ── 弹幕安全 / 口播方式（同样来自人设，见 apply_persona）──
        #  这些全部是**确定性**的：命中就挡、超长就截，不依赖模型自觉。
        #  实现都在 services/danmaku_filter.py，这里只存配置 + 调用。
        self._block_words: list = []         # 屏蔽词
        self._block_mode = "exact"           # exact = 整条相同（"1" 不误杀「扣1」）
        self._block_noise = True             # 纯数字/纯符号/重复字 → 不回
        self._inject_filter = True           # 注入攻击过滤
        self._dmax_len = 60                  # 单条弹幕长度上限，超出不回
        self._rate_limit = 3                 # 每人每 10 秒最多回几条
        self._rate_win: dict = {}            # sender -> deque[时间戳]
        self._fallback = "这个我就不接了啊，咱们还是聊产品。"
        self._call_name = False              # 用不用带 {name} 的模板
        self._read_msg = False               # 用不用带 {msg} 的模板
        self._name_max = 6
        self._msg_max = 24
        self._templates: list = []           # 回复模板（空 = 用内置默认）

        self._status_cbs: list = []

    @property
    def inflight_count(self) -> int:
        """已发出未播完的条数（[0] 正在播）—— 诊断页/状态接口用。"""
        return len(self._inflight)

    def mode_desc(self) -> str:
        """当前弹幕回复方式的可读描述（日志/诊断用）。"""
        if self._batch_trigger <= 1:
            return "逐条回复"
        return f"每 {self._batch_trigger} 条合并成一句（兜底 {self._batch_wait:g}s）"

    def apply_persona(self, persona: dict):
        """人设变化时刷新弹幕回复方式（不影响正在播的内容）。"""
        persona = persona or {}
        self._policy = persona.get("danmaku_policy") or ""
        try:
            self._batch_trigger = max(1, int(persona.get("danmaku_batch_trigger") or 3))
        except Exception:
            self._batch_trigger = 3
        try:
            self._batch_wait = max(0.5, min(30.0, float(persona.get("danmaku_batch_wait") or 3.0)))
        except Exception:
            self._batch_wait = 3.0
        try:
            self._max_chars = max(10, min(200, int(persona.get("danmaku_max_chars") or 60)))
        except Exception:
            self._max_chars = 60

        # ── 弹幕安全 / 口播方式 ──
        self._block_words = parse_words(persona.get("danmaku_block_words") or "")
        self._block_mode = "contains" if persona.get("danmaku_block_mode") == "contains" else "exact"
        self._block_noise = bool(persona.get("danmaku_block_noise", 1))
        self._inject_filter = bool(persona.get("danmaku_inject_filter", 1))
        self._fallback = (persona.get("danmaku_fallback") or "").strip() or DEFAULT_FALLBACK
        self._call_name = bool(persona.get("danmaku_call_name", 0))
        self._read_msg = bool(persona.get("danmaku_read_msg", 0))
        self._templates = parse_templates(persona.get("danmaku_reply_templates") or "")
        for key, attr, lo, hi, dflt in (
            ("danmaku_max_len", "_dmax_len", 10, 500, 60),
            ("danmaku_rate_limit", "_rate_limit", 0, 50, 3),
            ("danmaku_name_max", "_name_max", 1, 20, 6),
            ("danmaku_read_msg_max", "_msg_max", 2, 100, 24),
        ):
            try:
                v = int(persona.get(key) if persona.get(key) is not None else dflt)
                setattr(self, attr, max(lo, min(hi, v)))
            except Exception:
                setattr(self, attr, dflt)

    # ── 弹幕安全：进门第一道闸（确定性）─────────────────────────────
    def _screen(self, content: str, sender: str) -> str | None:
        """返回挡下的原因；None = 放行。

        判定顺序（长度 → 注入 → 屏蔽词 → 噪音）统一在 danmaku_filter.screen() 里，
        独立工具包用的是同一个函数，避免两边规则走偏。这里只多管一件事：刷屏限速。
        """
        reason = screen(content, {
            "block_words": self._block_words,
            "block_mode": self._block_mode,
            "block_noise": self._block_noise,
            "inject_filter": self._inject_filter,
            "max_len": self._dmax_len,
        })
        if reason:
            return reason
        if self._rate_limited(sender):
            return f"刷屏太快（{self._rate_limit} 条 / 10 秒）"
        return None

    def _rate_limited(self, sender: str) -> bool:
        """同一个人 10 秒内超过 N 条 → 后面的先不回（只是不回，不是拉黑）。"""
        if self._rate_limit <= 0:
            return False
        now = time.time()
        key = (sender or "?").strip() or "?"
        q = self._rate_win.get(key)
        if q is None:
            if len(self._rate_win) > 500:          # 防止字典无限膨胀
                self._rate_win.clear()
            q = self._rate_win[key] = collections.deque()
        while q and now - q[0] > 10.0:
            q.popleft()
        if len(q) >= self._rate_limit:
            return True
        q.append(now)
        return False

    def _wrap_reply(self, reply: str, sender: str, msg: str, para: str = "",
                    allow_name: bool = True) -> str:
        """套模板：称呼（读名字）+ 转述（念弹幕）都在这一层加。

        **`para` 是 AI 对观众那句话的"转述"，不是观众原话** —— 真主播从不照抄弹幕，
        而是概括一下再说（观众说「主播你现在在哪里直播呀」→ 念成「问我人在哪儿」）。
        转述拿不到/不合格时，带 {msg} 的模板会自动跳过，退化成不带复述的句式。
        """
        name = clean_name(sender, self._name_max, self._block_words) if (self._call_name and allow_name) else ""
        m = clean_paraphrase(para, msg, self._msg_max, self._block_words) if self._read_msg else ""
        tpl = pick_template(self._templates, allow_name=self._call_name and allow_name,
                            allow_msg=self._read_msg, has_name=bool(name), has_msg=bool(m))
        out = render_reply(tpl, name, m, reply)
        # 套完再洗一次（防止模板本身拼出怪东西）；**不截断** —— 正文已经限过长了，
        # 这里再按正文长度截会把刚加上的称呼/原文又砍掉。
        return sanitize_reply(out, 0, self._fallback)

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
            return {"high": [], "low": [], "playing": None}
        snap = await self.queue.snapshot()

        def _row(it, playing: bool) -> dict:
            return {
                "id": it.id,
                "type": it.type,
                "source": it.source,
                "content_preview": (it.content or "")[:80],
                # 低优只放话术，其余（弹幕/礼物/关注）算高优
                "level": "low" if it.source == "script" else "high",
                "presend": not playing,
                "playing": playing,
            }

        # 「正在播」单独一个字段给前端（不要混进 high/low：前端本来就有一行
        # 「正在播放」，混进去会重复显示）。播完了这里就是 None，
        # 前端每次 queue_update 都会用它同步 —— 不会再挂着上一条。
        snap["playing"] = _row(self._inflight[0], True) if self._inflight else None
        # 「已预送、马上播」的那些（零接缝预送窗口）并回 high/low
        for it in list(self._inflight[1:]):
            snap.setdefault("low" if it.source == "script" else "high", []).append(_row(it, False))
        return snap

    async def _emit_playing(self):
        """广播「现在在播哪条」（前端「正在播放」靠它）。

        _inflight[0] 才是当前在播的那条：上一条收到 end 事件后它才轮到。
        没有它就别报 —— 否则前端会一直显示上一条已经播完的内容。
        """
        if not self._inflight:
            # 已经没有在播的内容了 → 必须**显式**告诉前端「清掉正在播放」。
            # 原来这里是直接 return（什么都不发），前端 currentItem 就永远停在最后一条：
            # 明明没弹幕、没语音了，「正在播放」还挂着上一条（用户实测就是这个）。
            # 但只在"从有到无"这个转变时报一次 —— 否则 start() 时也会发一个假事件
            #（自测实测：刚起播就报了一次 playback_ended，此时根本没播过任何东西）。
            if getattr(self, "_had_playing", False):
                self._had_playing = False
                await self._emit({"type": "playback_ended"})
            return
        self._had_playing = True
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
        self._danmaku_buf = []

        # 1. 重新加载最新人设（前端可能刚改过 persona）+ 刷新弹幕回复方式
        if llm is not None and persona:
            llm.persona = persona
        self.apply_persona(persona)
        logger.info(f"[ls] 弹幕回复方式：{self.mode_desc()}"
                    f"（上限 {self._max_chars} 字，策略={'有' if self._policy else '无'}）")

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
        self._tasks = [
            asyncio.create_task(self._pump_watchdog()),
            asyncio.create_task(self._danmaku_aggregator()),
        ]
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
        self._danmaku_buf = []
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
            self._danmaku_buf = []
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
        # 缓冲里的弹幕也要清掉，否则打断完聚合器还会把积压的弹幕回出来
        self._danmaku_buf = []
        return {"code": 0, "msg": "ok", "dropped": n}

    # ── 预送流水线（消除话术之间的空白）──────────────────────────
    async def _do_send(self, item, priority: bool = False):
        """把一条交给 LiveTalking。用 item.id 当 utt，便于撤回未开播的那条。"""
        # 「一句里有快有慢」：一行里出现多次语气标签（例 [快]…[慢]…）→ 拆成多条，
        # 每条只剩一个标签、自带语速，依次送出就能连成一整句（听感是连续的快慢变化）。
        # 拆成多条也正好复用现有的记账：每条各自播完事件，预送/正在播放都不会错乱。

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
            # 语气标签：[快]/[慢]… 由 LLM 判断并写在行首 —— 剥掉再送 TTS（不会被念出来），
            # 语速交给豆包。
            # 最终语速 = **面板上的整体语速** +（语气开关打开时的）标签语速，见 tts_style.effective_rate。
            # 整体语速是对全站的（弹幕回复也走这个出口），标签只影响它自己那一条。
            # 一句只用一个语速、**不切段** —— 切段会有剥离感（上句下句不打杠）。
            _txt, _tag_rate = strip_all_and_rate(item.content or '')
            _rate = effective_rate(_tag_rate)
            # 0 = 原速 → 不传这个参数，和以前的行为逐字节一致
            _style = {"speech_rate": _rate} if _rate else {}
            res = await self.adapter.send_text(_txt, utt=utt, priority=priority, tts=_style)
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
            # 必须带 self.running：stop() 把 running 置 False 后，这里如果还在循环
            # 会继续往会话里送 1~2 条 —— 表现为"点了停止还在说"。
            while self.running and len(self._inflight) <= self._prefetch:
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
                        if item is not None:
                            # ★ 这条是**绕过队列**直接从话术库拿的，所以不会经过
                            #   PlayQueue._expand —— 带「分割符」的话术在这里必须自己摊开：
                            #   第一条现在发，剩下的塞回低优队列（此刻两个队列都是空的，
                            #   所以追加到队尾 == 排在弹幕后面，顺序不会乱）。
                            #
                            #   不摊开的后果（用户实测反馈）：整段话术当成一条发出去，
                            #   于是弹幕插队只能等**整段**播完，而不是等这一句说完 ——
                            #   分割符形同虚设。队列补位那条路是好的，只有这条漏了。
                            spread = PlayQueue._expand(item)
                            item = spread[0]
                            for extra in spread[1:]:
                                await self.queue.put_low(extra)
                            if len(spread) > 1:
                                logger.info(f"[ls] 补位话术已摊成 {len(spread)} 句（先发第 1 句，"
                                            f"其余 {len(spread) - 1} 句已排队 → 弹幕只需等这一句）")
                    if item is None:
                        break
                if item.type == "video":
                    # LiveTalking 侧没有 load_media/convert_custom_media，上游也没实现。
                    # 明确报出来并跳过，别让一条视频话术把补位卡住。
                    logger.warning(f"[ls] 跳过 video 类型话术（LiveTalking 无 load_media 接口）: "
                                   f"{(item.content or '')[:60]}")
                    continue
                try:
                    await self._do_send(item)
                except Exception as e:
                    logger.error(f"[ls] 发送失败: {e}")
                    break

    async def _withdraw_scripts(self):
        """抽回「已预送、**且还没开始合成**」的低优话术，给弹幕腾出紧邻的位置。

        只抽话术、不动已经排上的弹幕/礼物回复，原因：
          · 目的只是别让话术挡住弹幕的位置；
          · TTS 的 priority=True 是 appendleft（后到的排最前），如果连之前的弹幕
            回复也一起抽回来重发，连发弹幕时回复顺序会颠倒。

        ⚠ **只有真撤回成功（tts_dropped > 0）才把它从 _inflight 里摘掉** ——
        这一条是用户实测踩出来的两个 bug 的根因：

          · 撤不掉 = 那条**已经开始合成/马上就要响了**（音频帧已经缓冲在播放队列里）。
            老代码不管成不成功都从 _inflight 摘掉、然后在弹幕后面**重发一遍**，于是：
              ① 正在响的声音被腰斩 →「话术刚播一瞬间被收回，转而播弹幕回复」；
              ② 同一条被发了两次 → 音频和队列显示对不上（错位、觉着播的不是显示的那条）。
          · 撤得掉 = 它还在 TTS 的**待合成**队列里，抽掉就等于从没来过，完全没声音。

        撤不掉的就不动它，让弹幕排在它后面 —— 只多等一小句，但听感是连续的。
        """
        async with self._lock:
            candidates = [it for i, it in enumerate(self._inflight)
                          if i > 0 and it.source == "script"]
        moved = []
        for it in candidates:
            utt = it.metadata.get("utt", "")
            if not utt:
                continue
            dropped = 0
            try:
                res = await self.adapter.drop_queued_talk(utt)
                data = (res or {}).get("data") or {}
                dropped = int(data.get("tts_dropped") or 0)
            except Exception as e:
                logger.warning(f"[ls] 撤回预送话术失败（就当没撤）: {e}")
            if dropped <= 0:
                # 已经开始合成/在播 → 不摘、不重发，让弹幕排在它后面
                continue
            async with self._lock:
                if it in self._inflight:
                    self._inflight.remove(it)
            moved.append(it)
        if candidates and not moved:
            logger.info(f"[ls] 预送话术 {len(candidates)} 条已经开始合成，不撤回（弹幕排在它后面）")
        return moved

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

    # ── 弹幕：入缓冲 → 逐条回 / 攒批合并 → 插队播报 ────────────────
    # 参与回复的消息类型：进场(enter)/分享/下播等**只广播不回复**，与上游 LiveStream 一致。
    # 不挡住的话，抖音/视频号的 "xxx 进入了直播间" 会被当成弹幕丢给 LLM 去回。
    _REPLY_KINDS = ("danmaku", "gift", "follow")

    async def _on_danmaku(self, msg):
        """收到弹幕/礼物/关注：先过安全闸，再广播，再塞进缓冲由聚合器决定怎么回。

        安全闸（_screen）是**确定性**的：屏蔽词 / 注入攻击 / 超长 / 刷屏噪音 / 刷太快
        命中就**不回复**。但**照样广播到运营页**（带 blocked 原因）——
        运营看得见有人在刷屏、也看得见有人在攻击，不会以为消息丢了。
        """
        kind = getattr(msg, "msg_type", "danmaku") or "danmaku"
        sender = getattr(msg, "sender", "") or ""
        content = getattr(msg, "content", "") or ""
        blocked = self._screen(content, sender) if kind == "danmaku" else None
        await self._emit({
            "type": kind,
            "platform": getattr(msg, "platform", ""),
            "sender": sender,
            "content": content,
            "timestamp": getattr(msg, "timestamp", 0),
            "blocked": blocked or "",
        })
        self.danmaku_count += 1

        if blocked:
            logger.info(f"[ls] 弹幕挡下（{blocked}）[{kind}] {sender}: {content[:40]!r}")
            return

        if kind not in self._REPLY_KINDS:
            logger.debug("[ls] %s 事件只广播不回复：%s", kind, sender)
            return

        self._danmaku_buf.append({
            "kind": kind,
            "sender": sender,
            "content": content,
        })
        logger.info(f"[ls] 弹幕入缓冲（{len(self._danmaku_buf)}/{self._batch_trigger}）"
                    f"[{kind}] {sender}: {content[:40]}")

    async def _danmaku_aggregator(self):
        """把缓冲里的弹幕变成播报。

        · 逐条模式（batch_trigger <= 1）：一条一条单独生成回复，不做"值得不值得回"的判定
        · 聚合模式（batch_trigger >= 2）：攒满 N 条立即合并；不足 N 条等满 batch_wait 秒也合并
        两种模式都由 push_priority 插队：当前这句说完，回复紧跟其后。
        """
        batch_start = None            # 当前这批「第一条」弹幕的入缓冲时刻
        while self.running:
            await asyncio.sleep(0.1)
            try:
                if not self._danmaku_buf:
                    batch_start = None
                    continue
                now = time.time()
                if batch_start is None:
                    batch_start = now
                if self._batch_trigger <= 1:
                    batch = self._danmaku_buf[:1]                 # 逐条：一条一条来
                    del self._danmaku_buf[:1]
                else:
                    n = len(self._danmaku_buf)
                    if n >= self._batch_trigger:
                        batch = self._danmaku_buf[:self._batch_trigger]   # 攒满：多的留给下一批
                        del self._danmaku_buf[:self._batch_trigger]
                    elif now - batch_start >= self._batch_wait:
                        batch = list(self._danmaku_buf)           # 超时：1~2 条也回，不让观众干等
                        self._danmaku_buf.clear()
                    else:
                        continue
                batch_start = None
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[ls] 弹幕缓冲读取异常: {e}")
                continue

            try:
                if self._batch_trigger <= 1:
                    await self._reply_single(batch[0])
                else:
                    await self._reply_merged(batch)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[ls] 弹幕回复失败: {e}")

    def _playing_text(self, limit: int = 70) -> str:
        """此刻正在播的那句（剥掉语气标签），给 LLM 当"承上启下"的上文用。

        弹幕回复如果不给上下文，每条都是从零开始的客服式应答 ——
        用户反馈的"很违和、不像真人在介绍"就是这个。尽力而为：拿不到就返回空串。
        """
        try:
            if not self._inflight:
                return ""
            txt, _ = strip_all_and_rate(self._inflight[0].content or "")
            return (txt or "")[:limit]
        except Exception:
            return ""

    def _product_scope(self) -> str:
        """我们自己提供过的商品语料：正在播 / 已预送的**话术**（剥掉语气标签）。

        ⚠ 只收 `script`（商家自己写的话术），**不收弹幕回复** —— 回复是模型生成的，
        拿它当依据等于没依据（编一次以后就永远"有据可查"了）。
        """
        try:
            return "\n".join(
                (strip_all_and_rate(it.content or "")[0] or "")
                for it in (self._inflight or []) if it.source == "script")
        except Exception:
            return ""

    def _reply_scope(self, msg: str = "") -> str:
        """弹幕回复的**商品接地范围**：观众这句 + 参考知识 + 人设 + 正在播的话术。

        回复里出现的商品必须在这个范围里出现过，否则就是模型自己编的
        （实测踩过：卖狗粮、观众只说「你好」，它答「看看我家婴儿车」）。
        """
        try:
            llm_scope = self.llm.last_scope() if self.llm is not None else ""
        except Exception:
            llm_scope = ""
        parts = [llm_scope, msg or "", self._product_scope()]
        return "\n".join(p for p in parts if p)

    async def _reply_single(self, item: dict):
        """逐条模式：一条弹幕 → 一句回复；礼物/关注用固定话术。"""
        kind = item.get("kind") or "danmaku"
        sender = item.get("sender") or "观众"
        content = (item.get("content") or "").strip()
        if kind == "gift":
            await self.push_priority(f"谢谢{sender}的{content}！", source="gift",
                                     metadata={"sender": sender})
            return
        if kind == "follow":
            await self.push_priority(f"欢迎{sender}关注直播间！", source="follow",
                                     metadata={"sender": sender})
            return
        if self.llm is None or not content:
            return
        # 不再传 playing：实测把"正在播什么"喂给模型，它会当成"本店在卖什么"（串品类）。
        # 弹幕回复只依据：观众这句话 + 人设 + 知识库。
        raw, para = await self.llm.generate_reply_ex(
            content, sender,
            want_paraphrase=self._read_msg)      # 要复述才让模型多输出一行转述
        if not raw:
            return
        # 商品接地检查（最后一道闸，确定性）：模型编了一个我们自己资料里根本没有的商品
        # （实测：卖狗粮、观众只发「你好」，它答「看看我家婴儿车」）→ 整条换兜底话术。
        # 白名单 = 观众这句 + 参考知识 + 人设 + 我们自己正在播的话术。
        allowed = self._reply_scope(content)
        bad = ungrounded_mentions(raw, allowed)
        if bad:
            logger.warning(f"[ls] 这条回复里编了我们没提供过的商品 {bad} → 整条换兜底话术。"
                           f"原文：{raw!r}")
            body = reply = self._fallback
        else:
            # ① 输出校验（硬截断 / 重复抑制 / 元词汇丢弃 → 兜底话术）
            body = sanitize_reply(raw, self._max_chars, self._fallback, allowed=allowed)
            # ② 套模板（称呼 + 转述）；名字/转述洗不出来就自动换模板
            reply = self._wrap_reply(body, sender, content, para)
        if not reply:
            return
        # 日志打全，别截断 —— 排查"这句到底提没提到我的问题"时全靠它
        logger.info(f"[ls] 弹幕回复 ← {sender}: {content[:24]!r} → {reply}（{len(reply)} 字）"
                    + ("  [已套模板]" if reply != body else ""))
        await self.push_priority(reply, source="danmaku",
                                 metadata={"sender": sender, "original": content})

    async def _reply_merged(self, batch: list):
        """聚合模式：N 条 → LLM 合并成【一句】；判定全都不值得回应时返回空 → 不回。"""
        if self.llm is None:
            return

        # 只有一条时**不要**走"合并"那套：那个 prompt 又长又要求合并，推理型模型
        # 要烧掉上千 token（实测 4~10 秒），单条根本不需要合并。
        # 直接走轻量的逐条回复（1~2 秒），也正是「等下一条来或超时就单独回一句」的本意。
        if len(batch) == 1:
            await self._reply_single(batch[0])
            return

        merged = await self.llm.generate_merged_reply(
            batch, policy=self._policy, max_chars=self._max_chars)

        # merged is None = 调用失败（多半是推理模型把 token 吃光）→ 降级逐条回，别丢弹幕
        if merged is None:
            logger.warning(f"[ls] 合并回复失败，降级为逐条回复这 {len(batch)} 条")
            for it in batch:
                try:
                    await self._reply_single(it)
                except Exception as e:
                    logger.error(f"[ls] 降级逐条回复失败: {e}")
            return

        if merged:
            # 输出校验 + 套模板。合并模式一批里有好几个人，所以**不加称呼**
            # （allow_name=False → 带 {name} 的模板自动跳过），也不复述某一条原文。
            allowed = self._reply_scope(
                " ".join((b.get("content") or "") for b in batch))
            bad = ungrounded_mentions(merged, allowed)
            if bad:
                logger.warning(f"[ls] 合并回复里编了我们没提供过的商品 {bad} → 换兜底话术。"
                               f"原文：{merged!r}")
                merged = self._fallback
            else:
                body = sanitize_reply(merged, self._max_chars, self._fallback,
                                      allowed=allowed)
                merged = self._wrap_reply(body, "", "", allow_name=False)

        await self._emit({
            "type": "danmaku_batch",
            "count": len(batch),
            "merged": merged,
            "items": [{"kind": b.get("kind"), "sender": b.get("sender"),
                       "content": (b.get("content") or "")[:60]} for b in batch],
        })
        if merged:
            # 打全：包含"这一批到底是哪几条" + "合并后到底说了什么"，避免再出现
            # 「看队列只有一条，怀疑有一条没被提及」这种只能靠猜的情况。
            items_txt = " | ".join((b.get("content") or "")[:20] for b in batch)
            logger.info(f"[ls] 弹幕合并成一句（{len(batch)} 条：{items_txt}）→ {merged}"
                        f"（{len(merged)} 字）")
            await self.push_priority(merged, source="danmaku", metadata={
                "batch_count": len(batch), "merged": True,
                "senders": [b.get("sender") for b in batch],
            })
        else:
            logger.info(f"[ls] 这 {len(batch)} 条判定为无需回应（跳过，不占队列）: "
                        + " | ".join((b.get("content") or "")[:24] for b in batch))
            # 兜底：只要批里有"像样的内容"，就绝不能一条都不回。
            # 实测坑：两条一模一样的弹幕会被 LLM 按「重复内容忽略不提」整批跳过 →
            # 观众看到的是"我明明发了问题，一个字都没回"。prompt 已改，这里再加一道代码级保险。
            # 纯表情 / 纯符号 / 短刷屏（"666"、"啊啊啊"）依旧跳过。
            def _solid(txt) -> int:
                return len(re.sub(r"[\s\W_]+", "", str(txt or "")))

            cand = None
            for b in batch:
                if b.get("kind") in ("gift", "follow"):
                    cand = b
                    break
                if _solid(b.get("content")) >= 4 and _solid(b.get("content")) > _solid(
                        (cand or {}).get("content")):
                    cand = b
            if cand is not None:
                logger.info("[ls] 但批里有实质内容 → 兜底回一条给观众：%s",
                            (cand.get("content") or "")[:24])
                try:
                    await self._reply_single(cand)
                except Exception as e:
                    logger.error(f"[ls] 兜底回复失败: {e}")

    async def ingest(self, msg):
        """外部来源的弹幕塞进来（Windows 侧转发的抖音弹幕走这里）。

        走的路径和采集器回调完全一致：广播事件 → 入缓冲 → 按当前模式回复并插队播报。
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


# 注意：这里**不再有全局 runtime 单例** —— 多房间模式下每个房间在 routes._Room
# 里各自 new 一个 LiveStreamRuntime()，全局单例会让所有房间共用一套队列/预送窗口。
