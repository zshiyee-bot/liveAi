
import os
import time
import glob
import itertools
import threading
import cv2
import numpy as np
from tqdm import tqdm
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from utils.logger import logger


def imwrite_u(path, img, params=None):
    """UTF-8 路径安全的图像写入（替代 cv2.imwrite）。

    ⚠️ 为什么需要这个函数：
      Windows 上 OpenCV 的 cv2.imwrite() **无法写入含非 ASCII 字符的路径**
      （中文/日文/emoji 等）。它不抛异常，只**静默返回 False**，
      导致「任务显示成功但一个文件都没有」这种极难排查的问题。
      实测：
          cv2.imwrite("./data/avatars/_ascii/full_imgs/0.png", img) -> True
          cv2.imwrite("./data/avatars/数字人A/.../0.png", img)      -> False  ← 无任何报错

      本函数用 cv2.imencode 编码到内存，再用内置 open() 写文件，
      open() 走的是 Windows 宽字符 API，中文路径完全正常。
      （PIL 也可以，但 cv2.imencode 不引入额外依赖且与 OpenCV 编码参数一致。）

    返回 True/False，语义与 cv2.imwrite 一致。
    """
    try:
        ext = os.path.splitext(path)[1] or '.png'
        ok, buf = cv2.imencode(ext, img, params or [])
        if not ok:
            return False
        # 确保父目录存在（上游多处未建目录，cv2.imwrite 也是静默失败）
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, 'wb') as f:
            f.write(buf.tobytes())
        return True
    except Exception:
        return False


def imread_u(path, flags=cv2.IMREAD_COLOR):
    """UTF-8 路径安全的图像读取（替代 cv2.imread）。

    cv2.imread() 在 Windows 上同样无法读取含非 ASCII 的路径（静默返回 None）。
    用 np.fromfile + cv2.imdecode 绕过。
    """
    try:
        data = np.fromfile(path, dtype=np.uint8)
        if data.size == 0:
            return None
        return cv2.imdecode(data, flags)
    except Exception:
        return None

# def read_imgs(img_list):
#     frames = []
#     logger.info('reading images...')
#     for img_path in tqdm(img_list):
#         frame = cv2.imread(img_path)
#         frames.append(frame)
#     return frames

def read_imgs(img_list):
    def load_image(index, img_path):
        # 用 imread_u 而非 cv2.imread：中文路径下 cv2.imread 静默返回 None，
        # 会让后续 face_list_cycle[0].shape 炸出难懂的 IndexError/AttributeError。
        img = imread_u(img_path)
        if img is None:
            img = cv2.imread(img_path)   # 兜底（非 ASCII 环境外无差别）
        return index, img

    frames = [None] * len(img_list)  # Initialize a list with the same length as img_list
    # max_workers=4：上游不设上限会让 ThreadPoolExecutor 按 CPU 核数开线程，
    # 而每张 PNG 解码都要一次内存分配 + 一次 numpy copy，线程过多反而抢 GIL/带宽。
    # 实测 4 路是最稳的（再高收益递减，且 1080p 长素材下瞬时内存占用明显变大）。
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(load_image, idx, img_path): idx for idx, img_path in enumerate(img_list)}
        for future in tqdm(as_completed(futures), total=len(img_list)):
            idx, img = future.result()
            frames[idx] = img
    return frames

def sorted_imgs(d):
    """取目录里的图片并按文件名数字排序（素材帧的命名就是帧号）。"""
    fs = glob.glob(os.path.join(d, '*.[jpJP][pnPN]*[gG]'))
    try:
        return sorted(fs, key=lambda x: int(os.path.splitext(os.path.basename(x))[0]))
    except ValueError:
        # 文件名不是纯数字的目录：退回字典序，不要在这里抛异常
        return sorted(fs)


def mirror_index(size, index):
    # 顺序循环：播到末尾后回到第 0 帧从头继续。
    #
    # 上游原实现是「乒乓绕回」——播完倒着播回来：
    #     turn = index // size; res = index % size
    #     return res if turn % 2 == 0 else size - res - 1
    # 该行为会让动作倒放（抬手 → 落手 → 抬手），对「多段头尾相连」的
    # 链式素材是灾难：素材一旦走到段尾，全部帧被倒序播放。
    # 若确实需要乒乓效果，请自行准备首尾相接的素材，不要改回此处。
    return index % size


