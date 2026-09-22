#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""抖音弹幕转发器 —— 把本机 DouyinBarrageGrab 的弹幕送到 LiveTalking 服务器。

为什么需要它
------------
抖音弹幕没有给个人开发者的公开接口，只能靠 DouyinBarrageGrab 在「正在直播的那台
Windows」上抓包（它挂系统代理，解抖音的弹幕 WebSocket 流量）。但数字人/LiveStream
通常跑在另一台服务器上，而中继只监听它自己那台机器的 127.0.0.1:8888，服务器连不到。

这个脚本就是那根线，当一条**哑管道**：

    DouyinBarrageGrab (ws://127.0.0.1:8888)
            │  原始 JSON（Type / ProcessName / Data）
            ▼
    本脚本  ──POST──►  LiveTalking 服务器  /ls/api/danmaku/forward
                       （解析成弹幕 → LLM 回复 → 播放队列 → 数字人）

原始 JSON 原样转发，**解析全在服务器侧做**（DouyinCollector._parse_msg），所以抖音或
中继升级格式时只要服务器更新一次，所有用户的转发器都不用动。

用法
----
Windows：双击同目录下的「启动弹幕转发.bat」（先在 bat 里填服务器地址），或在 cmd 里：

    python danmaku_forward.py --server http://192.168.1.10:8063
    python danmaku_forward.py --server http://1.2.3.4:8063 --token 你的口令

参数也可用环境变量：LS_SERVER / LS_DANMAKU_TOKEN / LS_RELAY_WS
按 Ctrl+C 停止。
"""

import argparse
import json
import os
import sys
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


def build_url(server: str) -> str:
    s = (server or "").strip().rstrip("/")
    if not s:
        raise SystemExit("[错误] 必须指定 --server，例如 --server http://192.168.1.10:8063")
    if not s.startswith(("http://", "https://")):
        s = "http://" + s
    return s + "/ls/api/danmaku/forward"


def main() -> int:
    # 行缓冲：即使用重定向/管道跑，输出也立刻能看到（不然卡在缓冲区里像"没反应"）
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    ap = argparse.ArgumentParser(
        description="把本机 DouyinBarrageGrab 的弹幕转发到 LiveTalking 服务器")
    ap.add_argument("--server", default=os.getenv("LS_SERVER", ""),
                    help="LiveTalking 服务器地址，如 http://192.168.1.10:8063")
    ap.add_argument("--token", default=os.getenv("LS_DANMAKU_TOKEN", ""),
                    help="与服务器环境变量 LS_DANMAKU_TOKEN 一致；服务器没配就不用填")
    ap.add_argument("--relay", default=os.getenv("LS_RELAY_WS", "ws://127.0.0.1:8888"),
                    help="本机 DouyinBarrageGrab 的 WebSocket 地址")
    ap.add_argument("--key", default=os.getenv("LS_ROOM_KEY", ""),
                    help="房间标识：一台服务器带多场直播时用来区分（同一房间要填一样的值）")
    ap.add_argument("--dialect", default=os.getenv("LS_DIALECT", "auto"), choices=_DIALECTS,
                    help="抓包工具的报文方言：auto=自动猜（默认）/ ape=DouyinBarrageGrab / "
                         "wushuai=BarrageGrab")
    args = ap.parse_args()

    url = build_url(args.server)
    headers = {"Content-Type": "application/json"}
    if args.token:
        headers["X-Danmaku-Token"] = args.token
    if args.key:
        headers["X-Room-Key"] = args.key

    print("=" * 64)
    print(" 抖音弹幕转发器")
    print(f"   本机中继 : {args.relay}")
    print(f"   转发目标 : {url}")
    print(f"   房间标识 : {args.key or '(未设置 → 服务器默认房间)'}")
    print(f"   鉴权口令 : {'已设置' if args.token else '未设置（服务器没配 token 就不用管）'}")
    print(f"   报文方言 : {args.dialect}" + (f"（{_DIALECT_NOTE[args.dialect]}）" if args.dialect in _DIALECT_NOTE else
                                          "（按字段特征自动识别，日志里会显示每条被判成哪种）"))
    print("   按 Ctrl+C 停止")
    print("=" * 64)

    sess = requests.Session()
    pending: list = []
    sent = 0
    fails = 0
    dropped = 0
    seen_dialects: set = set()
    last_note = ""
    last_note_room = [""]        # 只提示一次"归到哪个房间"
    t_report = time.time()
    t_reach_err = 0.0

    def flush():
        """把攒下的弹幕发一包（单条就直接发对象，多条发 {"items":[...]}）。"""
        nonlocal sent, fails, last_note
        if not pending:
            return
        items = list(pending)
        pending.clear()
        payload = items[0] if len(items) == 1 else {"items": items}
        try:
            r = sess.post(url, json=payload, headers=headers, timeout=10)
        except Exception as e:
            fails += 1
            if fails <= 1 or fails % 20 == 0:
                print(f"[警告] 转发失败（第 {fails} 次）：{e}")
            return
        if r.status_code != 200:
            fails += 1
            if fails <= 1 or fails % 20 == 0:
                print(f"[警告] 服务器返回 {r.status_code}：{r.text[:160]}")
            return
        fails = 0
        sent += len(items)
        # 服务器会回 {"data":{"accepted":0,"reason":"直播未启动"}}，提示一次就够
        try:
            data = (r.json() or {}).get("data") or {}
            note = data.get("reason") or ""
            if data.get("room") and data["room"] != last_note_room[0]:
                last_note_room[0] = data["room"]
                print(f"[提示] 服务器已把弹幕归到房间：{data['room']}")
        except Exception:
            note = ""
        if note and note != last_note:
            last_note = note
            print(f"[提示] 服务器：{note} —— 弹幕收到了，但还没进队列")

    try:
        while True:
            try:
                ws = websocket.create_connection(args.relay, timeout=15)
            except Exception as e:
                now = time.time()
                if now - t_reach_err >= 30:      # 别刷屏，30 秒提示一次
                    t_reach_err = now
                    print(f"[等待] 连不上本机中继 {args.relay}：{e}")
                    print("       → 请确认已启动 DouyinBarrageGrab，且它的 WebSocket 端口是 8888")
                time.sleep(3)
                continue

            print(f"[ok] 已连上本机中继 {args.relay}，开始转发")
            ws.settimeout(_FLUSH_IDLE)
            try:
                while True:
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        flush()                      # 空闲节拍：把攒的发出、顺便报统计
                        if time.time() - t_report >= 60:
                            t_report = time.time()
                            print(f"[统计] 已转发 {sent} 条（失败 {fails} 次）")
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
                        dropped += 1
                        continue
                    if obj.get("Type") in _SKIP_TYPES:
                        continue
                    pending.append(obj)
                    if len(pending) >= _FLUSH_MAX:
                        flush()
            finally:
                flush()
                try:
                    ws.close()
                except Exception:
                    pass

            time.sleep(3)
    except KeyboardInterrupt:
        print(f"\n[退出] 共转发 {sent} 条弹幕，失败 {fails} 次"
              + (f"，丢弃 {dropped} 条（统计/分享等无关类型）" if dropped else ""))
        return 0


if __name__ == "__main__":
    sys.exit(main())
