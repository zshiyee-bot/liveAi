###############################################################################
#  LLM Service — 记忆管理 + 知识库 RAG + 弹幕回复生成
###############################################################################

import os
from typing import AsyncIterator

from openai import AsyncOpenAI
from app.config import load_settings
from app.utils.logger import logger

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
            from app.services.knowledge_base import KnowledgeBase
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
