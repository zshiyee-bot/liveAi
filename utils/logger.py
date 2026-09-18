"""统一日志：文件直写 + 控制台走独立线程。

===============================================================================
为什么必须这样（血泪教训，2026-09-18 实测）
===============================================================================
本项目的日志会**直接从业务线程写控制台**（root logger 上有 StreamHandler）。
Windows 命令行窗口只要进入「选择 / 快速编辑」状态（用户在窗口里拖选过文字、
甚至只是点了一下），**操作系统会挂住该进程往控制台写入的调用**。于是：

  1. 该线程握着 logging handler 的锁不放（py-spy 栈：MainThread 卡在
     `logging/__init__.py` 的 StreamHandler.emit → stream.write）；
  2. 其它任何线程只要打一条日志，就会卡在 `Handler.acquire`
     （py-spy 栈：media-player 线程卡在 `acquire`，来自
     `avatars/base_avatar.py` 的 render 里那条 warning）；
  3. 渲染线程、推理线程、process_frames、aiortc 事件循环（HTTP + WebRTC 发送）
     全部连锁阻塞 → **服务彻底假死**：页面无响应、`/api/libs` 超时、
     GPU 只有 6%（不是在算，是在等）、日志停在最后一行。
  4. 点一下 cmd 窗口按 Esc 取消选择态，服务立刻恢复 —— 这就是它的指纹。

修法：业务线程只把日志记录 `put_nowait` 进内存队列（永不阻塞），由**独立守护
线程**去写控制台。控制台再卡，也只卡那一个线程；文件与业务全部照常。
（`logger.propagate = False` + root 上的 StreamHandler 被换成队列版，
 双保险，见 `install_safe_console_logging()`。）

逃生开关：`LT_CONSOLE_LOG=0` → 完全不输出到控制台（只写文件）。
===============================================================================
"""
import atexit
import logging
import logging.handlers
import os
import queue
import sys
import threading
import time

formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')


class _DropOldestQueue(queue.Queue):
    """队列满时丢最旧一条，绝不阻塞业务线程（控制台输出丢几条不影响功能）。"""

    def __init__(self, maxsize=5000):
        super().__init__(maxsize=maxsize)
        self.dropped = 0

    def enqueue(self, record):
        while True:
            try:
                self.put_nowait(record)
                return
            except queue.Full:
                try:
                    self.get_nowait()
                    self.dropped += 1
                except queue.Empty:
                    return


class _SafeStreamHandler(logging.StreamHandler):
    """写控制台出错（编码问题/管道断开/控制台被关）不能把监听线程搞死。"""

    def emit(self, record):
        try:
            super().emit(record)
        except Exception:
            pass


# ── 文件日志：直写 ──────────────────────────────────────────────────────────
# 磁盘写入几乎不会挂；而文件是排障的唯一可靠记录（进程被强杀时 stderr 会丢），
# 所以**不走队列**，保证每条都落盘。
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
# 关键：绝不把记录再交给 root —— root 上可能有 basicConfig()/第三方库塞的
# 直写控制台的 StreamHandler（那正是上面死锁的入口）。
logger.propagate = False
if not any(isinstance(h, logging.FileHandler) for h in logger.handlers):
    fhandler = logging.FileHandler('livetalking.log', encoding="utf-8")
    fhandler.setFormatter(formatter)
    fhandler.setLevel(logging.INFO)
    logger.addHandler(fhandler)

# ── 控制台日志：队列 + 独立守护线程 ─────────────────────────────────────────
_console_queue = None
_console_listener = None
_console_handler = None
_queue_handler = None


def _sanitize_root(qh):
    """把 root 上「直写控制台」的 handler 换成队列版（死锁的唯一入口）。"""
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
            root.removeHandler(h)
    if qh not in root.handlers:
        root.addHandler(qh)
    if root.level in (logging.WARNING, logging.NOTSET):
        root.setLevel(logging.INFO)


def _stop_console_listener():
    global _console_listener
    try:
        if _console_listener is not None:
            _console_listener.stop()
    except Exception:
        pass
    _console_listener = None


def _console_watchdog():
    """看门狗：控制台被系统挂住（cmd 窗口「选择/快速编辑」态）时，往文件里留一条
    可解释的证据 —— 否则现象是「窗口不动了、日志不滚了」，下次又要靠 py-spy 抓栈。

    只写文件（那条 warning 自己也会进队列，但文件是直写的），所以控制台卡着也有效。
    """
    last = -1
    stuck = 0
    warned = False
    while True:
        try:
            time.sleep(2.0)
            q = _console_queue
            if q is None:
                continue
            n = q.qsize()
            if n > 0 and n == last:
                stuck += 1
            else:
                stuck = 0
                warned = False
            last = n
            if stuck >= 3 and not warned:          # 约 6 秒没有任何消费 = 控制台被挂住
                warned = True
                logger.warning(
                    "[logger] 控制台输出被挂住了（队列积压 %d 条 ≥6 秒没被消费）。"
                    "最常见原因：命令行窗口处于「选择/快速编辑」状态 —— "
                    "点一下那个窗口、按 Esc 即可恢复。**服务本身不受影响**："
                    "渲染/推理/推流照常，日志继续写 livetalking.log。", n)
        except Exception:
            pass


def install_safe_console_logging():
    """安装「控制台输出走独立线程」的 handler（幂等，可重复调用）。

    必须在日志产生前调用一次（本模块 import 时就调用；`app.py` 启动前再调一次
    也无害）。若 root 上已经有直写控制台的 StreamHandler（例如
    `server/webrtc.py` 里的 `logging.basicConfig()`），会被替换成队列版。
    """
    global _console_queue, _console_listener, _console_handler, _queue_handler
    if os.getenv('LT_CONSOLE_LOG', '1') == '0':
        return                      # 逃生开关：完全不输出到控制台
    if _console_listener is not None:
        # 已经装过：仍然再清一次 root —— 第三方库可能在 import 时又塞了
        # 一个直写控制台的 StreamHandler 进来（那就是死锁入口）。
        if _queue_handler is not None:
            _sanitize_root(_queue_handler)
        return

    _console_handler = _SafeStreamHandler(sys.stdout)
    _console_handler.setFormatter(formatter)
    _console_handler.setLevel(logging.INFO)

    _console_queue = _DropOldestQueue()
    qh = logging.handlers.QueueHandler(_console_queue)
    qh.setLevel(logging.INFO)       # DEBUG 不进队列，少折腾（文件里仍有 DEBUG）
    _queue_handler = qh

    _console_listener = logging.handlers.QueueListener(
        _console_queue, _console_handler, respect_handler_level=True)
    _console_listener.daemon = True
    _console_listener.start()
    atexit.register(_stop_console_listener)

    # 看门狗：控制台被挂住时往文件里留证据（见函数说明）
    _wd = threading.Thread(target=_console_watchdog, name='log-console-watchdog', daemon=True)
    _wd.start()

    # 本模块的 logger：文件 + 队列版控制台
    logger.addHandler(qh)

    # root：把 basicConfig()/第三方库塞进来的「直写控制台」handler 换成队列版。
    # 注意：这里是死锁的真正入口，必须清掉。
    _sanitize_root(qh)
    # 留一行运行期自证（写进文件日志），便于排障时确认这层防护确实生效
    logger.info("[logger] 控制台输出已切到独立线程（root handlers=%s，控制台再卡也不影响服务）",
                [type(h).__name__ for h in logging.getLogger().handlers])


install_safe_console_logging()
