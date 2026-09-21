###############################################################################
#  LLM Service — 记忆管理 + 知识库 RAG + 弹幕回复生成
###############################################################################

import os
from typing import AsyncIterator

from openai import AsyncOpenAI
from server.livestream.config import load_settings
from utils.logger import logger

# 标点符号列表 — 用于流式分句
_PUNCTUATION = ",.!;:，。！？：；"

_cfg = load_settings()


class LLMService:
    """LLM 服务：人设驱动 + 短期记忆 + 知识库增强"""

    def __init__(self, persona: dict, llm_api_key: str = "", llm_base_url: str = "",
                 llm_model: str = "", embedding_api_key: str = "",
                 embedding_base_url: str = "", embedding_model: str = ""):
        self.persona = persona
        self.llm_api_key = llm_api_key or os.getenv("OPENAI_API_KEY", "")
        self.llm_base_url = llm_base_url
        self.llm_model = llm_model
        self.embedding_api_key = embedding_api_key or os.getenv("EMBEDDING_API_KEY", "")
        self.embedding_base_url = embedding_base_url
        self.embedding_model = embedding_model

        if self.llm_api_key:
            self.client = AsyncOpenAI(
                api_key=self.llm_api_key,
                base_url=self.llm_base_url,
            )
        else:
            self.client = None
            logger.warning("LLM API key not configured — danmaku replies will be empty")

        # 短期记忆
        self._memory: dict[str, list[dict]] = {}
        self._memory_window = _cfg.memory_window_size
        # 知识库（延迟初始化）
        self._knowledge_base = None

    # ── 记忆管理 ──────────────────────────────────────────────────

    def _get_memory(self, user_id: str) -> list[dict]:
        """获取指定用户的对话历史"""
        if user_id not in self._memory:
            self._memory[user_id] = []
        return self._memory[user_id]

    def add_to_memory(self, user_id: str, role: str, content: str):
        """添加一条对话到记忆"""
        mem = self._get_memory(user_id)
        mem.append({"role": role, "content": content})
        # 保持窗口大小
        while len(mem) > self._memory_window * 2:  # user+assistant pairs
            mem.pop(0)

    def _build_messages(
        self, message: str, sender: str, kb_context: str = ""
    ) -> list[dict]:
        """构建发给 LLM 的完整消息列表"""
        system_prompt = self._build_system_prompt(kb_context)
        messages = [{"role": "system", "content": system_prompt}]
        # 添加该用户的对话历史
        messages.extend(self._get_memory(sender))
        # 添加当前消息（标注发送者）
        messages.append({"role": "user", "content": f"[{sender}] {message}"})
        return messages

    def _build_system_prompt(self, kb_context: str = "") -> str:
        """根据人设构建 system prompt"""
        p = self.persona
        parts = [
            f"你是{p.get('name', '小助手')}，一位正在直播的主播。",
            f"性格特点：{p.get('personality', '热情友好')}",
            f"说话风格：{p.get('style', '轻松活泼')}",
            f"知识范围：{p.get('knowledge_scope', '日常闲聊')}",
        ]
        forbidden = p.get("forbidden_topics", [])
        if forbidden:
            parts.append(f"禁止谈论以下话题：{'、'.join(forbidden)}")

        parts.extend([
            "",
            "直播间有观众发弹幕时，你需要：",
            "1. 如果弹幕是提问，用口语化的方式简短回答",
            "2. 如果弹幕是闲聊，用风趣幽默的方式互动",
            "3. 回复控制在1-2句话，适合TTS语音播报",
            "4. 不要使用表情符号和特殊字符",
            "5. 直接输出回复内容，不要加任何前缀或修饰",
        ])

        if kb_context:
            parts.append(f"\n参考知识：\n{kb_context}")

        return "\n".join(parts)

    # ── 弹幕回复生成 ──────────────────────────────────────────────

    async def generate_reply(self, message: str, sender: str) -> str:
        """为弹幕生成回复（非流式，直接返回完整结果）"""
        if self.client is None:
            return ""

        # 检索知识库
        kb_context = ""
        if self._knowledge_base:
            try:
                docs = await self._knowledge_base.search(message, k=2)
                if docs:
                    kb_context = "\n".join(d.page_content for d in docs)
            except Exception as e:
                logger.warning(f"Knowledge base search failed: {e}")

        messages = self._build_messages(message, sender, kb_context)

        try:
            response = await self.client.chat.completions.create(
                model=self.llm_model,
                messages=messages,
                max_tokens=200,
                temperature=0.8,
            )
            reply = response.choices[0].message.content or ""
            reply = reply.strip()

            # 记录到记忆
            self.add_to_memory(sender, "user", message)
            self.add_to_memory(sender, "assistant", reply)

            return reply
        except Exception as e:
            logger.error(f"LLM generate failed: {e}")
            return ""

    # ── 弹幕聚合回复：一批弹幕 → 一句话术（不是逐条回）─────────────

    async def generate_merged_reply(self, batch: list[dict], policy: str = "",
                                    max_chars: int = 60) -> str:
        """把「一批弹幕/礼物/关注」合并成【一句】自然的主播口播话术。

        batch 元素：{"kind": "danmaku"|"gift"|"follow", "sender": str, "content": str}
        返回：一句话（可直接朗读）；一条都不值得回应时返回 ""（LLM 输出 SKIP）。

        示例：输入 A「在哪」B「多少钱」→ 输出
        「刚刚有观众问我们位置和价格，我们是在苏州，价格也不贵，两百块」
        """
        if self.client is None or not batch:
            return ""

        p = self.persona
        _kind_cn = {"danmaku": "弹幕", "gift": "礼物", "follow": "关注"}
        lines = []
        for i, it in enumerate(batch, 1):
            kind = _kind_cn.get(it.get("kind"), "弹幕")
            who = it.get("sender") or "观众"
            what = (it.get("content") or "").strip()
            lines.append(f"[{i}] {kind} {who}：{what}")
        batch_text = "\n".join(lines)

        # ── 知识库检索：**逐条弹幕**单独查，再合并去重 ──
        # 为什么不能把整批拼成一个 query：一批里 3 条弹幕可能问完全不同的事
        # （位置 / 价格 / 尺码），拼起来只会命中其中一条，另外两条就丢了事实依据。
        # 礼物、关注不含问题 → 不检索；条数上限 8，避免一次弹幕风暴打出太多 embedding 调用。
        kb_docs: list = []
        kb_seen: set = set()
        kb_queries: list = []
        if self._knowledge_base:
            kb_queries = [(it.get("content") or "").strip() for it in batch
                          if it.get("kind") == "danmaku"]
            kb_queries = [q for q in kb_queries if len(q) >= 2][:8]
            for q in kb_queries:
                try:
                    docs = await self._knowledge_base.search(q, k=2)
                except Exception as e:
                    logger.warning(f"Knowledge base search failed for query={q[:20]!r}: {e}")
                    continue
                for d in (docs or []):
                    key = (getattr(d, "page_content", "") or "").strip()
                    if not key or key in kb_seen:
                        continue
                    kb_seen.add(key)
                    kb_docs.append(d)
            if kb_docs:
                logger.info(f"KB 逐条检索: {len(kb_queries)} 条问题 → 去重后 {len(kb_docs)} 段"
                            f"（取前 {min(len(kb_docs), 6)} 段进 prompt）")

        # 合并后总量上限 6 段（避免 prompt 太长、也避免不同问题的知识互相干扰）
        kb_context = "\n".join((d.page_content or "").strip() for d in kb_docs[:6])

        parts = [
            f"你是{p.get('name', '小助手')}，一位正在直播的主播。",
            f"性格特点：{p.get('personality', '热情友好')}",
            f"说话风格：{p.get('style', '轻松活泼')}",
            f"知识范围：{p.get('knowledge_scope', '日常闲聊')}",
        ]
        forbidden = p.get("forbidden_topics", [])
        if forbidden:
            parts.append(f"禁止谈论以下话题：{'、'.join(forbidden)}")

        parts.extend([
            "",
            "下面是刚刚这一小段时间里观众发的内容（按时间排序）：",
            batch_text,
            "",
            "请把其中【值得回应】的内容，合并成【一句】自然的主播口播话术 —— 不是逐条回答。",
            f"要求：只能是一句话；不超过 {max_chars} 个字；口语化、可直接朗读；",
            "不要编号、不要换行、不要 emoji、不要 markdown、不要引号、不要念观众账号名；",
            "礼物和关注必须顺带致谢；刷屏、无意义、纯表情、重复内容直接忽略不提；",
            "示例：输入「A问在哪」「B问多少钱」→ 输出：刚刚有观众问我们位置和价格，我们是在苏州，价格也不贵，两百块。",
            "如果全都不值得回应，只输出：SKIP",
            "现在只输出这句话（或 SKIP），不要任何解释、前缀、后缀。",
        ])

        policy = (policy or "").strip()
        if policy:
            parts.append(f"\n主播本人指定的弹幕回复策略（优先遵守）：\n{policy}")
        if kb_context:
            parts.append(f"\n参考知识（按上面观众问题的顺序检索而来；只用它回答事实性问题，"
                         f"不要照读原文，也不要把 A 问题的答案安到 B 问题上）：\n{kb_context}")

        messages = [
            {"role": "system", "content": "\n".join(parts)},
            {"role": "user", "content": "请直接给出这一句话。"},
        ]

        try:
            response = await self.client.chat.completions.create(
                model=self.llm_model,
                messages=messages,
                max_tokens=800,
                temperature=0.8,
            )
            choice = response.choices[0]
            reply = (choice.message.content or "").strip()
            if not reply:
                # 空串 ≠ SKIP，必须区分开：推理型模型（deepseek-flash / reasoner 之类）先输出
                # reasoning_content，一旦推理把 max_tokens 吃光，content 就是空串。
                # 之前这种情况被静默当成「无需回应」，日志里完全看不出来，白丢弹幕。
                logger.warning(
                    "[ls] LLM 返回空内容（model=%s, finish_reason=%s, usage=%s）—— "
                    "通常是 max_tokens 被推理过程吃光、或模型名/额度有问题；本条按「无需回应」处理",
                    self.llm_model, choice.finish_reason,
                    getattr(getattr(response, "usage", None), "completion_tokens_details", None),
                )
                return ""
        except Exception as e:
            logger.error(f"LLM merged reply failed: {e}")
            return ""

        # 清洗：合并成一行 / 去引号 / 超长截断
        reply = " ".join(reply.split())
        reply = reply.strip().strip('"').strip("'").strip("“”").strip()
        if not reply or reply.upper().startswith("SKIP"):
            return ""
        limit = max(20, int(max_chars)) + 20
        if len(reply) > limit:
            reply = reply[:limit]
        return reply

    async def generate_reply_stream(self, message: str, sender: str) -> AsyncIterator[str]:
        """流式生成弹幕回复，按标点分句 yield"""
        if self.client is None:
            yield ""
            return

        kb_context = ""
        if self._knowledge_base:
            try:
                docs = await self._knowledge_base.search(message, k=2)
                if docs:
                    kb_context = "\n".join(d.page_content for d in docs)
            except Exception:
                pass

        messages = self._build_messages(message, sender, kb_context)

        try:
            stream = await self.client.chat.completions.create(
                model=self.llm_model,
                messages=messages,
                max_tokens=200,
                temperature=0.8,
                stream=True,
            )

            buffer = ""
            full_reply = ""
            async for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta.content
                if delta is None:
                    continue

                for ch in delta:
                    buffer += ch
                    if ch in _PUNCTUATION and len(buffer) > 5:
                        sentence = buffer.strip()
                        if sentence:
                            yield sentence
                            full_reply += sentence
                        buffer = ""

            # 剩余内容
            if buffer.strip():
                yield buffer.strip()
                full_reply += buffer.strip()

            # 记录到记忆
            if full_reply:
                self.add_to_memory(sender, "user", message)
                self.add_to_memory(sender, "assistant", full_reply)

        except Exception as e:
            logger.error(f"LLM streaming failed: {e}")
            yield ""

    # ── 知识库 ────────────────────────────────────────────────────

    async def init_knowledge_base(self):
        """初始化知识库（从数据库加载文档并构建向量索引）"""
        try:
            from server.livestream.services.knowledge_base import KnowledgeBase
            self._knowledge_base = KnowledgeBase(
                docs_path=_cfg.knowledge_docs_path,
                persist_path=_cfg.chroma_persist_path,
                embedding_model=self.embedding_model,
                api_key=self.embedding_api_key,
                base_url=self.embedding_base_url,
            )
            await self._knowledge_base.initialize()
            logger.info("Knowledge base initialized")
        except Exception as e:
            logger.warning(f"Knowledge base init failed (will operate without KB): {e}")
            self._knowledge_base = None

    async def rebuild_knowledge_base(self):
        """重建知识库索引"""
        if self._knowledge_base:
            await self._knowledge_base.rebuild()
            logger.info("Knowledge base rebuilt")
