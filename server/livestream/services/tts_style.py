###############################################################################
#  语气标签 → 豆包 TTS 参数
#
#  LLM 自己判断"这句该怎么念"，在每条话术 / 每条弹幕回复的行首写一个标签：
#      [快] 来，家人们，这款今天真的顶！
#      [慢] 车把是无极变档的，孩子三岁用到十二岁都合适，不用年年换。
#      [平] 这个踏板我用手按了按，挺结实的。
#
#  播放前把标签**剥掉**（绝不会被念出来），并把语速传给豆包（speech_rate）。
#  标签是中文、看得懂，用户在话术列表里也能直接手改。
###############################################################################

import json
import os
import re

# ── 总开关 ────────────────────────────────────────────────────────────
# 实测（2026-09-23）：把一句按标签切成多段、各自语速分别合成，会有"剥离感" ——
# 每段是独立请求，TTS 不知道前文，语调/气息重置，听起来上句下句不打杠。
# 所以默认**关掉**（= 还原成之前的样子：标签只被剥掉、不调语速、也不切段）。
# 想开启：把 data/tts_style.json 里的 enabled 改成 true（或设环境变量 LS_TTS_STYLE=1）。
_SWITCH_PATH = os.path.join("data", "tts_style.json")
_ENABLED = None


def is_enabled() -> bool:
    """语气/语速功能总开关（默认关）。文件改了不用重启也要生效 → 每次读一次（很便宜）。"""
    global _ENABLED
    env = (os.getenv("LS_TTS_STYLE", "") or "").strip().lower()
    if env in ("1", "true", "yes", "on"):
        return True
    if env in ("0", "false", "no", "off"):
        return False
    try:
        with open(_SWITCH_PATH, encoding="utf-8") as f:
            _ENABLED = bool(json.load(f).get("enabled"))
    except Exception:
        if _ENABLED is None:
            _ENABLED = False
    return bool(_ENABLED)


def set_enabled(on: bool):
    """写开关文件（给前端/接口用）。"""
    global _ENABLED
    try:
        os.makedirs(os.path.dirname(_SWITCH_PATH), exist_ok=True)
        with open(_SWITCH_PATH, "w", encoding="utf-8") as f:
            json.dump({"enabled": bool(on)}, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    _ENABLED = bool(on)

# 标签 → 豆包 speech_rate（-50~100，0 = 正常，负数更慢、正数更快）
RATE_MAP = {
    "很快": 35,
    "快": 20,
    "平": 0,
    "正常": 0,
    "慢": -15,
    "很慢": -25,
    # 下面这些是"语气"词，先只映射语速（情绪要挑音色，等实测通过再加）
    "喊": 25,
    "激动": 15,
    "亲和": 5,
    "认真": -5,
    "小声": -10,
}

# 行首的标签：支持 [] 【】 () （） 四种括号，里面 1~6 个字
_TAG_RE = re.compile(r"^\s*[\[【(（]\s*([^\]】)）]{1,6}?)\s*[\]】)）]\s*")


def split_style(text: str) -> tuple[str, dict]:
    """把行首的语气标签剥下来。

    返回 (剥掉标签的正文, {"speech_rate": N})；没标签或标签不认识就原样返回 + {}。
    不认识的标签**不删**（避免误删正文里的方括号）。
    """
    text = text or ""
    m = _TAG_RE.match(text)
    if not m:
        return text, {}
    word = (m.group(1) or "").strip()
    rate = RATE_MAP.get(word)
    if rate is None:
        return text, {}
    body = text[m.end():].strip()
    if not body:                      # 只有标签没有正文 → 别把整条清空
        return text, {}
    return body, {"speech_rate": rate}


def describe(text: str) -> str:
    """给日志用：'[快] 正文…' → '快(+20) 正文…'"""
    body, style = split_style(text)
    if not style:
        return (text or "")[:40]
    return f"[语速{style['speech_rate']:+d}] {(body or '')[:36]}"


# 行内任意位置的标签（一句话里可以出现好几次：先急后缓）
_ANY_TAG_RE = re.compile(r"[\[【(（]\s*([^\]】)）]{1,6}?)\s*[\]】)）]")


def split_style_segments(text: str) -> list[str]:
    """把「一句里有多次语气转折」的文本切成多段，**每段自带它自己的标签**。

    例：'[快]凭啥卖这么贵啊！[慢]咱们这款车把无极变档。'
        → ['[快]凭啥卖这么贵啊！', '[慢]咱们这款车把无极变档。']

    只有 1 个标签（或没有）时原样返回单元素列表 —— 调用方据此决定要不要拆。
    不认识的标签不参与切分（避免把正文里的方括号当标记）。
    """
    text = text or ""
    hits = [(m.start(), m.group(1).strip()) for m in _ANY_TAG_RE.finditer(text)]
    hits = [(pos, w) for pos, w in hits if w in RATE_MAP]
    if len(hits) <= 1:
        return [text]
    out: list[str] = []
    for i, (pos, _w) in enumerate(hits):
        end = hits[i + 1][0] if i + 1 < len(hits) else len(text)
        seg = text[pos:end].strip()
        if seg:
            out.append(seg)
    # 第一个标签之前如果还有正文（罕见），并到第一段前面
    if out and hits[0][0] > 0:
        head = text[:hits[0][0]].strip()
        if head:
            out[0] = f"{head}{out[0]}"
    return out or [text]

def strip_all_and_rate(text: str) -> tuple[str, int | None]:
    """**不切段**的做法：把正文里所有语气标签剥掉，取「第一个标签」的语速作为整句语速。

    为什么不按标签切成多段：实测那样会有明显剥离感（每段独立合成，语调气息重置，
    上句下句不打杠）。所以一句只用一个语速，标签只当"提示"用。
    """
    text = text or ""
    rate = None
    for m in _ANY_TAG_RE.finditer(text):
        w = (m.group(1) or "").strip()
        if w in RATE_MAP and rate is None:
            rate = RATE_MAP[w]
    clean = _ANY_TAG_RE.sub("", text)
    clean = re.sub(r"\s{2,}", " ", clean).strip()
    return (clean or text), rate