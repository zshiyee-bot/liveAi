###############################################################################
#  弹幕安全与口播包装 —— **全部确定性，不依赖 LLM**
#
#  为什么单独一个模块：下面这些事必须"说到做到"，不能交给模型自觉：
#    · 屏蔽词 / 刷屏噪音        → 这条弹幕**回不回**
#    · 注入攻击识别             → 想让模型忘设定、重复100遍的，不回
#    · 昵称 / 弹幕原文清洗       → 念出来不能是噪音、不能带违规内容
#    · 回复输出校验             → 硬截断 / 重复抑制 / 元词汇丢弃 / 兜底话术
#    · 回复模板渲染             → 称呼 + 复述原文 那层壳
#
#  分工：LLM 只负责生成**回复正文**；外面那层壳（称呼谁、念不念原文、
#  念多长、要不要丢弃）全部在这里由程序决定。
#  engine.py 在「弹幕入口」和「送 TTS 之前」各调一次。
###############################################################################

import json
import os
import random
import re
import sys
import unicodedata

try:                                    # 项目内跑 → 用统一的日志（跟其它模块一致）
    from utils.logger import logger
except Exception:                       # 独立包/免安装版里没有它 → 保持零依赖
    import logging
    logger = logging.getLogger(__name__)


def app_dir() -> str:
    """「规则.json」应该放哪个目录。

    · 普通运行 → 本模块所在目录
    · 打包成免安装 exe（PyInstaller）后 → **exe 所在目录**
      （冻结后 __file__ 指向解包出来的临时目录，用户根本看不到、改不了，
        所以必须用 sys.executable 所在的目录，用户才能把 规则.json 放旁边改）
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


_RULES_PATH = os.path.join(app_dir(), "规则.json")

# ── 可选的外部规则文件 ────────────────────────────────────────────────
# 同目录放一个「规则.json」就能追加/覆盖规则，**不用改代码**。
# 独立工具包「弹幕拦截」用的就是它；项目里不放这个文件时，行为与以前完全一致。
# 只认这几个键：注入正则 / 噪音_同字重复次数 / 默认兜底话术
# 规则文件读失败时的原因（给命令行/界面报警用）。**不能静默**：
# 实测用记事本或 PowerShell 保存的 json 会带 BOM，用 encoding="utf-8" 读会直接
# JSONDecodeError → 规则被全部忽略，而用户毫不知情（还在纳闷"我改了怎么没用"）。
_RULES_ERROR = ""


def _load_rules() -> dict:
    """读规则文件。容忍 BOM（utf-8-sig）；失败时把原因记进 _RULES_ERROR。"""
    global _RULES_ERROR
    _RULES_ERROR = ""
    if not os.path.exists(_RULES_PATH):
        return {}
    try:
        with open(_RULES_PATH, encoding="utf-8-sig") as f:
            d = json.load(f)
        if not isinstance(d, dict):
            raise ValueError("顶层必须是一个对象 { ... }")
        return d
    except Exception as e:
        _RULES_ERROR = f"{type(e).__name__}: {e}"
        return {}


_RULES: dict = _load_rules()

_WS_RE = re.compile(r"\s+")
# 匹配用（屏蔽词/注入检测）时要去掉的标点与空白
_PUNCT_RE = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)
# 念出来时只保留这些：中日文、字母、数字
_SPEAK_KEEP_RE = re.compile(r"[^\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7afA-Za-z0-9]")


def normalize(text: str) -> str:
    """全角转半角（NFKC）+ 折叠空白 + 转小写。比较前统一走这里。

    全角是常见的绕过手段：`ｓｙｓｔｅｍ：` 不归一化就匹配不到。
    """
    t = unicodedata.normalize("NFKC", text or "")
    return _WS_RE.sub(" ", t).strip().lower()


def match_key(text: str) -> str:
    """匹配键：归一化后再去掉**所有标点和空白**。

    这样 "1" 不会命中 "扣1" 里的 "1"，"。1。" 也能命中 "1"。
    """
    return _PUNCT_RE.sub("", normalize(text))


def parse_words(raw: str) -> list[str]:
    """把多行 / 逗号 / 顿号 / 分号 / 竖线分隔的屏蔽词解析成去重列表。"""
    out: list[str] = []
    for p in re.split(r"[\n\r,，、;；|]+", raw or ""):
        w = p.strip()
        if w and w not in out:
            out.append(w)
    return out


# ── 刷屏噪音 ──────────────────────────────────────────────────────────
def _norm_same_char(v) -> int:
    """「同一个字重复几次算刷屏」——默认 3，规则文件可改（2~10 之间夹取）。"""
    try:
        n = int(v)
    except Exception:
        return 3
    return max(2, min(10, n))


# 同字重复几次算噪音（可用 规则.json 的「噪音_同字重复次数」覆盖）
_NOISE_SAME_CHAR = _norm_same_char(_RULES.get("噪音_同字重复次数"))


def is_noise(text: str) -> bool:
    """纯符号 / 纯数字 / 同一个字重复 N 次以上 —— 直播间典型刷屏，不值得回。

    刻意**不**把单个汉字（"好""嗯"）算噪音：那种话虽然短，但回一下很正常。
    """
    k = match_key(text)
    if not k:
        return True                       # 只有标点/表情/空白
    if k.isdigit():
        return True                       # 1 / 111 / 666 / 12345
    if len(k) >= _NOISE_SAME_CHAR and len(set(k)) == 1:
        return True                       # 哈哈哈 / 。。。 / 喵喵喵
    return False


# ── 屏蔽词 ────────────────────────────────────────────────────────────
def hit_block_word(text: str, words: list[str], mode: str = "exact") -> str | None:
    """命中屏蔽词就返回命中的那个词（写日志用），否则 None。

    mode:
      · "exact"（默认）整条相同才算 —— 屏蔽词填 "1" 时**不会误杀「扣1」**
      · "contains"      只要出现就挡 —— 更狠，适合屏蔽违禁词
    """
    if not words:
        return None
    key = match_key(text)
    if not key:
        return None
    for w in words:
        wk = match_key(w)
        if not wk:
            continue
        if (wk in key) if mode == "contains" else (key == wk):
            return w
    return None


# ── 注入攻击识别 ──────────────────────────────────────────────────────
# 只覆盖"常见话术"，不是万能的 —— 真正的兜底在输出层（sanitize_reply）。
_INJECT_PATTERNS = [
    r"system\s*[:：]",                                   # system: ...
    r"(忽略|无视|忘记|忘掉|清空|抛弃|删掉)[^。！？!?]{0,10}"
    r"(以上|前面|之前|上面|所有|你的|全部)?[^。！？!?]{0,10}"
    r"(设定|指令|提示|要求|规则|人设|角色|身份)",
    r"(你|您)(现在|从现在起|接下来)?\s*(是|要扮演|扮演|假装成|假装是|作为)\s*"
    r"[^\s，。！？]{0,14}(助手|机器人|ai|人工智能|管理员|开发者|黑客|猫|狗)",
    r"(重复|念|说|叫|喊|刷)\s*(我|它|这句话)?\s*\d+\s*(次|遍|回)",   # 喵100次
    r"repeat\s*[:：]?\s*\d+",                             # repeat / repeat: 10
    r"repeat\s+(this|it|after\s+me)",
    r"(输出|打印|告诉我|说出|复述)[^。！？!?]{0,12}(提示词|提示语|prompt|设定|系统消息|指令|人设)",
    # 语序反过来也要拦：「把系统提示词输出出来」「你的设定念一遍」
    r"(系统|你的|你的?初始)?(提示词|提示语|prompt|系统消息|设定|人设)[^。！？!?]{0,10}"
    r"(输出|打印|说出|复述|告诉|发出来|念|背)",
    r"系统提示词|system\s*prompt",                       # 直说这个词本身就很不正常
    r"<\s*\|[^|]*\|\s*>",                                # 伪造特殊 token
    r"(进入|开启|切换到?)\s*[^\s，。]{0,8}(开发者|调试|管理员|上帝|越狱)模式",
    r"jail\s*break|dan\s*mode|越狱模式",
]
# 内置的那批留一份原样（reload_rules 时要在它基础上重新追加外部规则）
_INJECT_BUILTIN = list(_INJECT_PATTERNS)
# 外部「规则.json」里的「注入正则」会**追加**在这后面（不是替换）——
# 内置的照样生效，用户加自己的话术不用懂代码。
_INJECT_PATTERNS += [p for p in (_RULES.get("注入正则") or []) if isinstance(p, str) and p.strip()]
_INJECT_RE = re.compile("|".join(_INJECT_PATTERNS), re.I)


def reload_rules() -> dict:
    """重新读一次规则文件（改完 规则.json 不用重启进程）。返回读到的规则。"""
    global _RULES, _INJECT_RE, _NOISE_SAME_CHAR, DEFAULT_FALLBACK
    _RULES = _load_rules()
    extra = [p for p in (_RULES.get("注入正则") or []) if isinstance(p, str) and p.strip()]
    _INJECT_RE = re.compile("|".join(_INJECT_BUILTIN + extra), re.I)
    _NOISE_SAME_CHAR = _norm_same_char(_RULES.get("噪音_同字重复次数"))
    DEFAULT_FALLBACK = ((_RULES.get("默认兜底话术") or "").strip()
                        or "这个我就不接了啊，咱们还是聊产品。")
    return _RULES


def looks_like_injection(text: str) -> str | None:
    """命中就返回命中的片段（写日志用），否则 None。"""
    m = _INJECT_RE.search(normalize(text))
    return m.group(0) if m else None


# ── 念得出来吗（昵称 / 弹幕原文）──────────────────────────────────────
def speakable(text: str, max_len: int = 0) -> str:
    """洗成"能念出来"的样子：去掉 emoji / 颜文字 / 特殊符号 / 链接残留。

    只做**可读性**清洗，不做内容合规判断（那是屏蔽词和输出校验的活）。
    """
    t = unicodedata.normalize("NFKC", text or "")
    t = _SPEAK_KEEP_RE.sub("", t)
    t = t.strip()
    if max_len and len(t) > max_len:
        t = t[:max_len]
    return t


def clean_name(name: str, max_len: int = 6, words: list[str] | None = None) -> str:
    """观众昵称 → 能念的样子；**念不出来就返回 ""**（调用方会换一个不带称呼的模板）。

    念不出来的情况：纯数字、像账号 ID（带 2 个以上数字）、命中屏蔽词。
    """
    n = speakable(name, 0)
    if not n:
        return ""
    if n.isdigit():
        return ""                                   # "12345"
    if len(re.findall(r"\d", n)) >= 2:
        return ""                                   # "user2024" / "小明9527"
    if hit_block_word(n, words or [], "contains"):
        return ""
    return n[:max_len] if max_len else n


def clean_msg(text: str, max_len: int = 24, words: list[str] | None = None) -> str:
    """弹幕原文 → 能念的样子；**不该念的就返回 ""**（调用方会换模板）。

    这些情况不念原文：命中注入特征、命中屏蔽词、太长了（截一半念出来更怪）。
    """
    if looks_like_injection(text):
        return ""
    if hit_block_word(text, words or [], "contains"):
        return ""
    m = speakable(text, 0)
    if not m:
        return ""
    if max_len and len(m) > max_len:
        return ""
    return m


def clean_paraphrase(para: str, original: str = "", max_len: int = 12,
                     words: list[str] | None = None) -> str:
    """AI 给的"转述"（用主播自己的话概括观众那句）→ 能念的样子；不行就返回 ""。

    **这里的关键是"不许照抄"**：真主播从来不会把弹幕一字不差念出来，
    而是概括一下（观众说「主播你现在在哪里直播呀」→ 念成「问我人在哪儿」）。
    所以：
      · 太长了不要（转述本来就该短）
      · 命中屏蔽词 / 注入特征不要
      · **和原话几乎一模一样就不要** —— 那说明模型偷懒照抄了，
        这种宁可退化成不带复述的句式，也别让它念出一句生硬的复读
    """
    p = speakable(para, 0)
    if not p:
        return ""
    p = p.strip("，。！？、,.!?;；:：\"'“”‘’(").strip()
    if not p:
        return ""
    if max_len and len(p) > max_len:
        return ""
    if hit_block_word(p, words or [], "contains"):
        return ""
    if looks_like_injection(p):
        return ""
    # 和原话高度重合 = 照抄 → 不要
    np, no = match_key(p), match_key(original)
    if no and np:
        if np == no:
            return ""
        if len(np) >= 6 and (np in no or no in np) and min(len(np), len(no)) >= 0.8 * max(len(np), len(no)):
            return ""
    return p


# ── 回复输出校验（最后一道闸，确定性）─────────────────────────────────
# 模型被绕过后可能吐出这些元词汇 / 系统提示泄漏 —— 整条丢弃
_META_RE = re.compile(
    r"(system\s*prompt|系统提示词|系统消息|上文的?指令|忽略以上|"
    r"我的?系统指令|作为一个?(ai|人工智能)语言模型|i am an ai|as an ai language model)")
# 同一个字符连续 4 次以上（喵喵喵喵 / 哈哈哈哈哈）→ 只留一个
_REPEAT_RE = re.compile(r"(.)\1{3,}")
# 模型爱加的前缀
_PREFIX_RE = re.compile(r"^\s*(回复|回答|答|主播回复)\s*[:：]\s*")
_TRIM_AT = "。！？；…!?;."

DEFAULT_FALLBACK = (_RULES.get("默认兜底话术") or "").strip() or "这个我就不接了啊，咱们还是聊产品。"


def _hard_trim(t: str, limit: int) -> str:
    """截到 limit 字以内，优先在标点处断开，别把词切一半。"""
    head = t[:limit]
    for p in _TRIM_AT:
        i = head.rfind(p)
        if i >= limit * 0.6:          # 为了一个标点砍掉一半，不值
            return head[:i + 1]
    return head


# ── 商品接地检查：不许提"我们自己从没提供过"的商品 ─────────────────────
# 实测踩过（用户报的）：观众只发一句「你好」，回复里凭空冒出「看看我家婴儿车」——
# 参考知识里是狗粮、人设里也没提车，那句就是模型自己编的。光在提示词里写"不要编"
# 挡不住（模型觉得自己在帮主播卖货），所以这里加一道**确定性**的闸。
#
# 做法是**通用**的、不写死任何类目：
#   白名单 = 我们自己提供过的文字（参考知识 + 人设 + 观众这句 + 正在播的话术）
#   命中白名单以外的"商品提法" → 判定为编造 → 整条换兜底话术
# 两道网：
#   ① 常见品类词表 —— 「婴儿车才几十块」这种裸提法
#   ② 「我家/咱家/我们家 + X」占有式提法 —— X 不在词表里也能抓（配合后缀判断）
COMMON_CATEGORY_WORDS = frozenset("""
婴儿车 童车 伞车 遛娃车 学步车 平衡车 滑板车 自行车 电动车 三轮车 玩具车 扭扭车
安全座椅 婴儿床 爬行垫 餐椅 背带 腰凳 奶瓶 奶嘴 奶粉 米粉 辅食 磨牙棒 纸尿裤 拉拉裤 尿不湿
湿巾 棉柔巾 洗澡盆 浴盆 隔尿垫 防走失带 童装 童鞋 爬服 抱被 睡袋 玩具 积木 拼图 绘本
图书 文具 笔袋 水彩笔 课桌 书包 手办 模型 盲盒 玩偶 公仔 抱枕
狗粮 猫粮 猫砂 狗窝 猫爬架 牵引绳 猫条 冻干 罐头 化毛膏 驱虫药 益生菌 宠物 狗绳 猫抓板
零食 坚果 果干 蜜饯 辣条 薯片 饼干 蛋糕 面包 巧克力 糖果 冰淇淋 方便面 螺蛳粉 火锅底料
牛肉干 火腿肠 鸡蛋 大米 面粉 食用油 酱油 食醋 食盐 白糖 蜂蜜 牛奶 酸奶 咖啡 茶叶 茶包
饮料 果汁 矿泉水 啤酒 白酒 红酒 保健品 维生素 钙片 蛋白粉 鱼油 胶原蛋白 代餐 燕窝 阿胶
手机 电脑 平板 笔记本 耳机 音箱 手表 手环 相机 摄像机 投影仪 充电宝 数据线 充电器 插排
键盘 鼠标 显示器 电视 冰箱 洗衣机 空调 风扇 加湿器 净化器 扫地机器人 电饭煲 空气炸锅
电水壶 榨汁机 破壁机 微波炉 烤箱 电磁炉 锅具 不粘锅 刀具 砧板 保鲜盒 水杯 保温杯 餐具
洗碗机 消毒柜 热水器 浴霸 马桶 花洒 台灯 灯泡 插座 开关 收纳箱 收纳柜 置物架 晾衣架
垃圾桶 拖把 扫把 抹布 纸巾 抽纸 卷纸 垃圾袋 洗衣液 洗洁精 消毒液 除螨仪 吸尘器
牙膏 牙刷 洗发水 护发素 沐浴露 洗面奶 面膜 精华 面霜 眼霜 防晒霜 护手霜 身体乳 口红
唇膏 粉底 气垫 遮瑕 眼影 眼线 睫毛膏 腮红 卸妆水 香水 美甲 化妆刷 美容仪 吹风机 卷发棒
剃须刀 电动牙刷 冲牙器 卫生巾 口罩 创可贴 体温计 血压计 血糖仪 按摩仪 颈椎枕 护腰
护膝 泡脚桶 瑜伽垫 哑铃 跳绳 跑步机 帐篷 登山鞋 钓鱼竿 羽毛球拍
衣服 外套 夹克 羽绒服 卫衣 毛衣 衬衫 打底衫 裤子 牛仔裤 裙子 连衣裙 睡衣 内衣 内裤 袜子
拖鞋 运动鞋 皮鞋 靴子 帽子 围巾 手套 腰带 眼镜 太阳镜 墨镜 首饰 项链 耳环 手链 戒指
包包 背包 双肩包 行李箱 钱包 手机壳 贴膜
床垫 枕头 被子 被套 四件套 床单 毛毯 地毯 沙发 茶几 餐桌 书桌 椅子 板凳 衣柜 鞋柜
酒柜 书架 床架 床头柜 窗帘 门帘
""".split())

# 「我家/咱家/我们家 + X」：X 会被单独拎出来看是不是商品
_PRODUCT_POSSESS_RE = re.compile(
    r"(?:我家|咱家|我们家|咱们家|本店|小店|店里|咱这店)(?:的)?"
    r"([\u4e00-\u9fa5A-Za-z0-9]{1,6})")
# 占有式提法里明确**不是商品**的（人/关系/泛指）—— 免得「我家孩子」被误判
_NOT_PRODUCT = frozenset("""
孩子 娃 娃儿 儿子 女儿 宝宝 老人 父母 爸妈 老爸 老妈 老婆 老公 家人 家里人 家里 兄弟 姐妹
哥哥 姐姐 弟弟 妹妹 爷爷 奶奶 姥姥 姥爷 闺女 孙子 孙女 宠物 狗 猫 猫咪 狗狗 东西 货 宝贝
产品 生意 直播间 直播 团队 客服 仓库 工厂 店 档口 供应商 货源 伙伴 客人 客户 粉丝 观众
""".split())
# 词表里没有、但一看就是商品的后缀（「我家蒸汽眼罩」这种也能兜住）
_PRODUCT_SUFFIX = (
    "车", "机", "器", "包", "袋", "盒", "杯", "锅", "刀", "鞋", "衣", "裤", "袜", "帽",
    "表", "镜", "灯", "椅", "桌", "床", "柜", "架", "垫", "毯", "帘", "纸", "笔", "书",
    "盘", "线", "套", "碗", "勺", "壶", "罐", "瓶", "伞", "球", "板", "网", "绳", "刷",
    "巾", "膜", "贴", "箱", "桶", "盆", "扇", "钟", "秤", "仪", "棒", "粉", "膏", "霜",
    "液", "乳", "油", "片", "丸", "剂", "糖", "茶", "酒", "米", "奶", "粮", "药", "窝",
    "笼", "罩", "枕", "席", "环", "链", "珠", "铃", "筒", "管", "圈", "袍", "裙", "夹",
    "神器", "好物", "款式", "型号", "套装", "组合",
)


def _looks_like_product(x: str) -> bool:
    """X 像不像一个商品：整体/前 2~4 字命中词表，或者以商品后缀结尾。"""
    if len(x) >= 2:
        for n in (len(x), 4, 3, 2):
            if x[:n] in COMMON_CATEGORY_WORDS:
                return True
    return len(x) >= 2 and x.endswith(_PRODUCT_SUFFIX)


def product_mentions(text: str) -> list[str]:
    """从回复里挑出所有"商品提法"（去重、保序）——不判断接不接地。"""
    t = speakable(text, 0) or (text or "")
    out: list[str] = []
    for w in sorted(COMMON_CATEGORY_WORDS, key=len, reverse=True):
        if w in t and w not in out:
            out.append(w)
    for m in _PRODUCT_POSSESS_RE.finditer(t):
        x = m.group(1)
        if len(x) < 2 or x in _NOT_PRODUCT:
            continue
        if any(w in x for w in out):        # 「奶粉」已经抓到了，「奶粉啊」不用再报一次
            continue
        if _looks_like_product(x) and x not in out:
            out.append(x)
    return out


def ungrounded_mentions(reply: str, allowed: str = "") -> list[str]:
    """回复里**凭空出现**的商品提法（allowed 里没有的）。

    allowed 必须是"我们自己提供过的文字"：参考知识 + 人设 + 观众这句 + 正在播的话术。
    allowed 为空时不做检查（返回 []）——宁可漏判，也不能把正常回复全兜底了。
    """
    if not (allowed or "").strip():
        return []
    a = allowed or ""
    return [w for w in product_mentions(reply) if w not in a]


def sanitize_reply(text: str, max_chars: int = 0, fallback: str = "",
                   allowed: str = "") -> str:
    """把回复洗成"能直接送 TTS"的样子；判定为异常就返回兜底话术。

    max_chars = 0 表示不截断（套完模板后已经有确定性上限了）。
    allowed  = 我们自己提供过的文字（参考知识+人设+弹幕+正在播的话术）。
               传了它就会做"商品接地检查"：回复里出现了我们没提供过的商品 → 整条换兜底，
               宁可少说一句，也不能让数字人凭空报出一个店里根本没有的货。
    """
    fb = (fallback or DEFAULT_FALLBACK).strip()
    t = _PREFIX_RE.sub("", (text or "").strip())
    t = t.strip().strip('"').strip("“”").strip("'").strip("‘’").strip()
    if not t:
        return fb
    if _META_RE.search(normalize(t)):
        return fb                     # 系统提示泄漏 / 元词汇 → 整条不要
    # "喵喵喵…×100" 这类：压完只剩一两个字，说明整条就是复制粘贴出来的 ——
    # 那是攻击的特征，不是正常回复，直接兜底（不然会念出一个孤零零的「喵」）。
    before = len(t)
    collapsed = _REPEAT_RE.sub(lambda m: m.group(1), t)
    if before >= 6 and len(collapsed) <= before * 0.4:
        return fb
    t = collapsed
    # 商品接地检查：回复里蹦出我们资料中根本没有的货 → 整条不要（走兜底）
    bad = ungrounded_mentions(t, allowed)
    if bad:
        logger.warning("回复里出现我们没提供过的商品 %s → 整条换成兜底话术", bad)
        return fb
    if max_chars > 0 and len(t) > max_chars:
        t = _hard_trim(t, max_chars)
    t = t.strip()
    return t or fb


# ── 回复模板（称呼 / 念原文 那层壳）────────────────────────────────────
# {reply} LLM 生成的正文   {name} 观众昵称（已清洗）   {msg} 弹幕原文（已清洗）
DEFAULT_TEMPLATES = [
    # ① 不带称呼 —— 「读观众名字」关着时用这几条（单独开「念原文」也有句式可用）
    "{reply}",
    "说到这个，{reply}",
    "来，我统一回一下，{reply}",
    "有人问{msg}，{reply}",
    "{msg}，{reply}",
    # ② 带称呼 —— 「读观众名字」开着时才用；后缀换着来，避免每次都「小明宝子」
    "{name}，{reply}",
    "来，{name}，{reply}",
    "{name}宝子，{reply}",
    "{name}家人，{reply}",
    "{name}宝宝，{reply}",
    "{name}这个问题问得好，{reply}",
    # ③ 带称呼 + 复述原文 —— 两个开关都开时才用
    "{name}问{msg}，{reply}",
    "刚才{name}问{msg}，{reply}",
]


def parse_templates(raw: str) -> list[tuple[str, float]]:
    """模板列表：一行一个，可选权重。返回 [(模板, 权重), ...]

    权重写法（按优先级）：
      · `模板<TAB>权重` —— 界面上的列表编辑器生成的就是这种
      · `模板 *2` / `模板 x2` / `模板 #2` —— 手写时也能用
      · 不写 = 权重 1
    权重 0 = 这条不用（比删掉更好：留着方便以后调回来）。
    """
    out: list[tuple[str, float]] = []
    seen: set[str] = set()
    for line in (raw or "").replace("\r", "\n").split("\n"):
        t = line.strip()
        if not t:
            continue
        w = 1.0
        if "\t" in t:
            t, _, tail = t.rpartition("\t")
            t = t.strip()
            try:
                w = float(str(tail).strip() or 1)
            except Exception:
                w = 1.0
        else:
            m = re.search(r"[\s]+[*x×#]\s*(\d+(?:\.\d+)?)\s*$", t)
            if m:
                try:
                    w = float(m.group(1))
                    t = t[:m.start()].strip()
                except Exception:
                    w = 1.0
        if not t or t in seen:
            continue
        seen.add(t)
        out.append((t, max(0.0, min(100.0, w))))
    return out


def default_templates() -> list[tuple[str, float]]:
    """内置默认模板（权重都一样）。"""
    return [(t, 1.0) for t in DEFAULT_TEMPLATES]


def usable_templates(templates: list[tuple[str, float]], *, allow_name: bool, allow_msg: bool,
                     has_name: bool, has_msg: bool) -> list[tuple[str, float]]:
    """筛出这次真正能用的模板（保留权重）。

    两个总闸（allow_*）关掉时，带对应占位符的模板整行失效；
    再看这次实际有没有可用的名字/原文（has_*），没有也跳过 ——
    否则会念出「，主播就在直播间」这种缺主语的怪句。
    权重 0 的条目不参与。
    """
    src = templates or default_templates()
    out: list[tuple[str, float]] = []
    for t, w in src:
        if w <= 0 or "{reply}" not in t:
            continue
        if "{name}" in t and not (allow_name and has_name):
            continue
        if "{msg}" in t and not (allow_msg and has_msg):
            continue
        out.append((t, w))
    return out or [("{reply}", 1.0)]


def pick_template(templates: list[tuple[str, float]], *, allow_name: bool, allow_msg: bool,
                  has_name: bool, has_msg: bool) -> str:
    """按**权重**随机挑一条 —— 权重 3 和权重 1 就是 3:1 的出现比例。"""
    usable = usable_templates(templates, allow_name=allow_name, allow_msg=allow_msg,
                              has_name=has_name, has_msg=has_msg)
    total = sum(w for _, w in usable)
    if total <= 0:
        return usable[0][0]
    r = random.uniform(0, total)
    acc = 0.0
    for t, w in usable:
        acc += w
        if r <= acc:
            return t
    return usable[-1][0]


def render_reply(template: str, name: str, msg: str, reply: str) -> str:
    """套模板 + 清理替换后留下的孤零零标点。

    小坑：转述通常长这样「问我人在哪儿」，而模板里可能已经带了「问」
    （`{name}问{msg}`）—— 直接拼会念成「小明**问问**我人在哪儿」。
    所以当 `{msg}` 前面紧跟「问」时，把转述开头那个「问」去掉。
    """
    t = (template or "{reply}")
    m = msg or ""
    idx = t.find("{msg}")
    if m and idx > 0 and t[:idx].rstrip().endswith("问"):
        m = re.sub(r"^\s*问(一下|问|了|的)?\s*", "", m) or m
    t = t.replace("{name}", name or "").replace("{msg}", m).replace("{reply}", reply or "")
    t = re.sub(r"^[\s，,、。:：;；]+", "", t)
    t = re.sub(r"[，,]{2,}", "，", t)
    return t.strip()


# ── 判定入口：一条弹幕到底回不回 ──────────────────────────────────────
#   **判定顺序的唯一出处**：项目（engine）和独立工具包都调它，避免两边各写一套走偏。
#   限速不在这里做 —— 那需要记住每个人的历史，交给调用方（engine / CLI 各自持有状态）。
def screen(text: str, cfg: dict | None = None) -> str | None:
    """返回挡下的原因（中文，能直接给人看）；None = 放行。

    cfg 支持的键（不传就用默认）：
      block_words   屏蔽词列表            默认 []
      block_mode    'exact' | 'contains'  默认 'exact'
      block_noise   是否过滤刷屏噪音       默认 True
      inject_filter 是否过滤注入攻击       默认 True
      max_len       单条弹幕长度上限，0=不限 默认 0
    """
    c = (text or "").strip()
    if not c:
        return "空内容"
    cfg = cfg or {}
    try:
        max_len = int(cfg.get("max_len") or 0)
    except Exception:
        max_len = 0
    if max_len and len(c) > max_len:
        return f"太长（{len(c)} > {max_len} 字）"
    if cfg.get("inject_filter", True):
        hit = looks_like_injection(c)
        if hit:
            return f"疑似注入攻击（{hit[:20]}）"
    hit = hit_block_word(c, cfg.get("block_words") or [], cfg.get("block_mode") or "exact")
    if hit:
        return f"命中屏蔽词「{hit}」"
    if cfg.get("block_noise", True) and is_noise(c):
        return "刷屏噪音（纯数字/纯符号/重复字）"
    return None