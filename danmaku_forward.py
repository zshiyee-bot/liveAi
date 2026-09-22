#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""弹幕转发器 —— 把弹幕送进 LiveTalking 服务器（两个数据源，一个程序）。

为什么需要它
------------
各平台的弹幕都没给个人开发者公开接口，只能靠抓：抓包工具在「正在直播的那台 Windows」
上把弹幕解出来，但数字人/LiveStream 通常跑在另一台服务器上，抓包工具只在本机广播，
服务器连不到。这个脚本就是那根线，当一条**哑管道**：

    ┌ 源 1：抓包工具 (ws://127.0.0.1:8888，抖音/快手…)
    │        原始 JSON（Type / ProcessName / Data）
    └ 源 2：淘宝直播（直接轮询官方 MTOP 接口，不需要抓包工具）
             │
             ▼
    本脚本  ──POST──►  LiveTalking 服务器  /ls/api/danmaku/forward
                       （解析成弹幕 → LLM 回复 → 播放队列 → 数字人）

抖音/网易那类抓包工具的原始 JSON 原样转发，**解析全在服务器侧做**；淘宝那边本程序自己
取到弹幕、直接拼成服务器认的格式再转发。所以服务器侧一个兼容逻辑都没有增加。

用法
----
Windows：双击同目录下的「启动弹幕转发.bat」（先在 bat 里填服务器地址），或在 cmd 里：

    # 抖音/快手：先开抓包工具，再跑这个
    python danmaku_forward.py --server http://192.168.1.10:8063

    # 淘宝直播：不需要任何抓包工具，给直播间 ID 就行
    python danmaku_forward.py --server http://192.168.1.10:8063 --source taobao --live-id 2318604422529278

    # 两个一起来
    python danmaku_forward.py --server http://... --source both --live-id 2318604422529278

参数也可用环境变量：LS_SERVER / LS_DANMAKU_TOKEN / LS_RELAY_WS / LS_DIALECT / LS_SOURCE / LS_LIVE_ID
按 Ctrl+C 停止。
"""

import argparse
import collections
import hashlib
import json
import os
import sys
import threading
import time

try:
    import websocket          # websocket-client
except ImportError:
    print("[错误] 缺少 websocket-client：pip install websocket-client")
    sys.exit(1)

try:
    import requests
except ImportError:
    print("[错误] 缺少 requests：pip install requests")
    sys.exit(1)


# 点赞消息（Type=2）在服务器侧本来就会被丢掉，而且它是量最大的一类
# （点赞风暴一秒几十条），所以在源头就不过网，省带宽也省服务器 CPU。
_SKIP_TYPES = {2}

# ── 报文方言 ──────────────────────────────────────────────────────────
# 抓抖音的工具有两家主流，**Type 编号和字段名不一样**，接错了会"弹幕被当进场回、
# 关注被丢掉、昵称变未知"。服务器侧是按 ape 那套写的，所以在转发器里统一掉：
#
#   ape    （默认，DouyinBarrageGrab / ape-byte）
#          Type 1=弹幕 2=点赞 3=进场 4=关注 5=礼物      Data = JSON 字符串
#   wushuai（BarrageGrab / wushuaihua520 的开源抖音版）
#          Type 1=进场 2=关注 3=弹幕 4=点赞 5=礼物  6=分享 7=统计 8=状态 9=粉丝团
#          Data = 对象，且用户昵称字段是 NickName（大写 N）
_DIALECTS = ("auto", "ape", "wushuai")
_WUSHUAI_TYPE_MAP = {1: 3, 2: 4, 3: 1, 4: 2, 5: 5}      # 6/7/8/9 一律丢
_DIALECT_NOTE = {"ape": "DouyinBarrageGrab(ape-byte)",
                 "wushuai": "BarrageGrab(wushuaihua520)"}

_FLUSH_MAX = 30              # 攒够这么多条就发一包
_FLUSH_IDLE = 0.5            # 或者空闲这么久就发一包（秒）


def detect_dialect(obj: dict) -> str:
    """按特征猜方言：wushuai 那套的用户字段是 NickName（大写 N），ape 是 Nickname。"""
    data = obj.get("Data") if obj.get("Data") is not None else obj.get("data")
    if not isinstance(data, dict):
        return "ape"
    user = data.get("User") or data.get("user") or {}
    if isinstance(user, dict) and "NickName" in user and "Nickname" not in user:
        return "wushuai"
    if "MemberCount" in data:
        return "wushuai"
    return "ape"


def normalize(obj: dict, dialect: str = "ape") -> dict | None:
    """把不同工具的报文统一成服务器认的 ape 格式；返回 None 表示这条不要。"""
    if dialect == "auto":
        dialect = detect_dialect(obj)
    if dialect != "wushuai":
        return obj                      # ape：服务器就是按这套写的，原样透传

    typ = obj.get("Type") if obj.get("Type") is not None else obj.get("type")
    new_type = _WUSHUAI_TYPE_MAP.get(typ)
    if new_type is None:
        return None                     # 分享/统计/状态变更/粉丝团 → 丢

    data = obj.get("Data") if obj.get("Data") is not None else obj.get("data")
    if isinstance(data, dict):
        data = dict(data)
        user = data.get("User") or data.get("user")
        if isinstance(user, dict) and "NickName" in user and "Nickname" not in user:
            user = dict(user)
            user["Nickname"] = user["NickName"]     # 服务器只认 Nickname/nickname
            data["User"] = user
    return {"Type": new_type, "Data": data,
            "ProcessName": obj.get("ProcessName") or obj.get("processName") or ""}


def describe(obj: dict) -> str:
    """从 ape 格式报文里取出「类型 昵称: 内容」，用来在窗口里打印。

    抓包工具是"抓本机正在播放的流量"，没有房间号可填 —— 所以能不能确认抓的是哪个房间，
    最直接的办法就是把抓到的每条弹幕打出来。
    """
    data = obj.get("Data") if obj.get("Data") is not None else obj.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            data = {}
    if not isinstance(data, dict):
        data = {}
    user = data.get("User") or data.get("user") or {}
    nick = (user.get("Nickname") or user.get("nickname") or user.get("NickName")
            or "观众") if isinstance(user, dict) else "观众"
    t = obj.get("Type") if obj.get("Type") is not None else obj.get("type") or 0
    label = {1: "弹幕", 2: "点赞", 3: "进场", 4: "关注", 5: "礼物"}.get(t, f"类型{t}")
    body = data.get("Content") or data.get("content") or ""
    if t == 5:
        body = f"{data.get('GiftName') or data.get('giftName') or '礼物'} x{data.get('GiftCount') or data.get('giftCount') or 1}"
    return f"{label} {nick}: {body}".strip() if body else f"{label} {nick}"


def build_url(server: str) -> str:
    s = (server or "").strip().rstrip("/")
    if not s:
        raise SystemExit("[错误] 必须指定 --server，例如 --server http://192.168.1.10:8063")
    if not s.startswith(("http://", "https://")):
        s = "http://" + s
    return s + "/ls/api/danmaku/forward"


# ══════════════════════════════════════════════════════════════════════
#  出口：把攒下的弹幕 POST 给服务器（线程安全，两个源共用）
# ══════════════════════════════════════════════════════════════════════

class Forwarder:
    def __init__(self, url: str, headers: dict):
        self.url = url
        self.headers = headers
        self._sess = requests.Session()
        self._lock = threading.Lock()
        self._pending: list = []
        self.sent = 0
        self.fails = 0
        self.dropped = 0
        self.last_note = ""
        self.last_room = ""

    def push(self, obj: dict):
        """塞一条待转发的（必须是服务器认的 ape 格式）。"""
        with self._lock:
            self._pending.append(obj)

    def flush(self):
        """把攒下的发一包（单条直接发对象，多条发 {"items":[...]}）。"""
        with self._lock:
            if not self._pending:
                return
            items = list(self._pending)
            self._pending.clear()
        payload = items[0] if len(items) == 1 else {"items": items}
        try:
            r = self._sess.post(self.url, json=payload, headers=self.headers, timeout=10)
        except Exception as e:
            self.fails += 1
            if self.fails <= 1 or self.fails % 20 == 0:
                print(f"[警告] 转发失败（第 {self.fails} 次）：{e}")
            return
        if r.status_code != 200:
            self.fails += 1
            if self.fails <= 1 or self.fails % 20 == 0:
                print(f"[警告] 服务器返回 {r.status_code}：{r.text[:160]}")
            return
        self.fails = 0
        self.sent += len(items)
        # 服务器会回 {"data":{"accepted":0,"reason":"直播未启动"}}，提示一次就够
        try:
            data = (r.json() or {}).get("data") or {}
            note = data.get("reason") or ""
            if data.get("room") and data["room"] != self.last_room:
                self.last_room = data["room"]
                print(f"[提示] 服务器已把弹幕归到房间：{data['room']}")
        except Exception:
            note = ""
        if note and note != self.last_note:
            self.last_note = note
            print(f"[提示] 服务器：{note} —— 弹幕收到了，但还没进队列")

    def stats_line(self) -> str:
        s = f"[统计] 已转发 {self.sent} 条（失败 {self.fails} 次"
        if self.dropped:
            s += f"，丢弃 {self.dropped} 条（统计/分享等无关类型）"
        return s + "）"


# ══════════════════════════════════════════════════════════════════════
#  源 1：抓包工具的 WebSocket 中继（抖音/快手…）
# ══════════════════════════════════════════════════════════════════════

def relay_source(args, fwd: Forwarder, stop: threading.Event):
    seen_dialects: set = set()
    t_reach_err = 0.0
    # 打印节流：一秒最多打 6 条，超了就只打每 10 条（弹幕风暴时别把窗口刷爆）
    print_win: list = [0.0, 0]          # [本秒起点, 本秒已打条数]
    suppressed = [0]

    def show(obj: dict):
        now = time.time()
        if now - print_win[0] >= 1.0:
            if suppressed[0]:
                print(f"        …（上一秒还有 {suppressed[0]} 条没打印）")
                suppressed[0] = 0
            print_win[0] = now
            print_win[1] = 0
        if print_win[1] < 6:
            print(f"   [收到] {describe(obj)}")
        else:
            suppressed[0] += 1
        print_win[1] += 1

    while not stop.is_set():
        try:
            ws = websocket.create_connection(args.relay, timeout=15)
        except Exception as e:
            now = time.time()
            if now - t_reach_err >= 30:      # 别刷屏，30 秒提示一次
                t_reach_err = now
                print(f"[等待] 连不上本机中继 {args.relay}：{e}")
                print("       → 请确认抓包工具已启动，且它的 WebSocket 端口是 8888")
            time.sleep(3)
            continue

        print(f"[ok] 已连上本机中继 {args.relay}，开始转发")
        ws.settimeout(_FLUSH_IDLE)
        try:
            while not stop.is_set():
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                except Exception as e:
                    print(f"[断开] 中继连接中断：{e}，3 秒后重连")
                    break
                if not raw:
                    continue
                if isinstance(raw, bytes):
                    try:
                        raw = raw.decode("utf-8")
                    except Exception:
                        continue
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue                     # 不是 JSON 就丢，别打断管道
                if not isinstance(obj, dict):
                    continue
                # 方言归一：让 ape / wushuai 两家的报文都能直接喂给服务器
                dialect = args.dialect
                if dialect == "auto":
                    dialect = detect_dialect(obj)
                    if dialect not in seen_dialects:
                        seen_dialects.add(dialect)
                        print(f"[识别] 判定报文方言为 {dialect}"
                              f"（{_DIALECT_NOTE[dialect]}）；不对就用 --dialect ape|wushuai 强制指定")
                obj = normalize(obj, dialect)
                if obj is None:
                    fwd.dropped += 1
                    continue
                if obj.get("Type") in _SKIP_TYPES:
                    continue
                show(obj)
                fwd.push(obj)
                if len(fwd._pending) >= _FLUSH_MAX:
                    fwd.flush()
        finally:
            fwd.flush()
            try:
                ws.close()
            except Exception:
                pass
        time.sleep(3)


# ══════════════════════════════════════════════════════════════════════
#  源 2：淘宝直播（直接轮询官方 MTOP 接口，不需要抓包工具）
#
#  实测（2026-09，未登录可用）：
#    1) mtop.roomstudio.live.detail.get {liveId}          → 拿到 topic（UUID）
#    2) mtop.taobao.iliad.comment.query.latest
#         {topic, tab:"0", limit:20}                      → data.comments[]（弹幕）
#  弹幕字段：commentId / content / publisherNick / timestamp
#  鉴权：MTOP H5 通用签名 —— sign = md5(token&t&appKey&data)，token 来自 _m_h5_tk cookie，
#        第一次请求服务端会把 cookie 下发下来（返回 FAIL_SYS_TOKEN_EMPTY），带 token 重试即可。
# ══════════════════════════════════════════════════════════════════════

_TB_APPKEY = "12574478"
_TB_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/126.0.0.0 Safari/537.36")
# 淘宝弹幕里混着的系统消息（进场/关注/点赞）用这些私有区字符开头，别当真人弹幕回
_TB_SYS_MARKS = ("\u2042", "\u2230", "\u23c7")


def _tb_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": _TB_UA,
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://tbzb.taobao.com/",
        "Origin": "https://tbzb.taobao.com",
    })
    return s


def _tb_mtop(sess: requests.Session, api: str, data: dict, v: str = "1.0") -> dict:
    """调一次 MTOP（自动完成 token 下发 + 签名重试）。失败返回 {}。"""
    dstr = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    for attempt in (1, 2):
        t = str(int(time.time() * 1000))
        tk = sess.cookies.get("_m_h5_tk", "") or ""
        token = tk.split("_")[0] if tk else ""
        sign = hashlib.md5(f"{token}&{t}&{_TB_APPKEY}&{dstr}".encode()).hexdigest()
        try:
            r = sess.get(
                f"https://h5api.m.taobao.com/h5/{api}/{v}/",
                params={"jsv": "2.6.1", "appKey": _TB_APPKEY, "t": t, "sign": sign,
                        "api": api, "v": v, "type": "originaljson", "dataType": "json",
                        "data": dstr},
                timeout=20)
            j = r.json()
        except Exception:
            return {}
        if any("TOKEN" in str(x).upper() for x in j.get("ret", [])) and attempt == 1:
            continue                       # 服务端刚把 _m_h5_tk 下发下来，带 token 再来一次
        return j
    return {}


def taobao_source(args, fwd: Forwarder, stop: threading.Event):
    """轮询淘宝直播弹幕。"""
    live_id = str(args.live_id).strip()
    sess = _tb_session()
    interval = max(1.0, float(args.interval))
    print(f"[淘宝] 直播间 {live_id}：正在打开页面取 cookie…")
    try:
        sess.get(f"https://tbzb.taobao.com/live?liveId={live_id}", timeout=25)
    except Exception as e:
        print(f"[淘宝] 打不开直播页（{e}），仍然继续尝试接口")

    topic = ""
    while not stop.is_set():
        det = _tb_mtop(sess, "mtop.roomstudio.live.detail.get", {"liveId": live_id})
        topic = ((det.get("data") or {}).get("topic") or "") if det else ""
        if topic:
            break
        print(f"[淘宝] 取 topic 失败（{str(det.get('ret'))[:80]}…），10 秒后重试")
        if stop.wait(10):
            return
    print(f"[淘宝] topic = {topic}，开始每 {interval} 秒轮询弹幕")

    seen = collections.OrderedDict()          # commentId -> True（去重 + 保序）
    seen_max = 1000
    first = True
    while not stop.is_set():
        j = _tb_mtop(sess, "mtop.taobao.iliad.comment.query.latest",
                     {"topic": topic, "tab": "0", "limit": 20})
        ret = str(j.get("ret") or "")
        if "SUCCESS" not in ret:
            print(f"[淘宝] 拉弹幕失败：{ret[:120]}；10 秒后重试")
            if stop.wait(10):
                return
            continue
        comments = ((j.get("data") or {}).get("comments") or [])
        # 接口返回的是"最近 N 条"（新的不一定在最前面）→ 按时间戳升序，保证念的顺序对
        comments.sort(key=lambda c: c.get("timestamp") or 0)
        fresh = []
        for c in comments:
            cid = c.get("commentId")
            if cid in seen:
                continue
            seen[cid] = True
            while len(seen) > seen_max:
                seen.popitem(last=False)
            fresh.append(c)
        # 第一次拉到的都是"启动前的老弹幕"，默认不念（要念就加 --replay-backlog）
        if first:
            fresh = fresh[-int(args.replay_backlog):] if int(args.replay_backlog) > 0 else []
            first = False
        for c in fresh:
            content = (c.get("content") or "").strip()
            if not content or content.startswith(_TB_SYS_MARKS):
                continue
            nick = (c.get("publisherNick") or c.get("tbNick") or "观众").strip()
            ctype = ((c.get("renders") or {}).get("commentType") or "normal")
            if ctype != "normal":
                continue
            fwd.push({
                "Type": 1,                     # 服务器认的 ape 格式：1=弹幕
                "Data": json.dumps({"User": {"Nickname": nick}, "Content": content,
                                    "CommentId": c.get("commentId")},
                                   ensure_ascii=False),
                "ProcessName": "taobao_live",
            })
            print(f"[淘宝] {nick}: {content}")
        if stop.wait(interval):
            return


# ══════════════════════════════════════════════════════════════════════

def main() -> int:
    # 行缓冲：即使用重定向/管道跑，输出也立刻能看到（不然卡在缓冲区里像"没反应"）
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    ap = argparse.ArgumentParser(
        description="把弹幕转发到 LiveTalking 服务器（抓包工具 或 淘宝直播）")
    ap.add_argument("--server", default=os.getenv("LS_SERVER", ""),
                    help="LiveTalking 服务器地址，如 http://192.168.1.10:8063")
    ap.add_argument("--token", default=os.getenv("LS_DANMAKU_TOKEN", ""),
                    help="与服务器环境变量 LS_DANMAKU_TOKEN 一致；服务器没配就不用填")
    ap.add_argument("--key", default=os.getenv("LS_ROOM_KEY", ""),
                    help="房间标识：一台服务器带多场直播时用来区分（同一房间要填一样的值）")
    ap.add_argument("--source", default=os.getenv("LS_SOURCE", "relay"),
                    choices=("relay", "taobao", "both"),
                    help="数据源：relay=抓包工具（默认）/ taobao=淘宝直播 / both=两个都要")
    ap.add_argument("--relay", default=os.getenv("LS_RELAY_WS", "ws://127.0.0.1:8888"),
                    help="抓包工具的 WebSocket 地址（source=relay/both 用）")
    ap.add_argument("--dialect", default=os.getenv("LS_DIALECT", "auto"), choices=_DIALECTS,
                    help="抓包工具的报文方言：auto=自动猜（默认）/ ape=DouyinBarrageGrab / "
                         "wushuai=BarrageGrab")
    ap.add_argument("--live-id", default=os.getenv("LS_LIVE_ID", ""),
                    help="淘宝直播间 ID（source=taobao/both 必填）：从地址栏 liveId= 后面那串数字拿")
    ap.add_argument("--interval", type=float, default=float(os.getenv("LS_TAOBAO_INTERVAL", "3")),
                    help="淘宝轮询间隔秒数（默认 3，别低于 2，太频会触发风控）")
    ap.add_argument("--replay-backlog", default=os.getenv("LS_TAOBAO_REPLAY", "0"),
                    help="淘宝：启动时补发最近 N 条老弹幕（默认 0 = 只发启动后的新弹幕，测试时可填 3）")
    args = ap.parse_args()

    if args.source in ("taobao", "both") and not str(args.live_id).strip():
        print("[错误] source=taobao/both 时必须给 --live-id（或环境变量 LS_LIVE_ID）")
        print("       淘宝直播间地址形如 https://tbzb.taobao.com/live?...&liveId=2318604422529278")
        return 1

    url = build_url(args.server)
    headers = {"Content-Type": "application/json"}
    if args.token:
        headers["X-Danmaku-Token"] = args.token
    if args.key:
        headers["X-Room-Key"] = args.key

    print("=" * 64)
    print(" 弹幕转发器")
    print(f"   数据源   : {args.source}")
    if args.source in ("relay", "both"):
        print(f"   本机中继 : {args.relay}")
        print(f"   报文方言 : {args.dialect}" + (f"（{_DIALECT_NOTE[args.dialect]}）"
                                                if args.dialect in _DIALECT_NOTE else "（自动识别）"))
    if args.source in ("taobao", "both"):
        print(f"   淘宝直播 : liveId={args.live_id}，每 {args.interval} 秒轮询一次")
        print(f"   补发老弹幕: {args.replay_backlog} 条（0=只发新弹幕）")
    print(f"   转发目标 : {url}")
    print(f"   房间标识 : {args.key or '(未设置 → 服务器默认房间)'}")
    print(f"   鉴权口令 : {'已设置' if args.token else '未设置（服务器没配 token 就不用管）'}")
    print("   按 Ctrl+C 停止")
    print("=" * 64)

    fwd = Forwarder(url, headers)
    stop = threading.Event()
    threads = []
    if args.source in ("relay", "both"):
        threads.append(threading.Thread(target=relay_source, args=(args, fwd, stop),
                                        name="relay", daemon=True))
    if args.source in ("taobao", "both"):
        threads.append(threading.Thread(target=taobao_source, args=(args, fwd, stop),
                                        name="taobao", daemon=True))
    for t in threads:
        t.start()

    t_report = time.time()
    try:
        while True:
            time.sleep(_FLUSH_IDLE)
            fwd.flush()
            if time.time() - t_report >= 60:
                t_report = time.time()
                print(fwd.stats_line())
    except KeyboardInterrupt:
        stop.set()
        fwd.flush()
        print(f"\n[退出] 共转发 {fwd.sent} 条弹幕，失败 {fwd.fails} 次"
              + (f"，丢弃 {fwd.dropped} 条（统计/分享等无关类型）" if fwd.dropped else ""))
        return 0


if __name__ == "__main__":
    sys.exit(main())
