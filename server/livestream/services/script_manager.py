###############################################################################
#  ScriptManager — 话术随机选取
###############################################################################

import random
import re
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from server.livestream.models import Script
from server.livestream.services.play_queue import QueueItem
from utils.logger import logger

# 切出来的片段短于这个长度就丢掉（避免只切出一个"。"）
_SPLIT_MIN_LEN = 2


def split_script_text(text: str, sep: str = "") -> list[str]:
    """把一条话术按「分割符」切成多句（话术管理里的分割功能）。

    · 先按换行切：AI 一次生成多条话术时是用换行拼起来的
    · 再按 sep 切；**分隔符保留在句尾**（"你好。" 这样 TTS 停顿更自然）
    · sep 为空 → 只按换行切；切不出多句就返回单元素列表
    """
    text = (text or "").strip()
    if not text:
        return []
    chunks = [c.strip() for c in re.split(r"[\r\n]+", text) if c.strip()]
    sep = (sep or "").strip()
    if not sep:
        return chunks or [text]

    out: list[str] = []
    for ch in chunks:
        parts = ch.split(sep)
        for i, p in enumerate(parts):
            p = p.strip()
            if not p:
                continue
            if i < len(parts) - 1:
                p = p + sep          # 句尾补回分隔符
            if len(p) >= _SPLIT_MIN_LEN:
                out.append(p)
    return out or [text]


class ScriptManager:
    """话术库管理 + 随机选取（避免短期重复）"""

    def __init__(self, db_session_factory):
        self._db_factory = db_session_factory
        self._recent_picks: list[int] = []
        self._max_recent = 5

    async def random_pick(self) -> QueueItem | None:
        async with self._db_factory() as session:
            stmt = select(Script).where(Script.enabled == True).order_by(Script.play_count.asc())
            result = await session.execute(stmt)
            scripts = result.scalars().all()

            if not scripts:
                logger.warning("No active scripts available for auto-fill")
                return None

            candidates = [s for s in scripts if s.id not in self._recent_picks]
            if not candidates:
                candidates = scripts
                self._recent_picks.clear()

            total_plays = sum(s.play_count + 1 for s in candidates)
            if total_plays == 0:
                weights = [1] * len(candidates)
            else:
                max_play = max(s.play_count for s in candidates) + 2
                weights = [max_play - s.play_count for s in candidates]

            chosen = random.choices(candidates, weights=weights, k=1)[0]
            chosen.play_count += 1
            chosen.last_used_at = datetime.now()
            await session.commit()

            self._recent_picks.append(chosen.id)
            if len(self._recent_picks) > self._max_recent:
                self._recent_picks.pop(0)

            content = chosen.content if chosen.type == "text" else (chosen.file_path or "")
            # 分割符：在入队前切好，写进 item.parts（PlayQueue 会把它们摊成多条，
            # 于是播放是"一句一句"的，弹幕插队只需等当前这一小句）
            parts: list = []
            if chosen.type == "text" and content:
                parts = split_script_text(content, getattr(chosen, "split_sep", "") or "")
                if len(parts) < 2:
                    parts = []          # 根本切不出多句 → 不当分割话术
            item = QueueItem(
                type=chosen.type,
                content=content,
                source="script",
                level="low",
                metadata={"script_id": chosen.id, "title": chosen.title},
                parts=parts,
            )
            if parts:
                logger.info(f"Auto-fill picked script: [{chosen.id}] {chosen.title}"
                            f"（按「{chosen.split_sep}」切成 {len(parts)} 句，逐句播）")
            else:
                logger.info(f"Auto-fill picked script: [{chosen.id}] {chosen.title}")
            return item