# ── 惰性帧序列：让内存占用与「素材总帧数」解耦 ────────────────────────────────
#
# 背景（实测）：read_imgs() 把每一帧都解码成 ndarray 常驻内存，1 帧 1080p 就是
#   1920*1080*3 = 5.93MB。于是 50s@25fps = 1250 帧 = 7.24GB/段，
#   30 段素材链 = 224GB，而素材链是「所有段同时常驻」的（_load_segments 循环里
#   每段都 read_imgs，然后挂到 self.playlist 上整场直播不放）。
#   608x480 这种小图也救不了：37500 帧 * 0.88MB = 33GB。
#
# 做法：给一个「看起来就是 list[np.ndarray]」的惰性序列 ——
#   - __len__      : 不解码（下游 _seg_len / len(frame_list_cycle) 照用）
#   - frames[i]    : 命中全局缓存直接返回，未命中同步解码并插入
#   - frames[:n]   : 返回惰性视图（**不会**真的解码 n 帧，否则等于没省）
#
# ⚠️ 缓存必须是**全局共享**的，不能每段一份：素材链有 N 段就有 N 个惰性序列，
#    如果各自持有一个 1GB 滑窗，30 段就是 30GB —— 等于没解决问题。
#    所以这里用「一个模块级全局缓存 + 全局字节预算 + 全局 LRU」，总内存恒定为
#    LT_FRAME_CACHE_MB，与段数、总帧数都无关。
#
# 为什么可以这样：消费端是**单生产者、严格顺序 + 回绕**（mirror_index 顺序递增、
# get_frame_index() 游标只前进）。帧数组虽然会被推理线程与渲染线程各读一次，
# 但两者按同一顺序、且被 res_frame_queue(maxsize=batch*2) 限制在 ~32 帧以内，
# 所以「全局 LRU + 顺序预取」足够覆盖，不会退化成每次同步解码。
#
# 只给「最近被读过的段」预取（_ACTIVE_WINDOW 秒内）：素材链同时只有一段在播，
# 让非活跃段继续预取只会把它自己的帧塞进全局缓存、再把活跃段的帧挤出去（抖动）。
# 切段后新段是冷的，第一帧同步解码（1080p 约 3ms）后后台以 ~350 帧/秒回填，
# 0.5s 内就把窗口追平，25fps 下无感。
#
# 可调：LT_EAGER_BUDGET_MB（整段小于它就还是全量读，短素材行为完全不变）
#       LT_FRAME_CACHE_MB（全局缓存预算上限）
_POOL_LOCK = threading.Lock()
_POOL = None
_POOL_WORKERS = 4

# 全局帧缓存：key = (store_key, 帧下标) -> ndarray，靠 move_to_end 当 LRU
_POOL_CACHE = OrderedDict()
_POOL_BYTES = 0
_POOL_CACHE_LOCK = threading.Lock()
_KEY_SEQ = itertools.count(1)
_BUDGET_CACHE = {}
_STATS = {'hits': 0, 'misses': 0}      # 累计命中/未命中（诊断用）
_ACTIVE_WINDOW = 3.0        # 秒：超过这么久没被读过的段停止预取


def _pool():
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            _POOL = ThreadPoolExecutor(max_workers=_POOL_WORKERS,
                                       thread_name_prefix='frame_decode')
        return _POOL


def _cache_budget():
    """全局缓存字节预算（读一次 env 后缓存）。"""
    if 'bytes' not in _BUDGET_CACHE:
        _BUDGET_CACHE['bytes'] = _env_int('LT_FRAME_CACHE_MB', 1024) * 1024 * 1024
    return _BUDGET_CACHE['bytes']


def _pool_put(key, img):
    """插入全局缓存并按全局字节预算淘汰最久未用的帧。调用方须持 _POOL_CACHE_LOCK。"""
    global _POOL_BYTES
    old = _POOL_CACHE.pop(key, None)
    if old is not None:
        _POOL_BYTES -= old.nbytes
    _POOL_CACHE[key] = img
    _POOL_BYTES += img.nbytes
    budget = _cache_budget()
    # 至少留 1 帧（单帧就超预算的极端情况不能把刚放进来的自己淘汰掉）
    while _POOL_BYTES > budget and len(_POOL_CACHE) > 1:
        _, ev = _POOL_CACHE.popitem(last=False)
        _POOL_BYTES -= ev.nbytes


