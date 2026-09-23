###############################################################################
#  LLM Service — 记忆管理 + 知识库 RAG + 弹幕回复生成
###############################################################################

import os
import random
import re
from typing import AsyncIterator

from openai import AsyncOpenAI
from server.livestream.config import load_settings
from utils.logger import logger

# 标点符号列表 — 用于流式分句
_PUNCTUATION = ",.!;:，。！？：；"

# 语速标签（[快] [慢] 这类）。生成"已用过的话术"清单时要剥掉 ——
# 给模型看的是正文，标签只会干扰它判断"开头是不是重复"。
_STYLE_TAG_RE = re.compile(r"[\[【(（]\s*(?:很快|快|平|慢|很慢|正常)\s*[\]】)）]")

# 开场方式 / 衔接感 —— 新写和改写都要遵守。
# 用户实测反馈：多轮生成时每一轮的开头几乎一样（都是「哎，家人们…」），
# 而且每条都从零开始铺垫，听着像广告片不像真人在直播间说话。
_OPENING_RULES = [
    "【开头必须多样 + 要有衔接感 —— 这是最容易被听出来的假】",
    "· **同一批里不要每条都用同一个开头**：尤其「哎，家人们」「家人们」「宝子们」"
    "这种称呼式开场，整批最多出现 1 次；换个说法、换个切入角度。",
    "· 每条换一种切入姿势（下面只是参考，自己也可以再想）："
    "直接抛问题（「凭啥别家几十块，咱这个要这个价？」）/ 接观众的话（「刚有姐妹在弹幕里问…」）"
    "/ 说现象（「我发现很多人挑的时候只看价格…」）/ 先拦再转（「先别急着下单，我把话说清楚…」）"
    "/ 指细节（「大家看这个位置…」）/ 报数据优惠（「这个规格这个月卖得最多…」）"
    "/ 催单倒计时（「就剩最后几件了啊…」）。",
    "· **每条都要有「接着聊」的感觉**：这些话是插在直播流里说的，不是宣传片的开头。"
    "可以顺手承接（「刚才说到…」「接着上面这个说」「我再补一句」「说到这儿」），"
    "**最忌讳每条都从零铺垫**（「今天给大家介绍一款…」这种一听就假）。",
]

_cfg = load_settings()


# ── 话术查重 ─────────────────────────────────────────────────────────
# 多轮生成时"开头撞车"只靠提示词是管不住的（模型会部分遵守）。
# 用户实测：生成 3 轮，3 轮开头几乎一样 —— 所以在生成端再做一次**确定性**过滤。
def norm_for_dup(text: str) -> str:
    """归一化：剥掉语气标签 + 去掉全部标点/空白。只用于查重，不改原文。"""
    t = _STYLE_TAG_RE.sub("", text or "")
    return re.sub(r"\W+", "", t)          # \w 在 str 下含中文，正好把标点清掉


def _bigrams(s: str) -> set:
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else ({s} if s else set())


