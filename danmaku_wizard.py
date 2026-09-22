#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""弹幕转发向导 —— 双击「启动弹幕转发.bat」后，一步步选，选完自动开始抓取+推送。

为什么用 Python 做向导而不是写在 .bat 里：
    cmd.exe 在 chcp 65001 下解析含中文的 .bat 会错位（会执行到 rem 行里），
    所以 .bat 只能是纯 ASCII；中文交互放在 Python 里最稳。

流程：
    ① 要抓哪个平台       抖音 / 淘宝 / 两个都要
    ② 抖音怎么抓         直播伴侣（自己开播，不开浏览器）/ 网页（抓别人的直播间）
    ③ 直播间号           抖音房间号或链接；淘宝 liveId
    ④ 服务器地址         默认 http://127.0.0.1:8063（数字人跑在本机）
    ⑤ 房间标识           一台服务器同时带多场直播时才填
    → 记住这份配置（下次直接回车就开始）
    → 需要抓包工具时自动检查管理员权限（要的话弹一次 UAC）
    → 静默启动抓包工具 → 开始转发（窗口里能看到每条弹幕）

用法：
    启动弹幕转发.bat                        进向导（会记住上次的选择）
    启动弹幕转发.bat --reset                忘掉上次的选择
    启动弹幕转发.bat --source taobao --live-id 123 --server http://1.2.3.4:8063
                                            跳过向导，直接按命令行跑（老用法）