def _pool_drop_store(store_key):
    """丢弃某个 store 的全部缓存帧（素材链热重载 / 会话关闭时用）。"""
    global _POOL_BYTES
    with _POOL_CACHE_LOCK:
        for k in [k for k in _POOL_CACHE if k[0] == store_key]:
            _POOL_BYTES -= _POOL_CACHE.pop(k).nbytes


def frame_cache_stats():
    """给诊断/测试用：当前全局缓存帧数、字节数与累计命中率。"""
    with _POOL_CACHE_LOCK:
        n = len(_POOL_CACHE)
        b = _POOL_BYTES
    tot = _STATS['hits'] + _STATS['misses']
    return {'frames': n, 'bytes': b, 'budget': _cache_budget(),
            'hits': _STATS['hits'], 'misses': _STATS['misses'],
            'hit_rate': (_STATS['hits'] / tot) if tot else None}


def reset_frame_cache_stats():
    _STATS['hits'] = 0
    _STATS['misses'] = 0


def _env_int(name, default):
    try:
        return int(os.environ.get(name, '') or default)
    except (TypeError, ValueError):
        return default


def _decode(path):
    """单帧解码（UTF-8 路径安全），沿用 read_imgs 的双保险策略。"""
    img = imread_u(path)
    if img is None:
        img = cv2.imread(path)
    return img


