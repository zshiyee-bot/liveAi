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

import re

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
