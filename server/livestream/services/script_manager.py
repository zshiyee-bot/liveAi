###############################################################################
#  ScriptManager — 话术随机选取
#
#  · 普通话术：随机挑一条；配了「分割符」的先在入队前切成多句（逐句播）
#  · AI 循环话术（ai_loop）：一次只取一句，缓冲快用完就后台再生成一段，
#    永不停歇 —— 话术不会长期固定，也就不会被平台按"重复话术"检测
###############################################################################

import asyncio
import json
import random
import re
import time
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from server.livestream.models import Script
from server.livestream.services.play_queue import QueueItem
from utils.logger import logger

# 切出来的片段短于这个长度就丢掉（避免只切出一个"。"）
_SPLIT_MIN_LEN = 2

# 循环话术：缓冲剩这么多句就提前生成下一段（不打断正在播的，纯后台）
_LOOP_PREFETCH_AT = 3
# 死循环保护：记住最近用过的这么多句，生成时尽量避开（避免换汤不换药）
_LOOP_SEEN_MAX = 300
# 生成失败后的冷却秒数（key 没配/模型报错时别疯狂重试刷日志）
_LOOP_FAIL_COOLDOWN = 60.0


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
    """话术库管理 + 随机选取（避免短期重复）；AI 循环话术的续写也在这里"""

    def __init__(self, db_session_factory, llm=None):
        self._db_factory = db_session_factory
        self._llm = llm
        self._recent_picks: list[int] = []
        self._max_recent = 5
        # script_id -> {"buffer": [...未播], "seen": [...], "batch_no": int,
        #               "generated": int, "task": Task|None, "fail_until": float}
        self._loop_state: dict[int, dict] = {}

    def set_llm(self, llm):
        """延迟注入 LLM（rebuild_deps 里调用；没有它循环话术就只能用存货）"""
        self._llm = llm

    # ── 选取 ──────────────────────────────────────────────────────────

    async def random_pick(self) -> QueueItem | None:
        async with self._db_factory() as session:
            stmt = select(Script).where(Script.enabled == True).order_by(Script.play_count.asc())
            result = await session.execute(stmt)
            scripts = result.scalars().all()

            if not scripts:
                # 话术库为空时别刷屏：这个 warning 以前被快速循环打到日志爆炸，
                # 把真正有用的信息全冲掉。顺便把"该怎么解决"写在提示里。
                now = time.time()
                if now - getattr(self, "_empty_warn_at", 0.0) > 60:
                    self._empty_warn_at = now
                    logger.warning("No active scripts available for auto-fill"
                                   "（话术库是空的：播放队列里只会有弹幕回复，"
                                   "去「话术管理」加一条就恢复正常）")
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
            self._recent_picks.append(chosen.id)
            if len(self._recent_picks) > self._max_recent:
                self._recent_picks.pop(0)

            # ── AI 循环话术：每句现取，缓冲快空就后台续写 ──────────────
            loop_cfg = chosen.ai_loop_cfg if chosen.type == "text" else {}
            if loop_cfg.get("enabled"):
                content = await self._next_loop_part(chosen, loop_cfg)
                if not content:
                    # 还没生成出来（key 没配/模型失败）→ 这次不选它，下次再说
                    return None
                chosen.play_count += 1
                chosen.last_used_at = datetime.now()
                state = self._loop_state.get(chosen.id) or {}
                loop_cfg = dict(loop_cfg)
                loop_cfg["buffer"] = list(state.get("buffer") or [])
                loop_cfg["seen"] = list(state.get("seen") or [])[-_LOOP_SEEN_MAX:]
                loop_cfg["batch_no"] = int(state.get("batch_no") or 0)
                loop_cfg["generated"] = int(state.get("generated") or 0)
                chosen.ai_loop = json.dumps(loop_cfg, ensure_ascii=False)
                await session.commit()
                logger.info(f"Auto-fill picked AI 循环话术: [{chosen.id}] {chosen.title}"
                            f"（剩 {len(state.get('buffer') or [])} 句，已生成 {loop_cfg['generated']} 句）")
                return QueueItem(
                    type="text",
                    content=content,
                    source="script",
                    level="low",
                    metadata={"script_id": chosen.id, "title": chosen.title,
                              "ai_loop": True, "batch_no": loop_cfg["batch_no"]},
                )

            # ── 普通话术 ──────────────────────────────────────────────
            chosen.play_count += 1
            chosen.last_used_at = datetime.now()
            await session.commit()

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

    # ── AI 循环话术 ───────────────────────────────────────────────────

    def _state_of(self, script, cfg: dict) -> dict:
        """拿（或按 DB 里的配置初始化）这条循环话术的运行时状态。"""
        state = self._loop_state.get(script.id)
        if state is None:
            state = {
                "buffer": [s for s in (cfg.get("buffer") or []) if s and s.strip()],
                "seen": [s for s in (cfg.get("seen") or []) if s],
                "batch_no": int(cfg.get("batch_no") or 0),
                "generated": int(cfg.get("generated") or 0),
                "task": None,
                "fail_until": 0.0,
            }
            self._loop_state[script.id] = state
        return state

    async def _next_loop_part(self, script, cfg: dict) -> str | None:
        """取下一句；缓冲空了就现等一段，快空了就后台先备好。"""
        state = self._state_of(script, cfg)
        if not state["buffer"]:
            task = self._kick_refill(script, state, cfg, wait=True)
            if task is not None:
                try:
                    await task
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(f"[ls] AI 循环话术续写异常: {e}")
        if not state["buffer"]:
            return None
        content = state["buffer"].pop(0)
        if len(state["buffer"]) <= _LOOP_PREFETCH_AT:
            self._kick_refill(script, state, cfg, wait=False)
        return content

    def _kick_refill(self, script, state: dict, cfg: dict, wait: bool = False):
        """起一个后台任务生成下一段；wait=True 时当场等它出结果。"""
        task = state.get("task")
        if task is not None and not task.done():
            return task          # 已经有人在生成了，别重复调
        if not wait and time.time() < (state.get("fail_until") or 0):
            return None          # 刚失败过，冷却中
        snapshot = {
            "requirements": cfg.get("requirements") or "",
            "per_segment": int(cfg.get("per_segment") or 10),
            "max_chars": int(cfg.get("max_chars") or 30),
            "split_sep": cfg.get("split_sep") or "",
        }
        task = asyncio.create_task(self._refill_task(script.id, state, snapshot))
        state["task"] = task
        return task

    async def _refill_task(self, script_id: int, state: dict, cfg: dict):
        """后台生成一段循环话术，结果塞进 state["buffer"]。失败只记日志、不抛。"""
        req = cfg.get("requirements") or ""
        count = max(1, min(50, int(cfg.get("per_segment") or 10)))
        max_chars = max(10, min(200, int(cfg.get("max_chars") or 30)))
        if self._llm is None or getattr(self._llm, "client", None) is None:
            state["fail_until"] = time.time() + _LOOP_FAIL_COOLDOWN
            logger.warning("[ls] AI 循环话术需要 LLM：请先在「系统配置」里填好 API Key 并保存重载")
            return
        try:
            got = await self._llm.generate_scripts(req, count=count, max_chars=max_chars)
        except Exception as e:
            state["fail_until"] = time.time() + _LOOP_FAIL_COOLDOWN
            logger.warning(f"[ls] AI 循环话术生成失败: {e}")
            return
        if not got:
            state["fail_until"] = time.time() + _LOOP_FAIL_COOLDOWN
            logger.warning("[ls] AI 循环话术没生成出内容（看 LLM 日志：key/模型名？）")
            return

        # 分割符：新生成的每句也按分割符再拆细（和创建时同一套逻辑）
        sep = cfg.get("split_sep") or ""
        flat: list[str] = []
        for g in got:
            flat.extend(split_script_text(g, sep) or [g])
        if not flat:
            state["fail_until"] = time.time() + _LOOP_FAIL_COOLDOWN
            logger.warning("[ls] AI 循环话术续写被分割符切没了，跳过这一段")
            return

        seen = set(state.get("seen") or [])
        fresh = [g for g in flat if g not in seen]
        if not fresh:
            fresh = list(flat)       # 全撞车了 → 至少给点，不然永远空
        state.setdefault("buffer", []).extend(fresh)
        state["seen"] = (list(state.get("seen") or []) + fresh)[-_LOOP_SEEN_MAX:]
        state["batch_no"] = int(state.get("batch_no") or 0) + 1
        state["generated"] = int(state.get("generated") or 0) + len(fresh)
        state["fail_until"] = 0.0
        logger.info(f"[ls] AI 循环话术续写第 {state['batch_no']} 段：{len(fresh)} 句"
                    f"（缓冲共 {len(state['buffer'])} 句，累计 {state['generated']} 句）")

    def cancel_loop_tasks(self):
        """停止直播时把还在跑的续写任务掐掉（否则白花钱调 LLM）。"""
        for state in self._loop_state.values():
            task = state.get("task")
            if task is not None and not task.done():
                task.cancel()
            state["task"] = None