class _FrameStore:
    """按路径下标惰性解码，共享全局帧缓存 + 顺序后台预取。线程安全。

    注意：缓存不在本对象里，而是提交给模块级 _POOL_CACHE（全局字节预算 + LRU）。
    这样 30 段素材链的总内存 = LT_FRAME_CACHE_MB，而不是 30 × 每段滑窗。
    """

    def __init__(self, paths, frame_bytes, budget_bytes=None):
        self.paths = paths
        self.n = len(paths)
        self.frame_bytes = max(1, int(frame_bytes))
        self.key = next(_KEY_SEQ)
        self.budget = int(budget_bytes or _cache_budget())
        # 预取深度：单个活跃段最多用掉全局预算的一半（另一半留给回看区与其它段）
        self.ahead = max(16, min(256, int(self.budget // self.frame_bytes) // 2))
        self._lock = threading.Lock()
        self._pending = set()           # 已提交给池、还没落地的下标
        self._last_i = -1               # 最近一次被请求的下标
        self._pf = 0                    # 预取游标（段内下标）
        self._last_access = 0.0
        self.closed = False
        self.hits = 0
        self.misses = 0

    # 消费者入口
    def get(self, i):
        k = (self.key, i)
        with _POOL_CACHE_LOCK:
            img = _POOL_CACHE.get(k)
            if img is not None:
                _POOL_CACHE.move_to_end(k)
        if img is not None:
            self.hits += 1
            _STATS['hits'] += 1
            self._advance(i)
            return img
        # 未命中：同步解码（切段后的头几帧会走到这里，1080p 约 3ms/帧）
        self.misses += 1
        _STATS['misses'] += 1
        img = _decode(self.paths[i])
        if img is not None:
            with _POOL_CACHE_LOCK:
                _pool_put(k, img)
        self._advance(i)
        return img

    def _advance(self, i):
        """记录访问位置并补足预取任务。"""
        with self._lock:
            if i < self._last_i:
                # 回绕（段播完回到第 0 帧）或渲染线程回读：把预取游标拉回来。
                # 已在全局缓存里的下标会被 _topup 跳过，所以回拉几乎没有代价。
                # 同时把 _last_i 拉回 i：否则回绕后 target 会一直停在段尾，
                # 每次循环都要把整段重新预取一遍（纯浪费 CPU）。
                self._pf = min(self._pf, i)
                self._last_i = i
            self._last_i = max(self._last_i, i)
            self._last_access = time.time()
            self._topup_locked()

    def _topup_locked(self):
        if self.closed:
            return
        # 非活跃段不预取：同时只有一段在播，替不播的段预取只会把活跃段的帧挤出全局缓存
        if time.time() - self._last_access > _ACTIVE_WINDOW:
            return
        target = min(self.n, self._last_i + 1 + self.ahead)
        while self._pf < target and len(self._pending) < self.ahead:
            j = self._pf
            self._pf += 1
            if j in self._pending:
                continue
            with _POOL_CACHE_LOCK:
                if (self.key, j) in _POOL_CACHE:
                    continue
            self._pending.add(j)
            try:
                _pool().submit(self._decode_into, j)
            except RuntimeError:
                # 解释器关闭时线程池已停，静默放弃预取即可
                self._pending.discard(j)
                return

    def _decode_into(self, i):
        img = _decode(self.paths[i])
        with self._lock:
            self._pending.discard(i)
            if self.closed:
                return
            if img is not None:
                with _POOL_CACHE_LOCK:
                    _pool_put((self.key, i), img)
            self._topup_locked()

    def close(self):
        with self._lock:
            self.closed = True
            self._pending.clear()
        _pool_drop_store(self.key)

    def __del__(self):
        # 对象被回收（会话关闭 / 素材链热重载换段）时，把它在全局缓存里的帧交还，
        # 否则这些帧要等 LRU 自然淘汰才让出预算。解释器关闭阶段模块可能已拆掉，忽略异常。
        try:
            _pool_drop_store(self.key)
        except Exception:
            pass


class LazyFrames:
    """list[np.ndarray] 的惰性替身：支持 len() / [i] / [a:b]，不整体解码。

    切片返回**惰性视图**（共享同一个 _FrameStore），所以上游
    `frames[:n]`、`segs.append((frames[:n], ...))` 这些写法不用改也不会爆内存。
    """

    def __init__(self, paths, frame_bytes=None, _store=None, _offset=0, _n=None):
        if _store is None:
            paths = list(paths)
            self._paths = paths
            self._store = _FrameStore(paths, frame_bytes or 1)
            self._offset = 0
            self._n = len(paths)
        else:
            self._paths = None
            self._store = _store
            self._offset = _offset
            self._n = _n

    @classmethod
    def _view(cls, store, offset, n):
        return cls(None, _store=store, _offset=offset, _n=n)

    @property
    def store(self):
        return self._store

    def __len__(self):
        return self._n

    def __getitem__(self, key):
        if isinstance(key, slice):
            start, stop, step = key.indices(self._n)
            if step == 1:
                return LazyFrames._view(self._store, self._offset + start,
                                        max(0, stop - start))
            return [self[i] for i in range(start, stop, step)]
        i = key
        if i < 0:
            i += self._n
        if not 0 <= i < self._n:
            raise IndexError(f'LazyFrames index out of range: {key}')
        return self._store.get(self._offset + i)

    def __iter__(self):
        for i in range(self._n):
            yield self[i]

    def close(self):
        if self._paths is not None:      # 只有「根」持有 store 的生命周期
            self._store.close()


def load_frames(img_list, eager_budget_mb=None, mode=None):
    """自适应加载：小段全量读（零延迟、行为与上游一致），大段走惰性滑窗。

    阈值按「整段解码后的字节数」判断，而不是帧数 —— 1080p 的 300 帧（1.8GB）
    和 480p 的 2000 帧（1.8GB）一样重，按帧数判断会误判。

    mode: None=按本段自己判断；'eager'/'lazy'=由调用方（load_frames_batch）指定。
    """
    paths = list(img_list)
    if not paths:
        return []
    if eager_budget_mb is not None:
        eager_budget = int(eager_budget_mb) * 1024 * 1024
    else:
        eager_budget = _env_int('LT_EAGER_BUDGET_MB', 4096) * 1024 * 1024

    first = _decode(paths[0])
    if first is None:
        # 首帧就解不出来（路径错/文件损坏）：退回上游行为，让它在原处报错
        return read_imgs(paths)
    frame_bytes = int(first.nbytes)
    total_bytes = frame_bytes * len(paths)

    if mode is None:
        mode = 'eager' if total_bytes <= eager_budget else 'lazy'

    if mode == 'eager':
        # frame 0 已经解好了，避免重复解码
        rest = read_imgs(paths[1:]) if len(paths) > 1 else []
        return [first] + rest

    lazy = LazyFrames(paths, frame_bytes=frame_bytes)
    logger.info(
        f'惰性帧加载：{len(paths)} 帧 {first.shape[1]}x{first.shape[0]} '
        f'({frame_bytes / 1048576:.2f}MB/帧，整段 {total_bytes / 2 ** 30:.2f}GB) '
        f'预取深度 {lazy.store.ahead} 帧，全局缓存预算 '
        f'{_cache_budget() / 2 ** 30:.2f}GB'
    )
    return lazy


def probe_frames_bytes(img_list):
    """只解码首帧，估算「这个图片序列全部读进内存要多少字节」（不解码其余）。

    返回 (frame_bytes, seq_bytes)；空列表或首帧损坏返回 (0, 0)。
    """
    paths = list(img_list)
    if not paths:
        return 0, 0
    first = _decode(paths[0])
    if first is None:
        return 0, 0
    return int(first.nbytes), int(first.nbytes) * len(paths)


def load_frames_batch(img_lists, eager_budget_mb=None):
    """按**整体**（整条素材链）决定 eager/lazy，返回等长的序列列表。

    ⚠️ 为什么不能每段各判各的：单段 368 帧 688x1312 只有 0.93GB，按段判断会走
    全量；但素材链有 30 段时就是 28GB —— 每段都「没超阈值」，整体却爆了。
    所以必须先把各段的首帧探一遍，用**总字节数**做一次决定。
    """
    lists = [list(p) for p in img_lists]
    if eager_budget_mb is not None:
        budget = int(eager_budget_mb) * 1024 * 1024
    else:
        budget = _env_int('LT_EAGER_BUDGET_MB', 4096) * 1024 * 1024
    total = 0
    for p in lists:
        total += probe_frames_bytes(p)[1]
    mode = 'eager' if total <= budget else 'lazy'
    if len(lists) > 1 and mode == 'lazy':
        logger.info(f'素材链整体 {total / 2 ** 30:.2f}GB 超过 {budget / 2 ** 30:.2f}GB '
                    f'预算 -> {len(lists)} 段全部改用惰性加载')
    return [load_frames(p, mode=mode) for p in lists]

# ─────────────────────────────────────────────────────────────────────────────
# 【中文/非 ASCII 路径 —— 全局兜底】把 cv2.imread / cv2.imwrite 换成 UTF-8 安全版本
#
# 为什么用「全局替换」而不是逐个改调用点：
#   全项目（含 musetalk / wav2lip 自带的 face_detection 第三方代码、ultralight）还有
#   20+ 处直接调用 cv2.imread / cv2.imwrite。逐个改既容易漏，又会在下次同步上游时被
#   revert 覆盖；而这两个函数的语义完全可以等价替换，所以在模块导入时换一次即可。
#
# 实测（本机 Windows + opencv，2026-09-17）：
#   cv2.imread(".../中文库/帧_0001.png")   -> None    ← 静默失败
#   cv2.imwrite(".../中文库/x.png", img)   -> False   ← 静默失败
#   cv2.VideoCapture(".../中文库/视频.mp4") -> 正常打开（688x1312，连续读 301 帧）
#   → 所以只有 imread / imwrite 需要兜底，VideoCapture / VideoWriter 不用动。
#
# 语义保证（与 cv2 原函数一致，替换零风险）：
#   读失败返回 None、写失败返回 False、都不抛异常（imread_u / imwrite_u 已包 try）。
#   非路径入参（numpy 数组等 cv2 允许的形态）原样交还 cv2 原实现，行为不变。
# 幂等：重复 import / reload 不会重复包裹。
# 逃生开关：设环境变量 LT_NO_CV2_PATCH=1 可临时停用（对照排查用）。
# ─────────────────────────────────────────────────────────────────────────────
_CV2_IMREAD_ORIG = cv2.imread
_CV2_IMWRITE_ORIG = cv2.imwrite


def _cv2_imread_u(path, flags=cv2.IMREAD_COLOR):
    """cv2.imread 的 UTF-8 路径安全替身。"""
    if isinstance(path, (str, bytes, os.PathLike)):
        return imread_u(path, flags)
    return _CV2_IMREAD_ORIG(path, flags)


def _cv2_imwrite_u(path, img, params=None):
    """cv2.imwrite 的 UTF-8 路径安全替身。"""
    if isinstance(path, (str, bytes, os.PathLike)):
        return imwrite_u(path, img, params)
    return _CV2_IMWRITE_ORIG(path, img, params)


if not os.environ.get('LT_NO_CV2_PATCH'):
    _cv2_imread_u._lt_unicode_safe = True
    _cv2_imwrite_u._lt_unicode_safe = True
    cv2.imread = _cv2_imread_u
    cv2.imwrite = _cv2_imwrite_u