###############################################################################
#  PlayQueue — 两级优先级播放队列
#
#  规则:
#    - put_high(item): 追加到高优队列右端（弹幕/礼物/关注 → 同优先级 FIFO）
#    - put_low(item):  追加到低优队列右端（话术补位 → 同优先级 FIFO）
#    - get_next():     优先从高优取，高优空则取低优
#    - auto_fill:      低优队列 < min_size 时自动从话术库补位
###############################################################################

import asyncio
import collections
import uuid
from dataclasses import dataclass, field
from typing import Optional

from utils.logger import logger


@dataclass
class QueueItem:
    """播放队列项"""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    type: str = "text"       # "text" | "audio"
    content: str = ""        # 文字内容 或 音频路径
    source: str = "script"   # "script" | "danmaku" | "gift" | "follow"
    level: str = "low"       # "high" | "low"
    metadata: dict = field(default_factory=dict)  # {sender, original_message, script_id, ...}
    # 分割后的多句（话术管理里配的「分割符」）：入队时会被摊平成多条队列项，
    # 于是播放是"一句一句"的 —— 弹幕插队只要等当前这一小句，不用等整段长话术。
    parts: list = field(default_factory=list)


class PlayQueue:
    """线程安全的双 deque 优先播放队列"""

    def __init__(self, script_manager=None):
        self._high = collections.deque()
        self._low = collections.deque()
        self._lock = asyncio.Lock()
        self._script_manager = script_manager
        self._auto_fill_task: Optional[asyncio.Task] = None
        self._min_size = 2
        self._fill_interval = 3.0
        self._running = False
        self._on_change: Optional[callable] = None  # 队列变更回调

    def set_on_change(self, callback):
        """设置回调：每次入队/出队后自动调用 callback()"""
        self._on_change = callback

    async def _notify_change(self):
        if self._on_change:
            try:
                result = self._on_change()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                pass

    # ── 入队 ──────────────────────────────────────────────────────

    @staticmethod
    def _expand(item: QueueItem) -> list:
        """把「一条含多句的话术」摊平成多条队列项（分割符功能）。

        只在入队时展开，DB 里那条话术仍然是 1 行 → 话术管理列表显示 1 条；
        播放队列里变成 N 句，一句播完再取下一句。
        """
        parts = [p for p in (getattr(item, "parts", None) or []) if p and p.strip()]
        if len(parts) <= 1:
            item.parts = []
            return [item]
        total = len(parts)
        out = []
        for i, p in enumerate(parts):
            out.append(QueueItem(
                # 第 1 句沿用原 id（前端/撤回都认得它），其余各给新 id，方便各自撤回
                id=item.id if i == 0 else uuid.uuid4().hex[:12],
                type=item.type, content=p, source=item.source, level=item.level,
                metadata={**(item.metadata or {}), "split_index": i + 1, "split_total": total},
            ))
        return out

    async def put_high(self, item: QueueItem):
        """高优先级入队（追加到高优队尾，同优先级 FIFO）"""
        item.level = "high"
        async with self._lock:
            self._high.extend(self._expand(item))
        logger.info(f"Queue put_high: [{item.source}] {item.content[:50]}... (high={len(self._high)}, low={len(self._low)})")
        await self._notify_change()

    async def put_low(self, item: QueueItem):
        """低优先级入队（追加到低优队尾，同优先级 FIFO）"""
        item.level = "low"
        async with self._lock:
            self._low.extend(self._expand(item))
        logger.info(f"Queue put_low: [{item.source}] {item.content[:50]}... (high={len(self._high)}, low={len(self._low)})")
        await self._notify_change()

    # ── 出队 ──────────────────────────────────────────────────────

    async def get_next(self) -> Optional[QueueItem]:
        """出队：优先高优，再低优（各自 FIFO）"""
        item = None
        async with self._lock:
            if self._high:
                item = self._high.popleft()
                logger.info(f"Queue pop from HIGH: [{item.source}] (high={len(self._high)}, low={len(self._low)})")
            elif self._low:
                item = self._low.popleft()
                logger.info(f"Queue pop from LOW: [{item.source}] (high={len(self._high)}, low={len(self._low)})")
        if item:
            await self._notify_change()
        return item

    # ── 队列信息 ──────────────────────────────────────────────────

    @property
    def high_length(self) -> int:
        return len(self._high)

    @property
    def low_length(self) -> int:
        return len(self._low)

    @property
    def total_length(self) -> int:
        return len(self._high) + len(self._low)

    async def snapshot(self) -> dict:
        """返回队列当前快照"""
        async with self._lock:
            return {
                "high": [{"id": i.id, "type": i.type, "source": i.source,
                          "content_preview": i.content[:80], "level": i.level}
                         for i in self._high],
                "low": [{"id": i.id, "type": i.type, "source": i.source,
                         "content_preview": i.content[:80], "level": i.level}
                        for i in self._low],
            }

    async def clear(self):
        """清空队列"""
        async with self._lock:
            self._high.clear()
            self._low.clear()

    # ── 自动补位 ──────────────────────────────────────────────────

    async def start_auto_fill(self, min_size: int = 2, interval: float = 3.0):
        """启动自动补位后台任务"""
        self._min_size = min_size
        self._fill_interval = interval
        self._running = True
        self._auto_fill_task = asyncio.create_task(self._auto_fill_loop())
        logger.info(f"Auto-fill started: min_size={min_size}, interval={interval}s")

    async def stop_auto_fill(self):
        """停止自动补位"""
        self._running = False
        if self._auto_fill_task:
            self._auto_fill_task.cancel()
            try:
                await self._auto_fill_task
            except asyncio.CancelledError:
                pass
            self._auto_fill_task = None
        # AI 循环话术：把还在后台续写的生成任务也掐掉，别在停播后继续烧 LLM 额度
        sm = self._script_manager
        if sm is not None and hasattr(sm, "cancel_loop_tasks"):
            try:
                sm.cancel_loop_tasks()
            except Exception:
                pass
        logger.info("Auto-fill stopped")

    async def _auto_fill_loop(self):
        """后台循环：低优队列不足时自动从话术库补位"""
        while self._running:
            try:
                if self.low_length < self._min_size and self._script_manager:
                    script_item = await self._script_manager.random_pick()
                    if script_item:
                        await self.put_low(script_item)
            except Exception as e:
                logger.warning(f"Auto-fill error: {e}")
            await asyncio.sleep(self._fill_interval)

    def set_script_manager(self, script_manager):
        """设置话术管理器（延迟注入）"""
        self._script_manager = script_manager
