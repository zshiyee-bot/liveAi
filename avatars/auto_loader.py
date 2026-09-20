###############################################################################
#  模型自动识别与懒加载
#
#  设计目标（依用户要求）：
#    启动时**不加载任何模型权重**（省显存、启动快），
#    用户连接时按「素材目录的特征」自动判定该用哪个模型，
#    自动加载对应权重，用户不需要选模型、也不需要选素材类型。
#
#  判定依据 —— 素材目录里的文件天然自证模型类型：
#    musetalk : full_imgs/ + mask/ + coords.pkl + latents.pt + mask_coords.pkl
#    wav2lip  : full_imgs/ + face_imgs/ + coords.pkl
#
#  约束：
#    - 只支持 wav2lip256 与 musetalk（用户已决定去掉 384）。
#      若检测到 384 素材（face_imgs 为 384x384），明确报错要求改用 256 素材，
#      因为 384 需要另一个权重文件 models/wav2lip384.pth 且上游 load_model
#      不返回分辨率信息（该补丁在老项目，故意未并入本项目）。
#    - 任意时刻只驻留**一个**模型：切换模型时先卸载旧的、清显存，再加载新的。
#    - 有活跃会话（正在推流）时拒绝切换，避免卸载正在使用的权重。
###############################################################################

import gc
import os
import threading

from utils.logger import logger

# 素材目录下的特征文件/目录 → 模型名
_MUSE_SIG = ('full_imgs', 'mask', 'coords.pkl', 'latents.pt', 'mask_coords.pkl')
_WAV_SIG = ('full_imgs', 'face_imgs', 'coords.pkl')

# 模型名 → (avatar 模块路径, 权重文件)
MODULE_OF = {
    'musetalk': 'avatars.musetalk_avatar',
    'wav2lip': 'avatars.wav2lip_avatar',
}
WEIGHT_OF = {
    'wav2lip': './models/wav2lip.pth',
}

AVATARS_ROOT = './data/avatars'

# 显存护栏：wav2lip384 + musetalk 会超 16GB，故明确不支持 384
_WAV2LIP_MAX_FACE_RES = 256


class ModelLoadError(Exception):
    """模型判定或加载失败，消息会原样返回给前端，故用中文且可操作。"""


def _has(base, name):
    return os.path.exists(os.path.join(base, name))


def detect_model(avatar_id):
    """按素材目录内容判定模型类型。

    返回 'musetalk' 或 'wav2lip'；无法判定时抛 ModelLoadError（消息面向用户）。
    """
    base = os.path.join(AVATARS_ROOT, avatar_id)
    if not os.path.isdir(base):
        raise ModelLoadError(
            f"素材「{avatar_id}」不存在。请检查角色 ID 是否写错，"
            f"或先在素材管理页面上传并训练素材。")

    missing_m = [n for n in _MUSE_SIG if not _has(base, n)]
    missing_w = [n for n in _WAV_SIG if not _has(base, n)]

    if not missing_m:
        kind = 'musetalk'
    elif not missing_w:
        kind = 'wav2lip'
    else:
        # 两个签名都不完整 —— 给出最具体的缺失清单，便于用户判断
        raise ModelLoadError(
            f"素材「{avatar_id}」不完整，无法判定模型类型。\n"
            f"  · 作为 musetalk 使用缺少：{', '.join(missing_m)}\n"
            f"  · 作为 wav2lip 使用缺少：{', '.join(missing_w)}\n"
            f"请先在素材管理页面完成训练。")

    if kind == 'wav2lip':
        _check_wav_res(base, avatar_id)
    return kind