"""

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import danmaku_forward as F                                  # noqa: E402

CFG_PATH = os.path.join(HERE, "danmaku_config.json")
GRABBER_DIR = os.path.join(HERE, "tools", "DouyinBarrageGrab")
GRABBER_EXE = os.path.join(GRABBER_DIR, "WssBarrageServer.exe")

PLATFORM_LABEL = {"douyin": "抖音直播", "taobao": "淘宝直播", "both": "抖音 + 淘宝"}
MODE_LABEL = {"companion": "直播伴侣（自己开播）", "browser": "网页/浏览器（别人的直播间）"}


def load_cfg() -> dict:
    try:
        with open(CFG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def save_cfg(cfg: dict):
    try:
        with open(CFG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  [警告] 配置保存失败（不影响本次运行）：{e}")


def pause(msg: str = "\n  按回车退出...") -> None:
    """等一个回车；没有输入（被重定向/管道跑）也不报错。"""
    try:
        input(msg)
    except (EOFError, KeyboardInterrupt):
        pass


def ask_choice(prompt: str, choices: dict, default: str) -> str:
    """带菜单的选择题：choices = {"1": ("标签", 值)}"""
    while True:
        print()
        print(prompt)
        for k, (label, _) in choices.items():
            mark = "  ← 直接回车选这个" if k == default else ""
            print(f"    {k}) {label}{mark}")
        raw = input(f"  请输入 [{'/'.join(choices)}]（回车={default}）: ").strip()
        if not raw:
            raw = default
        if raw in choices:
            return choices[raw][1]
        print("  ✗ 输入不对，请按上面列出的数字选。")


def ask_room_id(prompt: str, hint: str) -> str:
    """要一个直播间号：允许直接粘链接，自动把数字抠出来。"""
    while True:
        print()
        print(prompt)
        print(f"    {hint}")
        raw = input("  直播间号: ").strip()
        if not raw:
            print("  ✗ 这个必须填。")
            continue
        nums = re.findall(r"\d{6,}", raw)
        if nums:
            return nums[-1]
        print("  ✗ 没看出数字，把链接整个粘进来也行。")


def fetch_rooms(server: str) -> dict:
    """问服务器现在有哪些「放映间」（= 运营页里分开的直播控制面板 / 房间）。

    返回 {房间号: "正在直播"|"空闲"}；连不上就返回 {}（那就让用户手输）。
    """
    try:
        import requests
        base = (server or "").strip().rstrip("/")
        if not base.startswith(("http://", "https://")):
            base = "http://" + base
        r = requests.get(base + "/ls/api/diag", timeout=6)
        data = (r.json() or {}).get("data") or {}
        rooms = data.get("rooms") or {}
        out = {}
        if isinstance(rooms, dict):
            for key, info in rooms.items():
                info = info if isinstance(info, dict) else {}
                out[str(key)] = "正在直播" if info.get("running") else "空闲"
        if out:
            return out
        # 老接口兜底：只有默认房间
        return {"default": "正在直播" if data.get("running") else "空闲"}
    except Exception:
        return {}


def ask_room(cfg: dict) -> str:
    """选推给哪个放映间（房间标识）。列出服务器上已有的房间给用户挑。"""
    print()
    print("⑤ 要把弹幕推到哪个「放映间」？")
    print("    （放映间 = 运营页里分开的直播控制面板，多个房间互不干扰）")
    print("    只开一场直播 → 直接回车走默认房间")
    rooms = fetch_rooms(cfg.get("server") or "")
    keys = list(rooms.keys())
    if keys:
        print()
        print("    服务器上现在的房间：")
        for i, k in enumerate(keys, 1):
            tag = "" if k != "default" else "   ← 默认房间"
            print(f"      {i}) {k}（{rooms[k]}）{tag}")
        print("      （也可以直接输入一个新房名，比如 roomC）")
    else:
        print("    （连不上服务器拿房间列表，直接手输房间名也行）")
    cur = cfg.get("room_key") or ""
    print(f"    上次用的是：{cur or '默认房间'}")
    raw = input("  房间标识（回车=默认房间）: ").strip()
    if not raw:
        return cur if cur else ""
    if raw.isdigit() and keys and 1 <= int(raw) <= len(keys):
        return keys[int(raw) - 1]
    return raw


# ── 抓包工具（抖音才需要）────────────────────────────────────────────

def _is_admin() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return True          # 非 Windows 就别管了


def _elevate_self(args: list) -> bool:
    """用 runas 重新启动自己（弹一次 UAC）。返回 False = 用户点了否。"""
    try:
        import ctypes
        # SW_SHOWNORMAL = 1
        r = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, " ".join(f'"{a}"' for a in args), None, 1)
        return int(r) > 32
    except Exception:
        return False


def start_grabber(mode: str) -> subprocess.Popen | None:
    """静默启动抓包工具（独立进程、无窗口、无控制台）。"""
    if not os.path.exists(GRABBER_EXE):
        print(f"  [错误] 找不到抓包工具：{GRABBER_EXE}")
        return None
    cfg_src = os.path.join(GRABBER_DIR,
                           "config-browser.xml" if mode == "browser" else "config-companion.xml")
    if os.path.exists(cfg_src):
        try:
            with open(cfg_src, encoding="utf-8") as f:
                body = f.read()
            with open(os.path.join(GRABBER_DIR, "WssBarrageServer.exe.config"), "w",
                      encoding="utf-8") as f:
                f.write(body)
        except Exception as e:
            print(f"  [警告] 写抓包工具配置失败：{e}")
    stop_grabber()                     # 先清掉上次残留的
    flags = 0x08000000 if os.name == "nt" else 0        # CREATE_NO_WINDOW
    try:
        return subprocess.Popen([GRABBER_EXE], cwd=GRABBER_DIR, creationflags=flags,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"  [错误] 抓包工具启动失败：{e}")
        return None


def stop_grabber():
    """收掉抓包工具。先用温和方式（让它自己恢复系统代理），不行再强杀。"""
    for args, wait in ((["taskkill", "/IM", "WssBarrageServer.exe"], 8),
                       (["taskkill", "/IM", "WssBarrageServer.exe", "/F"], 10)):
        try:
            subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=wait)
        except Exception:
            pass
        time.sleep(1.5)
        if not _proc_alive("WssBarrageServer.exe"):
            break
    fix_proxy_residue()          # 兜底：万一它没把系统代理改回来


def _proc_alive(name: str) -> bool:
    try:
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {name}"],
                             capture_output=True, text=True, timeout=10).stdout or ""
        return name.lower() in out.lower()
    except Exception:
        return False


# ── 系统代理保护（浏览器模式会改系统代理，被强杀就会"断网"）────────────
PROXY_KEY = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
GRABBER_PROXY_PORT = "8827"          # tools/DouyinBarrageGrab 配置里的 proxyPort


def _reg_get(name):
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, PROXY_KEY) as k:
            return winreg.QueryValueEx(k, name)[0]
    except Exception:
        return None


def _reg_set(name, value, kind):
    try:
        import winreg
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, PROXY_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, name, 0, kind, value)
        return True
    except Exception:
        return False


def _notify_proxy_changed():
    """告诉系统/浏览器"代理设置变了"，不用重启浏览器。"""
    try:
        import ctypes
        ctypes.windll.wininet.InternetSetOptionW(0, 39, 0, 0)     # SETTINGS_CHANGED
        ctypes.windll.wininet.InternetSetOptionW(0, 37, 0, 0)     # REFRESH
    except Exception:
        pass


def proxy_snapshot() -> dict:
    return {"ProxyEnable": _reg_get("ProxyEnable"), "ProxyServer": _reg_get("ProxyServer"),
            "ProxyOverride": _reg_get("ProxyOverride"), "AutoConfigURL": _reg_get("AutoConfigURL")}


def proxy_restore(snap: dict):
    """把开抓包之前的代理设置原样放回去。"""
    if not snap:
        return
    import winreg
    if snap.get("ProxyEnable") is not None:
        _reg_set("ProxyEnable", int(snap["ProxyEnable"]), winreg.REG_DWORD)
    for k in ("ProxyServer", "ProxyOverride", "AutoConfigURL"):
        if snap.get(k) is not None:
            _reg_set(k, snap[k], winreg.REG_SZ)
    _notify_proxy_changed()


def fix_proxy_residue(verbose: bool = False) -> bool:
    """急救：系统代理还指着抓包工具的端口(8827)，但工具已经不在 → 关掉它。

    这就是"退掉抓弹幕服务之后就断网"的原因：抓包工具设了系统代理，
    被强杀时没来得及改回来，浏览器还在往一个死掉的代理发请求。
    """
    srv = str(_reg_get("ProxyServer") or "")
    enable = _reg_get("ProxyEnable")
    if GRABBER_PROXY_PORT in srv and int(enable or 0) == 1:
        import winreg
        _reg_set("ProxyEnable", 0, winreg.REG_DWORD)      # 必须用 REG_DWORD(4)，写错类型不生效
        _notify_proxy_changed()
        print("  [代理急救] 检测到系统代理还指向抓包工具的 127.0.0.1:8827（工具已退出），"
              "已自动关闭系统代理，网络恢复正常。")
        return True
    if verbose:
        print(f"  [代理检查] 当前 ProxyEnable={enable} ProxyServer={srv or '(空)'} —— 没有残留。")
    return False


# ── 向导 ─────────────────────────────────────────────────────────────

def ask_questions(old: dict) -> dict:
    cfg = dict(old)
    print()
    print("=" * 62)
    print(" 弹幕转发向导 —— 跟着选就行，选完自动开始抓取并推送给数字人")
    print("=" * 62)

    p_default = cfg.get("platform", "douyin")
    platform = ask_choice(
        "① 这次要抓哪个平台的弹幕？",
        {"1": ("抖音直播（需要抓包工具，会弹一次 UAC）", "douyin"),
         "2": ("淘宝直播（不需要任何工具，最省事）", "taobao"),
         "3": ("抖音 + 淘宝 一起抓好烦", "both")},
        {"douyin": "1", "taobao": "2", "both": "3"}.get(p_default, "1"))
    cfg["platform"] = platform

    mode = cfg.get("douyin_mode", "companion")
    if platform in ("douyin", "both"):
        mode = ask_choice(
            "② 抖音用哪种方式抓？",
            {"1": ("直播伴侣 —— 我自己开播时用（不开浏览器、不动系统代理，推荐）", "companion"),
             "2": ("网页/浏览器 —— 抓别人的直播间（⚠️ 会临时改系统代理；退出时自动恢复，"
                   "万一没恢复就双击「急救-网页打不开就双击我.bat」）", "browser")},
            {"companion": "1", "browser": "2"}.get(mode, "1"))
    cfg["douyin_mode"] = mode

    if platform in ("taobao", "both"):
        cfg["taobao_live_id"] = ask_room_id(
            "③ 淘宝直播间的号（liveId）是多少？",
            "打开直播间，地址栏里 liveId= 后面那串数字；整条链接粘进来也行")
    if platform in ("douyin", "both") and mode == "browser":
        cfg["douyin_room"] = ask_room_id(
            "③ 要抓的抖音直播间是哪个？",
            "形如 https://live.douyin.com/264865537245 —— 粘整条链接最省事")

    default_server = cfg.get("server") or "http://127.0.0.1:8063"
    print()
    print("④ 数字人（LiveTalking）跑在哪台机器上？")
    print("    数字人和这个窗口在同一台电脑 → 直接回车")
    print("    数字人在别的电脑/服务器上   → 输入 http://那台机器的IP:8063")
    raw = input(f"  服务器地址（回车={default_server}）: ").strip()
    cfg["server"] = raw or default_server

    cfg["room_key"] = ask_room(cfg)

    save_cfg(cfg)
    print()
    print(f"  ✔ 配置已记住：{CFG_PATH}")
    return cfg


def print_summary(cfg: dict):
    print()
    print("=" * 62)
    print(" 本次配置")
    print(f"   平台     : {PLATFORM_LABEL.get(cfg.get('platform'), cfg.get('platform'))}")
    if cfg.get("platform") in ("douyin", "both"):
        print(f"   抖音方式 : {MODE_LABEL.get(cfg.get('douyin_mode'), cfg.get('douyin_mode'))}")
        if cfg.get("douyin_mode") == "browser" and cfg.get("douyin_room"):
            print(f"   抖音房间 : {cfg['douyin_room']}")
    if cfg.get("platform") in ("taobao", "both"):
        print(f"   淘宝房间 : liveId={cfg.get('taobao_live_id', '')}")
    print(f"   服务器   : {cfg.get('server')}")
    print(f"   房间标识 : {cfg.get('room_key') or '(默认房间)'}")
    print("=" * 62)


def confirm_reuse(cfg: dict) -> bool:
    print()
    print("-" * 62)
    print(" 上次用的配置：")
    print(f"   平台：{PLATFORM_LABEL.get(cfg.get('platform'), cfg.get('platform'))}"
          + (f"（{MODE_LABEL.get(cfg.get('douyin_mode'), '')}）"
             if cfg.get("platform") in ("douyin", "both") else ""))
    print(f"   服务器：{cfg.get('server')}    房间：{cfg.get('room_key') or '默认'}")
    raw = input(" 直接用这份配置开始吗？[回车=开始 / n=重新选]: ").strip().lower()
    return raw not in ("n", "no")


# ── 主流程 ───────────────────────────────────────────────────────────

def build_args(cfg: dict) -> argparse.Namespace:
    return argparse.Namespace(
        server=cfg.get("server") or "http://127.0.0.1:8063",
        token=cfg.get("token", ""),
        key=cfg.get("room_key", ""),
        source=cfg.get("platform", "douyin"),
        relay=cfg.get("relay", "ws://127.0.0.1:8888"),
        dialect="auto",
        live_id=str(cfg.get("taobao_live_id", "") or ""),
        interval=float(cfg.get("interval", 3) or 3),
        replay_backlog=int(cfg.get("replay_backlog", 0) or 0),
    )


def run(cfg: dict) -> int:
    args = build_args(cfg)
    url = F.build_url(args.server)
    headers = {"Content-Type": "application/json"}
    if args.token:
        headers["X-Danmaku-Token"] = args.token
    if args.key:
        headers["X-Room-Key"] = args.key

    source = args.source
    need_grabber = source in ("douyin", "relay", "both") and os.path.exists(GRABBER_EXE) \
        and cfg.get("douyin_mode") != "off"

    print_summary(cfg)

    proxy_snap = None
    if need_grabber:
        if not _is_admin():
            print()
            print("  抖音抓包工具需要管理员权限（要挂钩直播伴侣/浏览器）。")
            print("  马上会弹一个 UAC 窗口 —— 请点「是」。")
            if not _elevate_self([os.path.abspath(__file__), "--elevated"]):
                print("  ✗ 没有拿到管理员权限，抖音这条抓不了。")
                print("    （淘宝那条不需要管理员权限，可以把平台改成「淘宝直播」再试）")
                pause()
                return 1
            return 0
        mode = cfg.get("douyin_mode", "companion")
        print()
        print(f"  正在静默启动抖音抓包工具（{MODE_LABEL.get(mode, mode)}）...")
        # 浏览器模式会改系统代理 → 先把原设置存下来，退出时恢复；被强杀也能下次急救
        proxy_snap = proxy_snapshot() if mode == "browser" else None
        if proxy_snap is not None:
            print("  （这个模式会临时改系统代理，我退出时会自动帮你改回来）")
            # 再上一道保险：就算脚本被异常终止，也尽量把代理恢复原样
            import atexit
            atexit.register(lambda s=proxy_snap: proxy_restore(s))
        if start_grabber(mode) is None:
            pause()
            return 1
        print("  等 6 秒让它接上弹幕通道...")
        time.sleep(6)
        if mode == "browser":
            room = str(cfg.get("douyin_room") or "").strip()
            if room:
                target = room if room.startswith("http") else f"https://live.douyin.com/{room}"
                print(f"  正在用默认浏览器打开直播间：{target}")
                try:
                    import webbrowser
                    webbrowser.open(target)
                except Exception:
                    print("  （浏览器没打开成功，自己手动打开那个直播间就行）")
            print("  ★ 那个直播间网页要保持开着，关掉网页就抓不到了。")
        else:
            print("  ★ 请确认抖音直播伴侣已经开始直播（没开播就抓不到弹幕）。")

    forwarder = F.Forwarder(url, headers)
    stop = threading.Event()
    threads = []
    if source in ("douyin", "relay", "both"):
        threads.append(threading.Thread(target=F.relay_source,
                                        args=(args, forwarder, stop), name="relay", daemon=True))
    if source in ("taobao", "both"):
        if not args.live_id:
            print("  [错误] 淘宝直播需要直播间号（liveId），请重新运行向导填写。")
            stop_grabber()
            pause()
            return 1
        threads.append(threading.Thread(target=F.taobao_source,
                                        args=(args, forwarder, stop), name="taobao", daemon=True))
    for t in threads:
        t.start()

    print()
    print("=" * 62)
    print(" 开始工作了 —— 这个窗口别关，按 Ctrl+C 停止")
    print(f"   推送到 : {url}")
    print("   抓到弹幕会这样显示：[收到] 弹幕 昵称: 内容")
    print("=" * 62)
    t_report = time.time()
    try:
        while True:
            time.sleep(0.5)
            forwarder.flush()
            if time.time() - t_report >= 60:
                t_report = time.time()
                print(forwarder.stats_line())
    except KeyboardInterrupt:
        print()
        print("  正在停止...")
    finally:
        stop.set()
        forwarder.flush()
        if need_grabber:
            stop_grabber()
        if proxy_snap:
            proxy_restore(proxy_snap)      # 把系统代理改回原样（本来没开就还是关着）
            print("  系统代理已恢复原状。")
        print(f"  已停止：共转发 {forwarder.sent} 条，失败 {forwarder.fails} 次")
    return 0


def main() -> int:
    # 行缓冲：不然输出会卡在缓冲区里，看着像"没反应"
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="弹幕转发向导")
    ap.add_argument("--elevated", action="store_true", help="内部用：已提权，别重复提问")
    ap.add_argument("--reset", action="store_true", help="忘掉上次的选择")
    ap.add_argument("--fix-proxy", action="store_true",
                    help="急救：退出抓包工具后网页打不开/断网时，运行这个把系统代理关掉")
    ap.add_argument("--yes", action="store_true", help="不问，直接用记住的配置")
    # 高级用法：直接给参数就跳过向导（兼容老的命令行方式）
    ap.add_argument("--server")
    ap.add_argument("--source", choices=("douyin", "relay", "taobao", "both"))
    ap.add_argument("--live-id")
    ap.add_argument("--key")
    ap.add_argument("--token")
    args, _unknown = ap.parse_known_args()

    if args.fix_proxy:
        print()
        print("=" * 62)
        print(" 系统代理急救 —— 退出抓包工具后网页打不开时用这个")
        print("=" * 62)
        if not fix_proxy_residue(verbose=True):
            print("  没发现抓包工具留下的代理残留。")
            print("  如果还是上不了网，手动检查：设置 → 网络和 Internet → 代理 → 关掉『使用代理服务器』")
        pause()
        return 0

    fix_proxy_residue()          # 上次被强杀留下的残留，开机先清掉

    if args.reset and os.path.exists(CFG_PATH):
        os.remove(CFG_PATH)
        print("  已清除上次记住的配置。")

    cfg = load_cfg()

    # 命令行直接给全了 → 跳过向导
    if args.server and args.source:
        cfg = {"platform": args.source, "server": args.server,
               "room_key": args.key or "", "token": args.token or "",
               "taobao_live_id": args.live_id or "", "douyin_mode": "companion"}
        return run(cfg)

    if args.elevated or args.yes:
        if not cfg:
            print("  [错误] 没有记住的配置，请先正常运行一次向导。")
            pause()
            return 1
        return run(cfg)

    try:
        if cfg and confirm_reuse(cfg):
            return run(cfg)
        cfg = ask_questions(cfg)
        return run(cfg)
    except KeyboardInterrupt:
        print("\n  已取消。")
        return 0
    except Exception as e:
        import traceback
        print(f"\n  [出错] {e}")
        traceback.print_exc()
        pause()
        return 1


if __name__ == "__main__":
    sys.exit(main())