def too_similar(a: str, b: str) -> bool:
    """两条话术算不算"撞车"。

    · **开头 6 个字一样** → 撞车（用户最反感的就是这个：每条都「哎，家人们…」）
    · 或者整条高度重合（bigram 覆盖率 ≥ 0.8）
    """
    na, nb = norm_for_dup(a), norm_for_dup(b)
    if not na or not nb:
        return False
    if len(na) >= 6 and len(nb) >= 6 and na[:6] == nb[:6]:
        return True
    ba, bb = _bigrams(na), _bigrams(nb)
    if not ba or not bb:
        return False
    return len(ba & bb) / min(len(ba), len(bb)) >= 0.8


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
        self, message: str, sender: str, kb_context: str = "", playing: str = ""
    ) -> list[dict]:
        """构建发给 LLM 的完整消息列表"""
        system_prompt = self._build_system_prompt(kb_context, playing)
        messages = [{"role": "system", "content": system_prompt}]
        # 添加该用户的对话历史
        messages.extend(self._get_memory(sender))
        # 添加当前消息（标注发送者）
        messages.append({"role": "user", "content": f"[{sender}] {message}"})
        return messages

    def _build_system_prompt(self, kb_context: str = "", playing: str = "") -> str:
        """根据人设构建 system prompt

        playing = 此刻正在播的那句口播（engine 传进来的）。给模型一个"上文"，
        它回复才能承上启下；否则每条弹幕都是从零开始的客服式应答，很违和。
        """
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
            "6. **要像真人在直播里顺口接话，不要像客服机器人**：能接上刚才在讲的内容就接一句"
            "（「说到这个」「你这个问题问得正好」「刚还提到呢」「正好接着说」），"
            "**别每条都从「你好」「谢谢」这种零起点开始**；",
            "7. **不要每条回复都用同一个开头**：换着说，也别每条都喊「家人们」「宝子们」；",
            "8. **不要复述观众的原话**，也不要念观众的账号名。",
        ])

        if playing:
            parts.append(
                f"\n【你此刻正在讲的内容（上一条口播，给你衔接用）】\n{playing}\n"
                "这条弹幕如果和它相关，就顺着它接一句（承上启下，像正在聊天）；"
                "不相关就正常回答，**不要硬扯、也不要复述它**。"
            )

        if kb_context:
            parts.append(f"\n参考知识：\n{kb_context}")

        return "\n".join(parts)

    # ── 弹幕回复生成 ──────────────────────────────────────────────

    async def generate_reply(self, message: str, sender: str, playing: str = "") -> str:
        """为弹幕生成回复（非流式，直接返回完整结果）

        playing = 此刻正在播的那句（来自 engine），用来让回复承上启下。
        """
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

        messages = self._build_messages(message, sender, kb_context, playing)

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
                # 空回复 = 这条弹幕被静默丢掉，必须留痕。
                # 推理型模型（deepseek-flash、*-reasoner 之类）先输出 reasoning_content，
                # 那些 token 同样算进 max_tokens；预算被推理吃光时 content 就是空串。
                # 实测 deepseek-flash + max_tokens=200 → finish_reason=length、content=""，
                # 三次里两次把弹幕吞了。所以这里给足预算，并且空回复打 WARNING。
                logger.warning(
                    "LLM 返回空内容（model=%s finish_reason=%s）—— 这条弹幕不回；"
                    "若频繁出现，请换非推理模型（如 deepseek-chat）",
                    self.llm_model, getattr(choice, 'finish_reason', None))
                return ""

            # 记录到记忆
            self.add_to_memory(sender, "user", message)
            self.add_to_memory(sender, "assistant", reply)

            return reply
        except Exception as e:
            logger.error(f"LLM generate failed: {e}")
            return ""

    # ── 弹幕聚合回复：一批弹幕 → 一句话术（不是逐条回）─────────────

    async def generate_merged_reply(self, batch: list[dict], policy: str = "",
                                    max_chars: int = 60, playing: str = ""):
        """把「一批弹幕/礼物/关注」合并成【一句】自然的主播口播话术。

        batch 元素：{"kind": "danmaku"|"gift"|"follow", "sender": str, "content": str}

        返回三种结果，**必须区分开**：
          · 一句话  → 直接朗读
          · ""      → LLM 明确判定「都不值得回应」（输出 SKIP），跳过就行
          · None    → **调用失败 / 返回空内容**（多半是推理型模型的 token 被推理吃光）
                      —— 调用方必须降级处理，绝不能当成"无需回应"把整批弹幕丢掉
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
            "【重要】每一条提问都必须在那句话里得到回应 —— 可以概括成一句，但绝不能漏掉其中某一条；",
            "礼物和关注必须顺带致谢；",
            "只有纯表情、无意义的刷屏才忽略；两条内容相同的弹幕只提一次，不要因为重复就一条都不回；",
            "【承上启下】如果你此刻正讲到某个话题，就把回应**顺进去**，别像从零开始的客服回答；"
            "但也不许硬扯、不许复述在讲的内容；",
            "不要每次都用同一个开头（「刚有观众问」别连用两次就换个说法："
            "「说到这个」「正好」「来，我统一回一下」…）；",
            "示例：输入「A问在哪」「B问多少钱」→ 输出：刚刚有观众问我们位置和价格，我们是在苏州，价格也不贵，两百块。",
            "如果全都不值得回应（比如全是表情），只输出：SKIP",
            "现在只输出这句话（或 SKIP），不要任何解释、前缀、后缀。",
        ])

        policy = (policy or "").strip()
        if policy:
            parts.append(f"\n主播本人指定的弹幕回复策略（优先遵守）：\n{policy}")
        if playing:
            parts.append(
                f"\n【你此刻正在讲的内容（上一条口播，给你衔接用）】\n{playing}\n"
                "把上面这些回应**顺着它**说出来（承上启下），但不要复述它、也不要硬扯。"
            )
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
                max_tokens=2000,          # 聚合 prompt 长，推理型模型的 reasoning 也算在这里
                temperature=0.8,
            )
            choice = response.choices[0]
            reply = (choice.message.content or "").strip()
            if not reply:
                # 空串 ≠ SKIP 必须区分开：推理型模型（deepseek-flash / reasoner 之类）先输出
                # reasoning_content，一旦推理把 max_tokens 吃光，content 就是空串。
                # 返回 None（失败）让调用方降级成逐条回复，而不是当成"无需回应"丢掉整批弹幕。
                logger.warning(
                    "[ls] LLM 合并回复返回空内容（model=%s, finish_reason=%s, usage=%s）—— "
                    "多半是 max_tokens 被推理过程吃光；交给调用方降级处理",
                    self.llm_model, choice.finish_reason,
                    getattr(getattr(response, "usage", None), "completion_tokens_details", None),
                )
                return None
        except Exception as e:
            logger.error(f"LLM merged reply failed: {e}")
            return None

        # 清洗：合并成一行 / 去引号 / 超长截断
        reply = " ".join(reply.split())
        reply = reply.strip().strip('"').strip("'").strip("“”").strip()
        if not reply or reply.upper().startswith("SKIP"):
            return ""
        limit = max(20, int(max_chars)) + 20
        if len(reply) > limit:
            reply = reply[:limit]
        return reply

    # ── 话术生成（话术管理里的「AI 生成话术」）──────────────────────

    async def generate_scripts(self, requirements: str, count: int = 5,
                               max_chars: int = 0, min_chars: int = 0,
                               with_style: bool = False,
                               avoid: list[str] | None = None):
        """按用户要求生成一批主播口播话术。

        **字数不再固定**：默认每次在 15~45 字之间抽一个随机区间，并要求各条**长短不一** ——
        真人口播本来就不会每条一样长，句式长度机械统一反而容易被平台判成模板话术。
        想强制固定字数时才传 max_chars（min_chars 一起传就按你给的区间）。

        avoid：**已经生成过的话术**（多轮生成时把前几轮的结果传进来）。
        不给它的话，每一轮都是独立请求，模型必然收敛到同一个开场 ——
        用户实测"生成 3 轮，3 轮开头几乎一样"，就是没有这个上下文导致的。

        返回：list[str]（每条一句，已清洗）；调用失败返回 None（调用方好区分）。
        """
        if self.client is None:
            return None
        count = max(1, min(50, int(count or 5)))
        hi = int(max_chars or 0)
        lo = int(min_chars or 0)

        # ── 把"已经生成过的"整理成一段清单，喂给模型 ──
        # 只取前 N 条、每条截断，避免 prompt 被撑爆（多轮生成时 items 会越来越长）
        avoid_block: list[str] = []
        for s in (avoid or []):
            t = _STYLE_TAG_RE.sub("", str(s or ""))
            t = re.sub(r"\s+", " ", t).strip()
            if not t:
                continue
            avoid_block.append(f"  · {t[:44]}")
            if len(avoid_block) >= 12:
                break
        if avoid_block:
            avoid_block = [
                "",
                "【下面这些已经生成过了 —— 这一轮必须避开】",
                *avoid_block,
                "要求：① 内容不要和它们重复（换角度：上次讲价格，这次就讲材质/场景/售后/发货）；"
                "② **开场方式一定要换**：它们用过的开头词（尤其「哎，家人们」这类）这一轮不许再用；"
                "③ 不要只是把它们的词序调一下、换几个字 —— 那还是重复。",
            ]

        # 用户给的"要求"里如果是一整段完整文案（很长、带句号），那是要**改写**而不是让模型另写：
        # 必须按原文的**信息量和长度**改写，不能压缩成摘要、也不能丢卖点。
        # 实测踩过的坑：强制 15~45 字时，用户给 300 字文案会被压成一两句摘要。
        src = (requirements or "").strip()
        src_len = len(re.sub(r"\s+", "", src))
        rewrite = src_len >= 80 and ("。" in src or "！" in src or "！" in src or "?" in src
                                     or "？" in src or "\n" in src)
        if rewrite and not hi:
            # 按原稿长度推区间：8 折 ~ 1.3 倍（允许更精简或更啰嗦一点）
            lo = max(60, int(src_len * 0.8))
            hi = max(lo + 30, int(src_len * 1.3))
        elif hi:
            hi = max(10, min(200, hi))
            lo = max(10, min(hi, lo or max(10, int(hi * 0.55))))   # 只给上限 → 下限按 55% 推
        elif not rewrite:
            lo, hi = 15, 45                                        # 默认：口播自然长度区间
        target = random.randint(lo, hi)          # 每批再抽一个目标值，批次之间也不一样

        p = self.persona
        if rewrite:
            head = [
                f"请把下面这段直播口播文案**改写**成 {count} 个版本（同一件事，换一种说法）。",
                f"原稿 {src_len} 字，改写后的每一版长度要在 {lo}~{hi} 字之间"
                f"（围绕 {target} 字左右）—— **这是改写，不是摘要，不许压缩成一两句**。",
                "【数字人口播铁律 —— 新写和改写都必须遵守】",
            "· 你是数字人主播：**没有身体、没有家人宠物、没有亲身经历**。绝对不要写"
            "「我一脚踩上去试试」「我用手按了按」「我穿过/吃过/用过」「我儿子骑上去不肯下来」"
            "「我家狗子抢着吃」「我昨晚一口气看了六集」这类需要肉身或生活经历的句子；",
            "· 要让人信服，就用**观众能验证的说法**：产品参数/材质/规格/尺寸、优惠力度、"
            "库存发货售后，以及「很多买家反馈」「评论区问得最多的」「大家看这个细节」；",
            "· 内容一切以**用户给的商品和要求**为准，不要自己假设是什么商品、"
            "也不要绑定某个类目（换任何品类都要能用）；",
            "· 直播间口语要有：称呼（家人们/宝子们/姐妹们）、重复强调、语气词（额/哎/就是）、"
            "催单、互动引导（想要的扣个1、问题打在弹幕上）—— 但**绝不编造亲身经历**。",
            "硬性要求：",
                "① **原稿里的每一个信息点、卖点、优惠、数字都必须保留**（一个都不能丢、不能含糊掉；原稿里若有第一人称亲身经历，改写成数字人能说的等价表达：看细节/参数/买家反馈 —— 不要保留、也不要另编），"
                "顺序和说服结构也照原样：先讲痛点/对比 → 再讲卖点 → 最后催单；",
                "② 只换**说法**：句式、用词、开场词、语序、举例方式都要跟原稿不一样，"
                f"而且 {count} 个版本彼此之间也要明显不同（**不许照抄原句，也不许只换几个词**）；",
                "③ **要像真人即兴说话，不要像广告文案、也不要像播音稿**。允许并且欢迎出现："
                "口头禅、语气词、重复、停顿、自我打断或改口（例如"
                "「来，宝子们，家人们，来看看啊，额，这款…」「这个这个，我先说啊」「不是，我是说…」）；"
                "偶尔的咕噜话、多说半句、自己纠正自己，都是**加分项**（真人就是这样）；",
                "④ 每一条都是一段**完整**的话（能单独念给观众听），不要写成提纲、不要写成要点列表；",
                "⑤ 不要编号、不要 emoji、不要引号、不要任何解释说明（不要写「版本一」这类字样）；",
                "⑥ **每一条的行首必须带一个语速标签**，由你自己判断这句该怎么念，"
                "只能从这五个里选：`[很快]` `[快]` `[平]` `[慢]` `[很慢]`。**一句话里可以放 2~3 个标签**（例如前半句质问用 [快]、后半句讲参数用 [慢]），标签插在那半句前面 —— 这样念出来才会有快有慢、像真人；最多 3 个，太多了会一顿一顿。；",
                "⑦ **一行一条**，只输出话术本身（含行首标签）。",
                "",
                "要改写的原稿：",
                src,
                "",
                "⚠️ 再次强调：上面那段是**要改写的原稿**，不是给你参考风格的样例 —— "
                f"请把它完整地改写成 {count} 个不同说法的版本，每个版本都要有它全部的信息量。",
            ]
            parts = [
                f"你是{p.get('name', '小助手')}，一位正在直播的主播。",
                f"性格特点：{p.get('personality', '热情友好')}",
                f"说话风格：{p.get('style', '轻松活泼')}",
                f"知识范围：{p.get('knowledge_scope', '日常闲聊')}",
                "",
                *head,
                *_OPENING_RULES,
                *avoid_block,
            ]
            messages = [
                {"role": "system", "content": "\n".join(parts)},
                {"role": "user", "content": f"请直接给出这 {count} 个改写版本，一行一条。"},
            ]
            return await self._chat_scripts(messages, count, with_style=with_style)

        parts = [
            f"你是{p.get('name', '小助手')}，一位正在直播的主播。",
            f"性格特点：{p.get('personality', '热情友好')}",
            f"说话风格：{p.get('style', '轻松活泼')}",
            f"知识范围：{p.get('knowledge_scope', '日常闲聊')}",
            "",
            f"请生成 {count} 条可以直接朗读的直播口播话术（{count} 只是参考，"
            "如果出现特别短的句子，可以多写一两条，总信息量够就行）。",
            "【数字人口播铁律 —— 新写和改写都必须遵守】",
            "· 你是数字人主播：**没有身体、没有家人宠物、没有亲身经历**。绝对不要写"
            "「我一脚踩上去试试」「我用手按了按」「我穿过/吃过/用过」「我儿子骑上去不肯下来」"
            "「我家狗子抢着吃」「我昨晚一口气看了六集」这类需要肉身或生活经历的句子；",
            "· 要让人信服，就用**观众能验证的说法**：产品参数/材质/规格/尺寸、优惠力度、"
            "库存发货售后，以及「很多买家反馈」「评论区问得最多的」「大家看这个细节」；",
            "· 内容一切以**用户给的商品和要求**为准，不要自己假设是什么商品、"
            "也不要绑定某个类目（换任何品类都要能用）；",
            "· 直播间口语要有：称呼（家人们/宝子们/姐妹们）、重复强调、语气词（额/哎/就是）、"
            "催单、互动引导（想要的扣个1、问题打在弹幕上）—— 但**绝不编造亲身经历**。",
            "硬性要求：",
            "① 每一条都要是**一段完整的话**（能单独念给观众听），但**长度要自然错落**："
            f"短的可以只有几个字（如「来」「哎对了」「家人们」「咱就是说」），"
            f"长的可以到几十字，大致落在 {lo}~{hi} 字之间（这一批可以围绕 {target} 字左右）。"
            "**绝对不要每条都差不多长** —— 真人说话本来就有长有短、有急有缓；",
            "② **要像真人即兴说话，不要像广告文案、也不要像播音稿**。允许并且欢迎出现："
            "口头禅、语气词、重复、停顿、自我打断或改口（例如"
            "「来，宝子们，家人们，来看看啊，额，这款…」「这个这个，我先说啊」「不是，我是说…」）；"
            "偶尔的咕噜话、多说半句、自己纠正自己，都是**加分项**（真人就是这样）；",
            "③ **绝对不要**把一段长话拆成几行、也不要把同一句话拆成几条 —— "
            "那种'一行只是一个短句'的结果是错的，会被退回来；",
            "④ 各条之间话题、句式、开场词、节奏都不要雷同（不要只是换几个字，"
            "也不要每条都用同一个开头）；",
            "⑤ 不要编号、不要 emoji、不要引号、不要任何解释说明；",
            "⑥ **每一条的行首必须带一个语速标签**，由你自己判断这句该怎么念，"
            "只能从这五个里选：`[很快]` `[快]` `[平]` `[慢]` `[很慢]`。**一句话里可以放 2~3 个标签**（例如前半句质问用 [快]、后半句讲参数用 [慢]），标签插在那半句前面 —— 这样念出来才会有快有慢、像真人；最多 3 个，太多了会一顿一顿。"
            "判断依据：喊话/催单/惊喜 → [快]或[很快]；讲参数/讲道理/说细节 → [慢]；"
            "平常闲聊 → [平]。标签写在行首、方括号里，后面紧跟正文，中间不要加空格以外的字符。",
            "⑦ **一行一条**，只输出话术本身（含行首标签）。",
            "",
            "主播本人给出的要求（优先满足）：",
            requirements.strip() or "（未填写特殊要求，围绕上面的人设自由发挥）",
            "",
            "⚠️ 如果上面那段要求里包含了一段**示例话术**：那只是给你参考语气和风格的样例。"
            f"请模仿它的口吻，写出 {count} 条**全新的、每条各自完整**的话术，"
            "**不要把那段样例拆成几段分别返回，也不要只换几个字就当成新的一条**。",
            *_OPENING_RULES,
            *avoid_block,
        ]
        messages = [
            {"role": "system", "content": "\n".join(parts)},
            {"role": "user", "content": f"请直接给出这 {count} 条话术，一行一条。"},
        ]
        return await self._chat_scripts(messages, count, temperature=0.95, with_style=with_style)

    async def _chat_scripts(self, messages: list, count: int, temperature: float = 0.95,
                            with_style: bool = False):
        """调模型 → 逐行清洗成话术列表（改写/新写两条路共用）。失败返回 None。"""
        # 推理模型（deepseek-flash / o1 这类）会把 token 先花在"思考"上，
        # 预算给少了会出现 finish_reason=length 且正文为空（实测 reasoning_tokens=2000 吃满）。
        # 所以：① 预算给大 ② 撞到 length 且正文为空就自动翻倍重试一次 ③ 失败时打印真实原因
        if not with_style:
            # 用户把"语气"关掉了 → 明确告诉模型别写标签（省得白写还得剥）
            messages = list(messages)
            messages.append({"role": "user", "content": "本次不需要语速标签，只输出话术正文，不要出现任何 [ ] 。"})
        budgets = [4000, 8000]
        reply = ""
        last_choice = None
        for attempt, budget in enumerate(budgets, 1):
            try:
                response = await self.client.chat.completions.create(
                    model=self.llm_model,
                    messages=messages,
                    max_tokens=budget,    # 一次多条，留足（推理型模型的 reasoning 也算这里）
                    temperature=temperature,   # 话术要多样，温度调高一点
                )
            except Exception as e:
                logger.error(f"LLM generate scripts failed: {e}")
                return None
            choice = response.choices[0]
            last_choice = choice
            reply = (choice.message.content or "").strip()
            if reply:
                break
            usage = getattr(getattr(response, "usage", None), "completion_tokens_details", None)
            logger.warning(
                "[ls] 生成话术返回空内容（model=%s, finish_reason=%s, usage=%s, max_tokens=%s）",
                self.llm_model, choice.finish_reason, usage, budget)
            if choice.finish_reason != "length" or attempt == len(budgets):
                return None
            logger.warning("[ls] 模型把 token 全用在思考上了 → 把预算翻倍到 %s 再试一次",
                           budgets[attempt])
        if not reply:
            _ = last_choice
            return None

        out: list[str] = []
        _TAG_STRIP = re.compile(r"[\[【(（]\s*(?:很快|快|平|慢|很慢)\s*[\]】)）]\s*")
        for ln in reply.split("\n"):
            # 去掉模型爱加的编号（"1."、"1、"、"1)")、引号、markdown 记号
            ln = re.sub(r"^\s*(?:[-*•]|\d+\s*[.、)．])\s*", "", ln or "").strip()
            ln = ln.strip('"').strip("'").strip("“”").strip("`").strip()
            if not with_style:
                ln = _TAG_STRIP.sub("", ln).strip()   # 关掉语气 → 标签一律不保留
            if len(ln) >= 2:
                out.append(ln)
        if not out:
            return None
        logger.info(f"[ls] AI 生成话术 {len(out)} 条")
        # 段数不卡死：允许比要求多几条（真人说话有碎句，可能多出一两句来）
        return out[:count + 3]

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
            # 合并进 LiveTalking 后包路径是 server.livestream.* —— 原来写的是上游的
            # app.services.*，导致这里永远 ImportError、知识库永远起不来
            # （日志表现为 "No module named 'app.services'; 'app' is not a package"）
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