def _check_wav_res(base, avatar_id):
    """wav2lip 素材的 face_imgs 分辨率必须是 256（384 需另一权重，本项目不支持）。"""
    face_dir = os.path.join(base, 'face_imgs')
    try:
        names = sorted(os.listdir(face_dir))
    except OSError:
        return
    png = [n for n in names if os.path.splitext(n)[1].lower() in ('.png', '.jpg', '.jpeg')]
    if not png:
        return
    first = os.path.join(face_dir, png[0])
    try:
        from PIL import Image
        with Image.open(first) as im:
            w, h = im.size
    except Exception:
        return
    if max(w, h) > _WAV2LIP_MAX_FACE_RES:
        raise ModelLoadError(
            f"素材「{avatar_id}」的 face_imgs 是 {w}x{h}，属于 wav2lip384 素材，"
            f"本项目只支持 256。请用 --img_size 256 重新训练该素材，"
            f"或改用 256 的素材。")


def scan_available():
    """扫描 data/avatars，返回 {模型名: [可用素材 id, ...]}。

    用于启动日志与前端提示，让用户知道「连哪些 ID 会加载哪个模型」。
    """
    out = {'musetalk': [], 'wav2lip': [], 'broken': {}}
    root = AVATARS_ROOT
    if not os.path.isdir(root):
        return out
    for name in sorted(os.listdir(root)):
        base = os.path.join(root, name)
        if not os.path.isdir(base) or name.startswith('.'):
            continue
        # 跳过"库"目录（库里面装的是片段，自身不是素材）
        if _has(base, 'uploads') and not _has(base, 'coords.pkl'):
            continue
        try:
            kind = detect_model(name)
            out[kind].append(name)
        except ModelLoadError as e:
            out['broken'][name] = str(e).split('\n')[0]
        except Exception as e:              # 扫描不应该因单个坏目录整体失败
            out['broken'][name] = f'{type(e).__name__}: {e}'
    return out


def _free_cached_vram(tag: str = ''):
    """把「模型加载 / warm_up 期间被 PyTorch 缓存分配器扣住、但实际不再使用」
    的显存交还给驱动。

    为什么要做（本机 RTX 4060 Ti 16GB 实测，musetalk + batch 16）：
      · 加载 + warm_up 之后：allocated ≈ 1.9GB，而 reserved ≈ 10.5GB，
        nvidia-smi 看到进程占用 11.5GB —— 其中 ~8.6GB 只是缓存里的空闲块。
      · 这台机器上浏览器（同机看画面会软解 688x1312@25fps）、DSH Desktop、
        GameViewerServer、dwm 也都要显存；总需求一旦超过物理显存，WDDM 会把
        显存换到内存，CUDA 核掉到 PCIe 速度 —— 实测表现就是
        inference_batch 从 43.6ms/帧 涨到 205ms/帧、推流 4~6fps、GPU 只有约 30W。
      · empty_cache() 之后：reserved 10.5GB → 2.5GB，进程占用 11.5GB → 3.4GB，
        等于凭空多出 8GB 安全余量。这一步只回吐空闲缓存，不动任何在用张量。

    逃生开关：LT_FREE_VRAM=0 可关闭。
    """
    if os.getenv('LT_FREE_VRAM', '1') == '0':
        return
    try:
        import torch
        if not torch.cuda.is_available():
            return
        torch.cuda.synchronize()
        before = torch.cuda.memory_reserved() / (1 << 20)
        torch.cuda.empty_cache()
        after = torch.cuda.memory_reserved() / (1 << 20)
        logger.info("[gpu] 显存缓存已交还驱动（%s）：reserved %.0fMB -> %.0fMB", tag, before, after)
    except Exception as e:
        logger.warning("[gpu] empty_cache 失败（忽略）：%s", e)


class AutoModelManager:
    """按需加载 / 卸载模型。任意时刻至多驻留一个模型。

    - get(kind)        取当前模型；若已是目标模型直接返回，否则尝试切换。
    - unload()         卸载并清显存。
    - busy_check       由外部注入的回调，返回当前活跃会话数（>0 时拒绝切换）。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._kind = None          # 当前驻留的模型名
        self._model = None         # 权重对象
        self._mod = None           # 对应 avatar 模块（提供 load_model/load_avatar/warm_up）
        self._busy_check = None
        self._sweep = None

    def set_busy_check(self, fn):
        self._busy_check = fn

    def set_sweeper(self, fn):
        """注入僵尸会话回收函数（session_manager.drop_stale_connecting）。"""
        self._sweep = fn

    @property
    def current(self):
        return self._kind

    def module_of(self, kind):
        return MODULE_OF[kind]

    def _import_mod(self, kind):
        import importlib
        return importlib.import_module(MODULE_OF[kind])

    def get(self, kind, batch_size=16):
        """确保 kind 模型已就绪并返回 (model, module)。必要时切换。"""
        if kind not in MODULE_OF:
            raise ModelLoadError(f"不支持的模型类型：{kind}")

        with self._lock:
            if self._kind == kind and self._model is not None:
                # 复用已驻留模型：也要回吐「上一次会话运行期」留下的空闲缓存，
                # 否则第二次连接是带着 4~5GB 缓存开始的（实测会话停掉后仍占 6.6GB）。
                _free_cached_vram(f'{kind}:cached')
                return self._model, self._mod

            # 需要切换 —— 先确认没有别的会话正在用旧模型
            # 先回收「卡在 connecting 的僵尸会话」，否则客户端异常退出后
            # 会一直挡住模型切换，用户只能重启服务。
            if self._sweep is not None:
                try:
                    gone = self._sweep()
                    if gone:
                        logger.info("[auto] 已回收 %d 个僵尸会话（连接建立失败/客户端消失）",
                                    len(gone))
                except Exception:
                    logger.exception("[auto] 回收僵尸会话时出错（忽略，继续）")

            blockers = []
            if self._busy_check is not None:
                try:
                    blockers = list(self._busy_check() or [])
                except Exception:
                    blockers = []
            if blockers and self._kind is not None:
                # 把会话描述成用户能看懂的形式（素材名比 sessionid 有用）
                names = []
                for b in blockers[:3]:
                    try:
                        names.append(getattr(b.opt, 'avatar_id', None) or str(b)[:8])
                    except Exception:
                        names.append(str(b)[:8])
                who = '、'.join(names)
                more = f" 等共 {len(blockers)} 个会话" if len(blockers) > 3 else ""
                raise ModelLoadError(
                    f"当前有连接正在使用「{self._kind}」模型（{who}{more}），"
                    f"无法切换到「{kind}」。请先断开这些连接再重连。")

            if self._kind is not None:
                logger.info(f"[auto] 切换模型 {self._kind} -> {kind}，先卸载旧模型")
                self._unload_locked()

            logger.info(f"[auto] 按需加载模型：{kind}")
            mod = self._import_mod(kind)
            model = self._load_locked(kind, mod, batch_size)
            self._kind, self._model, self._mod = kind, model, mod
            logger.info(f"[auto] 模型 {kind} 就绪")
            return model, mod

    def _load_locked(self, kind, mod, batch_size):
        try:
            if kind == 'musetalk':
                model = mod.load_model()
                mod.warm_up(batch_size, model)
            else:  # wav2lip
                model = mod.load_model(WEIGHT_OF['wav2lip'])
                mod.warm_up(batch_size, model, 256)
            # warm_up 会把大块 activation 缓存留在显存里（实测 ~8.6GB 空闲缓存），
            # 交还驱动，避免与同机其它程序一起挤爆 16GB 触发 WDDM 换页。
            _free_cached_vram(kind)
            return model
        except ModelLoadError:
            raise
        except Exception as e:
            raise ModelLoadError(f"加载「{kind}」模型失败：{type(e).__name__}: {e}")

    def _unload_locked(self):
        self._model = None
        self._mod = None
        self._kind = None
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                torch.cuda.ipc_collect()
        except Exception:
            pass
        gc.collect()
        logger.info("[auto] 旧模型已卸载，显存已清理")

    def unload(self):
        with self._lock:
            self._unload_locked()


auto_models = AutoModelManager()
