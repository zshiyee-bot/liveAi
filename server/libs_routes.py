###############################################################################
#  库（Library）管理 API — 「库 → 片段」两级素材组织
#
#  设计（按用户要求）
#    一个「库」= 一个数字人的全部素材，对应 data/avatars/<库名>/
#    库内的「片段」= 一个子文件夹，里面放一段训练好的素材
#
#    data/avatars/
#      digitalhuman1/            <- 库（库名 = 直播页要填的角色 ID）
#        playlist.json           <- 素材链配置（决定哪些片段参与循环）
#        coords.pkl / full_imgs/ <- 兼容文件，指向 playlist 第一段（load_avatar 先读它们）
#        clip1/                  <- 片段（训练产物：coords.pkl + full_imgs/ + face_imgs/）
#        clip2/
#        uploads/                <- 上传的原始视频（未训练）
#          9月15日(1).mp4
#
#  上传 vs 训练
#    上传的视频只是原始 mp4，**必须训练**才能用（生成 coords.pkl / full_imgs / face_imgs）。
#    训练 = 复用 server/task_manager.py 的既有流程（TaskManager 已是 max_workers=1
#    串行队列，天然满足「一个完成再进行下一个」）。
###############################################################################

import os
import re
import json
import stat
import shutil
import glob

from aiohttp import web

from utils.logger import logger

from server.materials_routes import (
    AVATARS_ROOT, PLAYLIST_NAME, KIND_SIGNATURES,
    probe_avatar, json_ok, json_error, _chain_segments_detail,
    _make_junction, _count_png, _frame_mae, _first_last_frames,
)

UPLOAD_DIRNAME = 'uploads'

# 库名/片段名允许的字符：中英文、数字、下划线、连字符（禁止路径分隔符与 ..）
_NAME_OK = re.compile(r'^[\w\u4e00-\u9fff\-\. ]{1,64}$')


def _safe(name):
    """校验名字是否安全（防路径穿越）。返回 (ok, err)"""
    if not name:
        return False, "名字不能为空"
    if not _NAME_OK.match(name):
        return False, "名字只能包含中英文、数字、下划线、连字符、点、空格（1-64 字）"
    if name.startswith('.') or '..' in name:
        return False, "名字不能以点开头或包含 .."
    return True, None


def lib_path(name):
    return os.path.join(AVATARS_ROOT, name)


# ─── 库元信息（可选）─────────────────────────────────────────────
# 只存"默认训练类型"这一个偏好项，用于省掉每次训练时的一次点击。
# 它是**默认值**而非硬属性：库内仍可混装两种模型的片段。
LIB_META_NAME = 'lib.json'


def read_lib_meta(lib):
    """读库元信息，不存在或损坏时返回 {}。"""
    p = os.path.join(lib_path(lib), LIB_META_NAME)
    if not os.path.isfile(p):
        return {}
    try:
        with open(p, 'r', encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        logger.warning(f"库元信息损坏，忽略：{p}")
        return {}


def _write_lib_meta(lib, meta):
    """写库元信息（合并式写入）。"""
    p = os.path.join(lib_path(lib), LIB_META_NAME)
    cur = read_lib_meta(lib)
    cur.update(meta or {})
    tmp = p + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(cur, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    return cur



def list_clips(lib):
    """列出库内所有片段（子目录）及其训练状态。
    片段 = 有 coords.pkl + full_imgs/ 且不是 uploads/。
    """
    base = lib_path(lib)
    out = []
    if not os.path.isdir(base):
        return out
    for nm in sorted(os.listdir(base)):
        p = os.path.join(base, nm)
        if not os.path.isdir(p) or nm == UPLOAD_DIRNAME or nm.startswith('.'):
            continue
        # 判断是片段（有素材）还是只是空文件夹
        has_coords = os.path.isfile(os.path.join(p, 'coords.pkl'))
        has_full = os.path.isdir(os.path.join(p, 'full_imgs'))
        has_face = os.path.isdir(os.path.join(p, 'face_imgs'))
        has_mask = os.path.isdir(os.path.join(p, 'mask'))
        has_latents = os.path.isfile(os.path.join(p, 'latents.pt'))
        frames = _count_png(os.path.join(p, 'full_imgs'))
        if not has_full:
            continue
        if has_face and has_coords:
            kind = 'wav2lip'
        elif has_mask and has_coords and has_latents:
            kind = 'musetalk'
        else:
            kind = 'unknown'
        first, last = _first_last_frames(None, p)
        size = None
        if first:
            try:
                from PIL import Image
                with Image.open(first) as im:
                    size = im.size
            except Exception:
                size = None
        out.append({
            'name': nm,
            'kind': kind,
            'frames': frames,
            'duration_sec': round(frames / 25.0, 2),
            'width': size[0] if size else None,
            'height': size[1] if size else None,
            'trained': bool(has_coords and frames > 0 and kind != 'unknown'),
            'tail_head_mae': _frame_mae(first, last) if (first and last) else None,
        })
    return out


def list_uploads(lib):
    """列出库内待训练/已上传的原始视频。"""
    base = os.path.join(lib_path(lib), UPLOAD_DIRNAME)
    out = []
    if not os.path.isdir(base):
        return out
    for nm in sorted(os.listdir(base)):
        p = os.path.join(base, nm)
        if not os.path.isfile(p):
            continue
        ext = os.path.splitext(nm)[1].lower()
        if ext not in ('.mp4', '.avi', '.mov', '.mkv', '.webm', '.flv'):
            continue
        try:
            sz = os.path.getsize(p)
        except OSError:
            sz = 0
        # 已训练过？约定：同名片段目录存在即视为已训练
        stem = os.path.splitext(nm)[0]
        trained_as = None
        for c in list_clips(lib):
            if c['name'] == stem and c['trained']:
                trained_as = stem
                break
        out.append({
            'file': nm,
            'stem': stem,
            'size_mb': round(sz / 1048576, 1),
            'trained_as': trained_as,
        })
    return out


def list_libs():
    """列出所有库。库有以下几种形态（任意一种即算库）：

    1. 含片段的目录（用本界面训练过的库）
    2. 自身就是一段素材的目录（老式单目录素材：有 coords.pkl + full_imgs/，
       官方 avatar 与 start.bat 用的那种）
    3. **新建但还没上传的空库**（有 uploads/ 子目录 —— api_lib_create 一定会建它）
    4. 有待训练上传视频的库（有自己的 uploads/ 且里面有视频）

    不列入的是「历史遗留的纯素材/垃圾目录」：既没有片段、也不是自素材、
    也没有 uploads/ 子目录（例如早期实验留下的空目录 test）。

    ⚠️ 这里曾有一个严重 bug：判空条件只写了 `not clips and not self_ready`，
    导致「刚建好还没上传视频的新库」被静默跳过，用户在侧栏看不到自己的库，
    以为新建失败。修法：只要目录里有 uploads/ 子目录（= 是本界面建的库）就列出。
    """
    out = []
    if not os.path.isdir(AVATARS_ROOT):
        return out
    for nm in sorted(os.listdir(AVATARS_ROOT)):
        p = os.path.join(AVATARS_ROOT, nm)
        if not os.path.isdir(p) or nm.startswith('.'):
            continue
        clips = list_clips(nm)
        pl_path = os.path.join(p, PLAYLIST_NAME)
        playlist = None
        if os.path.isfile(pl_path):
            try:
                with open(pl_path, 'r', encoding='utf-8-sig') as f:
                    playlist = json.load(f)
            except Exception as e:
                logger.warning(f"读取 {pl_path} 失败: {e}")
        # 判断库自身是否也是一段素材（load_avatar 先读它）
        self_ready = (os.path.isfile(os.path.join(p, 'coords.pkl'))
                      and os.path.isdir(os.path.join(p, 'full_imgs')))
        uploads = list_uploads(nm)
        # 「是本界面建的库」的标志：存在 uploads/ 子目录（api_lib_create 必建）
        is_ui_lib = os.path.isdir(os.path.join(p, UPLOAD_DIRNAME))
        if not clips and not self_ready and not uploads and not is_ui_lib:
            continue
        # 按模型种类汇总片段数：库内可能同时有 wav2lip 与 musetalk 片段
        # （都是历史遗留或从别处移进来的），前端据此做筛选与「混编」告警。
        kinds = {}
        for c in clips:
            kinds[c['kind']] = kinds.get(c['kind'], 0) + 1
        # 「单目录素材」（data/avatars/<id>/ 自身就是一段素材，如官方 avatar 与
        # start.bat 用的那些）没有 clips，必须单独探测它自己的类型，
        # 否则界面上的"按模型筛选"对它们完全失效。
        self_kind = ''
        if self_ready and not clips:
            try:
                if (os.path.isfile(os.path.join(p, 'latents.pt'))
                        and os.path.isdir(os.path.join(p, 'mask'))):
                    self_kind = 'musetalk'
                elif (os.path.isdir(os.path.join(p, 'face_imgs'))
                        and os.path.isfile(os.path.join(p, 'coords.pkl'))):
                    self_kind = 'wav2lip'
            except Exception:
                self_kind = ''
        out.append({
            'name': nm,
            'clips': clips,
            'clip_count': len(clips),
            'kinds': kinds,
            # mixed=True 表示库内同时存在多种模型的片段 —— 编排时必须按类型分开
            'mixed': len([k for k in kinds if k != 'unknown']) > 1,
            'uploads': uploads,
            'is_ui_lib': is_ui_lib,
            # 建库时选定的"默认训练类型"，仅用于给训练下拉框预选值
            # （'' 表示没预设，训练时必选一次）。不是库的硬属性。
            'default_model': read_lib_meta(nm).get('model', ''),
            # 该库实际已训练出哪些类型（用于界面提示"这个库目前是 X 类型"）
            # 注意把"单目录素材"自身的类型也算进去，否则根视图筛选会失效。
            'trained_kinds': sorted(set(
                [k for k in kinds if k != 'unknown'] + ([self_kind] if self_kind else [])
            )),
            'self_kind': self_kind,
            'playlist': playlist,
            'has_playlist': playlist is not None,
            'self_ready': self_ready,
            'self_frames': _count_png(os.path.join(p, 'full_imgs')),
        })
    return out


# ─── HTTP 处理 ────────────────────────────────────────────────────────────

async def api_libs_list(request):
    """GET /api/libs — 列出所有库（含片段、上传、训练状态）"""
    try:
        libs = list_libs()
        return json_ok({"libs": libs, "root": os.path.abspath(AVATARS_ROOT),
                        "count": len(libs)})
    except Exception as e:
        logger.exception('api_libs_list exception:')
        return json_error(str(e))


async def api_lib_create(request):
    """POST /api/libs {name, model?} — 新建库（= 新建数字人）

    model 是**可选**的默认训练类型（'wav2lip' | 'musetalk' | ''=每次训练时再选）。
    它只是"省一次点击"的默认值，**不是库的硬属性** ——
    一个库里仍然可以混装两种模型的片段（用户的素材/模型组合自由度优先）。
    真正的类型永远由片段产物的内容推断，见 list_clips()。
    """
    try:
        p = await request.json()
        name = (p.get('name') or '').strip()
        ok, err = _safe(name)
        if not ok:
            return json_error(err)
        model = (p.get('model') or '').strip()
        if model not in ('', 'wav2lip', 'musetalk'):
            return json_error("model 只能是 wav2lip、musetalk 或留空（每次训练时再选）")
        d = lib_path(name)
        if os.path.isdir(d):
            return json_error(f"库「{name}」已存在")
        os.makedirs(d, exist_ok=True)
        os.makedirs(os.path.join(d, UPLOAD_DIRNAME), exist_ok=True)
        if model:
            _write_lib_meta(name, {'model': model})
        logger.info(f"已新建库: {d}（默认训练类型={model or '未指定'}）")
        return json_ok({"name": name, "path": os.path.abspath(d), "model": model})
    except Exception as e:
        logger.exception('api_lib_create exception:')
        return json_error(str(e))


async def api_lib_set_model(request):
    """POST /api/libs/{lib}/model {model} — 设置该库的默认训练类型。

    model 可为 'wav2lip' / 'musetalk' / ''（留空=每次训练时都要选）。
    只是默认值，不影响已训练好的片段类型。
    """
    try:
        lib = request.match_info['lib']
        ok, err = _safe(lib)
        if not ok:
            return json_error(err)
        if not os.path.isdir(lib_path(lib)):
            return json_error(f"库「{lib}」不存在", code=404)
        p = await request.json() if request.can_read_body else {}
        model = (p.get('model') or '').strip()
        if model not in ('', 'wav2lip', 'musetalk'):
            return json_error("model 只能是 wav2lip、musetalk 或留空")
        _write_lib_meta(lib, {'model': model})
        logger.info(f"库「{lib}」默认训练类型设为：{model or '未指定'}")
        return json_ok({"name": lib, "model": model})
    except Exception as e:
        logger.exception('api_lib_set_model exception:')
        return json_error(str(e))


async def api_lib_delete(request):
    """DELETE /api/libs/{lib} — 删除整个库（含所有片段与上传）"""
    try:
        # 路由注册为 {lib}，此处必须用 'lib' 取值
        name = request.match_info['lib']
        ok, err = _safe(name)
        if not ok:
            return json_error(err)
        d = lib_path(name)
        if not os.path.isdir(d):
            return json_error(f"库「{name}」不存在", code=404)
        # junction/符号链接要用 rmdir 删，避免删到源
        _rmtree_safe(d)
        logger.info(f"已删除库: {d}")
        return json_ok({"name": name})
    except Exception as e:
        logger.exception('api_lib_delete exception:')
        return json_error(str(e))


def _rmtree_safe(d):
    """删除目录树，**绝不跟随 junction/符号链接**。

    ⚠️ 血的教训：原先用 `os.walk(d, topdown=False)` + 判断 islink 的写法是**错的** ——
    `os.walk` 默认 `followlinks=False` 只是不递归**子目录符号链接**，但在
    Windows 上对 **junction（目录 reparse point）它会照常进入**，于是：
      库/片段junction -> 真实素材目录
    walk 进到真实目录里，把 coords.pkl / full_imgs 全部真删了。
    实测损失：new111_wav / new444_wav / new555_wav 三个素材被清空（已重新生成）。

    正确做法：**自底向上手动递归，遇到 reparse point 只 rmdir 那个链接本身**，
    绝不 os.walk 进去。
    """
    def _rm(p):
        # 1) 先判断是不是链接/重解析点：是则只删链接本身（rmdir 对目录链接有效）
        if _is_link_like(p):
            try:
                if os.path.isdir(p):
                    os.rmdir(p)      # 删 junction 本身，**不会**删到源目录内容
                else:
                    os.remove(p)
            except OSError as e:
                logger.warning(f"删除链接失败 {p}: {e}")
            return
        # 2) 普通目录：手动列出成员（不递归进链接）
        if os.path.isdir(p):
            try:
                entries = list(os.scandir(p))
            except OSError as e:
                logger.warning(f"读取目录失败 {p}: {e}")
                return
            for ent in entries:
                _rm(ent.path)            # 递归（每个成员自己再判是否链接）
            try:
                os.rmdir(p)
            except OSError as e:
                logger.warning(f"删除目录失败 {p}: {e}")
            return
        # 3) 普通文件
        try:
            os.remove(p)
        except OSError as e:
            logger.warning(f"删除文件失败 {p}: {e}")

    if not os.path.exists(d):
        return
    # 关键：如果传入的 d 本身就是 junction，只删链接，不碰目标
    if _is_link_like(d):
        try:
            os.rmdir(d)
        except OSError as e:
            logger.warning(f"删除库链接失败 {d}: {e}")
        return
    _rm(d)


def _is_reparse(p):
    """判断是否为 junction / 符号链接 / 其他重解析点。

    ⚠️ 必须用 **os.lstat**（不跟随）取 st_file_attributes！
    实测（Windows 11 + Python 3.12）对 mklink /J 建的 junction：
        os.path.islink(p)                     -> False   ← 靠这个判不出来
        os.path.isdir(p)                      -> True    ← 会被当成普通目录
        os.lstat(p).st_file_attributes        -> 0x2410  ← 含 0x400 重解析位 ✅
        os.stat(p).st_file_attributes         -> 0x2010  ← 跟随目标后，重解析位没了 ❌
    用 os.stat 会漏判，从而走进 junction 删掉真实素材（已发生过一次数据损失）。
    """
    try:
        st = os.lstat(p)
    except Exception:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    return bool(getattr(st, 'st_file_attributes', 0) & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT

def _is_link_like(p):
    """链接/重解析点统一判定（junction 不是 os.path.islink，必须走 _is_reparse）。"""
    return os.path.islink(p) or _is_reparse(p)


async def api_clip_create(request):
    """POST /api/libs/{lib}/clips {name} — 在库内新建空片段文件夹"""
    try:
        lib = request.match_info['lib']
        ok, err = _safe(lib)
        if not ok:
            return json_error(err)
        d = lib_path(lib)
        if not os.path.isdir(d):
            return json_error(f"库「{lib}」不存在", code=404)
        p = await request.json()
        name = (p.get('name') or '').strip()
        ok, err = _safe(name)
        if not ok:
            return json_error(err)
        c = os.path.join(d, name)
        if os.path.isdir(c):
            return json_error(f"片段「{name}」已存在")
        os.makedirs(c, exist_ok=True)
        logger.info(f"已新建片段文件夹: {c}")
        return json_ok({"lib": lib, "name": name})
    except Exception as e:
        logger.exception('api_clip_create exception:')
        return json_error(str(e))


async def api_clip_delete(request):
    """DELETE /api/libs/{lib}/clips/{name} — 删除片段（含素材）"""
    try:
        lib = request.match_info['lib']
        name = request.match_info['name']
        for n in (lib, name):
            ok, err = _safe(n)
            if not ok:
                return json_error(err)
        c = os.path.join(lib_path(lib), name)
        if not os.path.isdir(c):
            return json_error("片段不存在", code=404)
        _rmtree_safe(c)
        logger.info(f"已删除片段: {c}")
        return json_ok({"lib": lib, "name": name})
    except Exception as e:
        logger.exception('api_clip_delete exception:')
        return json_error(str(e))


# ─── 整理：重命名 / 移动 ───────────────────────────────────────────────────

def _sync_playlist_after_change(lib, rename_map=None, drop_names=None, add_names=None):
    """片段改名/移动后同步库的 playlist.json，否则编排里会残留旧名导致加载失败。

    rename_map: {old: new}；drop_names: 要移除的；add_names: 追加的。
    目标库原本没有 playlist.json 时，若本次是「追加」（移动过来的片段），
    则新建一个只含该片段的 playlist —— 否则移到新库的片段不会被任何素材链引用，
    用户会以为「移动后不见了」。
    """
    p = os.path.join(lib_path(lib), PLAYLIST_NAME)
    if not os.path.isfile(p):
        # 只有「追加」才有必要凭空建档；纯重命名/移除时库还没编排过，不用建
        if not add_names:
            return False
        cfg = {'segments': [], 'entry': 0, 'mode': 'sequence', '_lib': lib}
    else:
        try:
            with open(p, 'r', encoding='utf-8-sig') as f:
                cfg = json.load(f)
        except Exception as e:
            logger.warning(f"同步 playlist 时读取失败 {p}: {e}")
            return False
    segs = list(cfg.get('segments') or [])
    drop = set(drop_names or [])
    segs = [s for s in segs if s not in drop]
    if rename_map:
        segs = [rename_map.get(s, s) for s in segs]
    for a in (add_names or []):
        if a not in segs:
            segs.append(a)
    cfg['segments'] = segs
    # entry 越界保护
    try:
        e = int(cfg.get('entry', 0))
    except Exception:
        e = 0
    if segs:
        cfg['entry'] = max(0, min(e, len(segs) - 1))
    else:
        cfg['entry'] = 0
    try:
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        logger.info(f"已同步 playlist: {p} segments={segs}")
        return True
    except Exception as e:
        logger.warning(f"同步 playlist 写入失败 {p}: {e}")
        return False


async def api_clip_rename(request):
    """POST /api/libs/{lib}/clips/{name}/rename {name: 新名} — 片段重命名"""
    try:
        lib = request.match_info['lib']
        old = request.match_info['name']
        for n in (lib, old):
            ok, err = _safe(n)
            if not ok:
                return json_error(err)
        p = await request.json()
        new = (p.get('name') or p.get('new') or '').strip()
        ok, err = _safe(new)
        if not ok:
            return json_error(err)
        if new == old:
            return json_ok({"lib": lib, "old": old, "name": new})
        base = lib_path(lib)
        src = os.path.join(base, old)
        dst = os.path.join(base, new)
        if not os.path.isdir(src):
            return json_error("片段不存在", code=404)
        if os.path.exists(dst):
            return json_error(f"目标名「{new}」已存在")
        try:
            os.rename(src, dst)
        except OSError as e:
            return json_error(f"重命名失败（文件夹可能被占用）: {e}")
        _sync_playlist_after_change(lib, rename_map={old: new})
        logger.info(f"片段重命名: {src} -> {dst}")
        return json_ok({"lib": lib, "old": old, "name": new})
    except Exception as e:
        logger.exception('api_clip_rename exception:')
        return json_error(str(e))


async def api_clip_move(request):
    """POST /api/libs/{lib}/clips/{name}/move {to: 目标库} — 把片段移到另一个库

    用途：把训练好的片段在「数字人」之间挪动，不用重新训练。
    同盘用 os.rename（瞬间完成）；跨盘自动回退到 shutil.move。
    """
    try:
        lib = request.match_info['lib']
        name = request.match_info['name']
        for n in (lib, name):
            ok, err = _safe(n)
            if not ok:
                return json_error(err)
        p = await request.json()
        to = (p.get('to') or '').strip()
        ok, err = _safe(to)
        if not ok:
            return json_error(err)
        if to == lib:
            return json_error("目标库与当前库相同")
        src_base, dst_base = lib_path(lib), lib_path(to)
        if not os.path.isdir(src_base):
            return json_error(f"源库「{lib}」不存在", code=404)
        if not os.path.isdir(dst_base):
            return json_error(f"目标库「{to}」不存在", code=404)
        src = os.path.join(src_base, name)
        dst = os.path.join(dst_base, name)
        if not os.path.isdir(src):
            return json_error("片段不存在", code=404)
        if os.path.exists(dst):
            return json_error(f"目标库已有同名片段「{name}」")
        # 目标库必须存在 uploads/ 才算「本界面建的库」；顺手补齐
        os.makedirs(os.path.join(dst_base, UPLOAD_DIRNAME), exist_ok=True)
        try:
            os.rename(src, dst)
            how = 'rename'
        except OSError:
            import shutil
            try:
                shutil.move(src, dst)
                how = 'move'
            except Exception as e:
                return json_error(f"移动失败: {e}")
        # 两个库的 playlist 都要同步
        _sync_playlist_after_change(lib, drop_names=[name])
        _sync_playlist_after_change(to, add_names=[name])
        logger.info(f"片段移动: {src} -> {dst} (via {how})")
        return json_ok({"from": lib, "to": to, "name": name, "how": how})
    except Exception as e:
        logger.exception('api_clip_move exception:')
        return json_error(str(e))


async def api_upload_rename(request):
    """POST /api/libs/{lib}/upload/{name}/rename {name: 新文件名} — 上传视频重命名

    只改 uploads/ 里的原始视频文件名；不影响已训练出的片段目录。
    """
    try:
        lib = request.match_info['lib']
        old = request.match_info['name']
        for n in (lib, old):
            ok, err = _safe(n)
            if not ok:
                return json_error(err)
        if os.path.basename(old) != old:
            return json_error("非法文件名")
        p = await request.json()
        new = (p.get('name') or p.get('new') or '').strip()
        if os.path.basename(new) != new or not new:
            return json_error("非法文件名")
        ext = os.path.splitext(new)[1].lower()
        if ext not in ('.mp4', '.avi', '.mov', '.mkv', '.webm', '.flv'):
            return json_error(f"不支持的视频格式: {ext}")
        updir = os.path.join(lib_path(lib), UPLOAD_DIRNAME)
        src, dst = os.path.join(updir, old), os.path.join(updir, new)
        if not os.path.isfile(src):
            return json_error("上传文件不存在", code=404)
        if os.path.exists(dst):
            return json_error(f"目标文件名「{new}」已存在")
        try:
            os.rename(src, dst)
        except OSError as e:
            return json_error(f"重命名失败（文件可能被占用）: {e}")
        logger.info(f"上传文件重命名: {src} -> {dst}")
        return json_ok({"lib": lib, "old": old, "name": new})
    except Exception as e:
        logger.exception('api_upload_rename exception:')
        return json_error(str(e))


async def api_lib_rename(request):
    """POST /api/libs/{lib}/rename {name: 新库名} — 库（数字人）重命名

    注意：库名就是启动时的 --avatar_id / 网页里填的角色 ID，
    改名后要同步改 playlist.json 的引用，并在界面上提示用户更新角色 ID。
    """
    try:
        old = request.match_info['lib']
        ok, err = _safe(old)
        if not ok:
            return json_error(err)
        p = await request.json()
        new = (p.get('name') or p.get('new') or '').strip()
        ok, err = _safe(new)
        if not ok:
            return json_error(err)
        if new == old:
            return json_ok({"old": old, "name": new})
        src, dst = lib_path(old), lib_path(new)
        if not os.path.isdir(src):
            return json_error(f"库「{old}」不存在", code=404)
        if os.path.exists(dst):
            return json_error(f"库「{new}」已存在")
        try:
            os.rename(src, dst)
        except OSError as e:
            return json_error(f"重命名失败（目录可能被占用，先断开该会话）: {e}")
        logger.info(f"库重命名: {src} -> {dst}")
        return json_ok({"old": old, "name": new,
                        "hint": "库名=角色 ID，网页里连接时要填新名字"})
    except Exception as e:
        logger.exception('api_lib_rename exception:')
        return json_error(str(e))


async def api_upload(request):
    """POST /api/libs/{lib}/upload — 上传原始视频到库的 uploads/ 目录（multipart）
    注意：只存原始文件，**不会自动训练**。训练请再调 /train。
    """
    try:
        lib = request.match_info['lib']
        ok, err = _safe(lib)
        if not ok:
            return json_error(err)
        base = lib_path(lib)
        if not os.path.isdir(base):
            return json_error(f"库「{lib}」不存在", code=404)
        updir = os.path.join(base, UPLOAD_DIRNAME)
        # ⚠️ 必须显式判定库目录存在（上面那行）之后才建 uploads/。
        #    早期 makedirs(exist_ok=True) 会「顺手」把库目录也建出来，
        #    于是往一个拼错的库名上传时会静默创建出一个新库（实测 bug）。
        os.makedirs(updir, exist_ok=True)

        saved = []
        reader = await request.multipart()
        while True:
            part = await reader.next()
            if part is None:
                break
            if part.name != 'video_file':
                continue
            fn = part.filename or 'upload.mp4'
            # 只取文件名，防路径穿越
            fn = os.path.basename(fn)
            ext = os.path.splitext(fn)[1].lower()
            if ext not in ('.mp4', '.avi', '.mov', '.mkv', '.webm', '.flv'):
                return json_error(f"不支持的视频格式: {ext}")
            dst = os.path.join(updir, fn)
            # 重名加序号
            i = 1
            while os.path.exists(dst):
                stem, e = os.path.splitext(fn)
                dst = os.path.join(updir, f"{stem}_{i}{e}")
                i += 1
            with open(dst, 'wb') as f:
                while True:
                    chunk = await part.read_chunk()
                    if not chunk:
                        break
                    f.write(chunk)
            saved.append(os.path.basename(dst))
            logger.info(f"已上传: {dst} ({os.path.getsize(dst)} B)")
        if not saved:
            return json_error("没有收到视频文件（字段名应为 video_file）")
        return json_ok({"lib": lib, "files": saved, "saved": saved})
    except Exception as e:
        logger.exception('api_upload exception:')
        return json_error(str(e))


async def api_upload_delete(request):
    """DELETE /api/libs/{lib}/upload/{name} — 删除库内一个已上传的原始视频。

    只删 <lib>/uploads/<name> 这个文件，**不动**同名已训练出的片段目录。
    """
    try:
        lib = request.match_info['lib']
        name = request.match_info['name']
        for n in (lib, name):
            ok, err = _safe(n)
            if not ok:
                return json_error(err)
        # 只允许删文件名本身，堵死路径穿越
        if os.path.basename(name) != name:
            return json_error("非法文件名")
        p = os.path.join(lib_path(lib), UPLOAD_DIRNAME, name)
        if not os.path.isfile(p):
            return json_error("上传文件不存在", code=404)
        os.remove(p)
        logger.info(f"已删除上传文件: {p}")
        return json_ok({"lib": lib, "name": name})
    except Exception as e:
        logger.exception('api_upload_delete exception:')
        return json_error(str(e))


async def api_train(request):
    """POST /api/libs/{lib}/train — 把库内上传的视频加入训练队列（串行执行）
    body: {"files": ["a.mp4", ...]  # 不传=全部未训练
           "model": "wav2lip"|"musetalk",
           "img_size": 256, "bbox_shift": 0, "version": "v15"}
    训练产物落在 <lib>/<视频名去扩展名>/，成功后自动刷新 playlist.json 纳入新片段。
    """
    try:
        from server.task_manager import task_manager

        lib = request.match_info['lib']
        ok, err = _safe(lib)
        if not ok:
            return json_error(err)
        base = lib_path(lib)
        if not os.path.isdir(base):
            return json_error(f"库「{lib}」不存在", code=404)

        p = await request.json() if request.can_read_body else {}
        # 训练类型优先级：请求里显式指定 > 建库时设的默认值 > wav2lip
        meta_model = read_lib_meta(lib).get('model', '')
        model = p.get('model') or meta_model or 'wav2lip'
        if model not in ('wav2lip', 'musetalk'):
            return json_error("model 只能是 wav2lip 或 musetalk")
        img_size = int(p.get('img_size', 256))
        bbox_shift = int(p.get('bbox_shift', 0))
        version = p.get('version', 'v15')

        ups = list_uploads(lib)
        want = p.get('files')
        if want:
            ups = [u for u in ups if u['file'] in set(want)]
        # 只训练未训练过的
        todo = [u for u in ups if not u['trained_as']]
        if not todo:
            return json_error("没有待训练的视频（都已训练过，或 uploads/ 为空）")

        queued, skipped = [], []
        for u in todo:
            clip_name = u['stem']
            # 片段名可能含不安全字符，做一次规范化
            safe_name = re.sub(r'[^\w\u4e00-\u9fff\-]', '_', clip_name)[:48] or f"clip{len(queued)+1}"
            target = os.path.join(base, safe_name)
            if os.path.isdir(target) and os.path.isfile(os.path.join(target, 'coords.pkl')):
                skipped.append(u['file'])
                continue
            # ⚠️ genavatar 内部会做 os.path.join(save_path, avatar_id)，
            #    所以这里 save_path 必须传「库根目录」，avatar_id 传「片段名」，
            #    产物才会落在 <lib>/<clip>/。若传 save_path=<lib>/<clip> 会双层嵌套。
            video_path = os.path.join(base, UPLOAD_DIRNAME, u['file'])
            params = {
                'video_path': video_path,
                'save_path': base,
            }
            if model == 'wav2lip':
                params.update({
                    'img_size': img_size,
                    'pads': [0, 10, 0, 0],
                    'nosmooth': False,
                    'face_det_batch_size': 1,
                })
            else:
                params.update({
                    'bbox_shift': bbox_shift,
                    'extra_margin': 10,
                    'parsing_mode': 'jaw',
                    'version': version,
                    'face_det_batch_size': 1,
                })
            tid = task_manager.add_task(model, safe_name, params)
            queued.append({'file': u['file'], 'clip': safe_name, 'task_id': tid})

        logger.info(f"库「{lib}」训练队列已入队 {len(queued)} 个任务: "
                    f"{[q['clip'] for q in queued]}")
        return json_ok({
            "lib": lib,
            "model": model,
            "queued": queued,
            "skipped": skipped,
            "note": "TaskManager 为串行队列(max_workers=1)，一个完成后再处理下一个",
        })
    except Exception as e:
        logger.exception('api_train exception:')
        return json_error(str(e))


async def api_lib_playlist(request):
    """POST /api/libs/{lib}/playlist — 设置库的片段链
    body: {"clips": ["clip1","clip2"]   # 显式列出
           "mode": "all"}               # 或用 mode=all 表示库内全部片段（自动）
    写入 <lib>/playlist.json，并建兼容文件指向第一段。
    """
    try:
        lib = request.match_info['lib']
        ok, err = _safe(lib)
        if not ok:
            return json_error(err)
        base = lib_path(lib)
        if not os.path.isdir(base):
            return json_error(f"库「{lib}」不存在", code=404)

        p = await request.json()
        mode = p.get('mode')
        if mode == 'all':
            clips = [c['name'] for c in list_clips(lib) if c['trained']]
        else:
            clips = p.get('clips') or []
        if not clips:
            return json_error("没有可用的片段（请先训练）")

        # 校验都在库内且已训练
        avail = {c['name']: c for c in list_clips(lib)}
        bad = [c for c in clips if c not in avail or not avail[c]['trained']]
        if bad:
            return json_error(f"以下片段不存在或未训练完成：{bad}")

        # ⚠️ 关键校验：同一个素材链里的片段必须**同一种模型**。
        #    原因：--model 是服务启动时的单选全局参数，一次只能加载一种模型
        #    （wav2lip 与 musetalk 显存相加 7.2+7.1=14.3GB > 16GB 装不下）。
        #    wav2lip 的片段只有 full_imgs/face_imgs，musetalk 只有 full_imgs/mask/latents，
        #    混在一条链里播放会在切换段时找不到 face_imgs 或 latents.pt 而崩帧。
        kinds = {}
        for c in clips:
            kinds.setdefault(avail[c]['kind'], []).append(c)
        if len(kinds) > 1:
            parts = []
            for k, names in kinds.items():
                label = {'wav2lip': 'wav2lip', 'musetalk': 'musetalk'}.get(k, k)
                parts.append(f"{label}（{len(names)} 段：{', '.join(names[:4])}"
                             + ("…" if len(names) > 4 else "") + "）")
            return json_error(
                "同一条素材链不能混用不同模型的片段，请分开编排。<br>"
                + "<br>".join(parts)
                + f"<br><br>提示：库内既有 wav2lip 又有 musetalk 片段时，"
                  f"先在片段列表里按类型筛选，再勾选同一类型的片段编排。")

        entry = int(p.get('entry', 0))
        if entry < 0 or entry >= len(clips):
            return json_error(f"entry 越界：{entry}")

        # 播放模式：
        #   sequence = 顺序循环（按 clips 列表顺序，播完最后一段回到第一段）
        #   shuffle  = 随机（每段播完随机挑一段，排除刚播过的那段）
        #   weight   = 随机但「本轮还没播过的段优先」= 时间点越靠后的段概率越大，
        #              保证一轮内每段都出现一次（避免长段被反复抽到、短段饿死）
        mode = p.get('mode')
        mode = mode if mode in ('sequence', 'shuffle', 'weight') else 'shuffle'
        if len(clips) <= 1:
            mode = 'sequence'   # 单段时顺序/随机等价

        cfg = {"segments": clips, "entry": entry, "mode": mode, "_lib": lib}
        pl_path = os.path.join(base, PLAYLIST_NAME)
        with open(pl_path, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        logger.info(f"库「{lib}」素材链已保存: {cfg}")

        warn = _ensure_lib_compat(base, clips[0])

        # 保存后**尝试实时生效**：对已存在的会话热重载素材链。
        # 未命中（没有会话在跑该库）不算错误 —— 下次 /offer 新建会话时自然读取新配置。
        reloaded = []
        try:
            reloaded = await _reload_sessions_for_lib_async(lib)
        except Exception as e:
            logger.warning(f"素材链热重载失败（不影响保存）：{e}")

        return json_ok({"lib": lib, "playlist": cfg, "warnings": warn,
                        "mode": mode, "reloaded_sessions": reloaded})
    except Exception as e:
        logger.exception('api_lib_playlist exception:')
        return json_error(str(e))


# 运行中的 app.py 模块引用（由 setup_lib_routes 注入）。
# 为什么不能直接 `import app`：app.py 是以 `__main__` 身份运行的，
# 再 `import app` 会**新执行一遍模块顶层**，得到一个空白的第二实例
# （global_avatars={}、load_avatar 未绑定），热重载就会静默失效。
_APP = None


def set_app_module(m):
    """由 server/routes.py 在注册路由时注入运行中的 app 模块（__main__）。"""
    global _APP
    _APP = m


async def _reload_sessions_for_lib_async(lib):
    """异步包装：把耗时的 load_avatar（读几千张 PNG，实测 2~4 秒）
    丢到线程池执行，**绝不阻塞 aiohttp 事件循环**
    —— 否则事件循环卡住会让所有 WebRTC 会话出现数秒停帧（实测 max 帧间隔 3.5s）。
    """
    import asyncio as _aio
    loop = _aio.get_running_loop()
    return await loop.run_in_executor(None, _reload_sessions_for_lib, lib)


def _reload_sessions_for_lib(lib):
    """把库「lib」的新素材链**热重载**到正在运行的会话上（零重启）。

    做法：
      1. 重新调 avatar 模块的 load_avatar(lib) 拿到新的 segments 列表；
      2. 换掉 global_avatars[lib] 缓存；
      3. 对每个 avatar_id == lib 的活跃会话，调用 session.reload_playlist(segments)。

    reload_playlist 只替换「素材段数组 + 播放头」，不重建 ASR/TTS/输出，
    因此**音频与嘴型链条一帧不断**（切换素材不应造成断音）。

    返回被更新的 sessionid 列表。找不到会话时返回 []（不是错误）。
    """
    from server.session_manager import session_manager

    if _APP is None:
        logger.warning("素材链热重载跳过：未注入 app 模块（setup_lib_routes 未调用？）")
        return []

    gv = getattr(_APP, 'global_avatars', None)
    loader = getattr(_APP, 'load_avatar', None)
    if gv is None or loader is None:
        logger.warning("素材链热重载跳过：app 模块缺少 global_avatars / load_avatar")
        return []

    if lib not in gv:
        # 没有被加载过 -> 没有活跃会话在用，无需热重载
        logger.info(f"素材链热重载跳过：库「{lib}」不在 global_avatars 缓存中 "
                    f"（现有键={list(gv.keys())}）")
        return []

    new_avatar = loader(lib)
    gv[lib] = new_avatar

    segs = new_avatar[3] if isinstance(new_avatar, (tuple, list)) and len(new_avatar) == 4 else None

    done = []
    logger.info(f"素材链热重载：库「{lib}」开始，会话数={len(session_manager.sessions)}，"
                f"segs={len(segs) if segs else 0} 段")
    for sid, sess in list(session_manager.sessions.items()):
        if sess is None:
            logger.info(f"  会话 {sid}: None，跳过")
            continue
        sess_av = getattr(sess.opt, 'avatar_id', None)
        if sess_av != lib:
            logger.info(f"  会话 {sid}: avatar_id={sess_av!r} != {lib!r}，跳过")
            continue
        if not hasattr(sess, 'reload_playlist'):
            logger.info(f"  会话 {sid}: 无 reload_playlist，跳过")
            continue
        sess.reload_playlist(segs)
        done.append(sid)
        logger.info(f"素材链热重载：会话 {sid}（库「{lib}」，{len(segs or [])} 段）")
    return done


async def api_lib_reload(request):
    """POST /api/libs/{lib}/reload — 把库的素材链热重载到活跃会话（免重启）。

    保存 playlist 时已自动尝试热重载；本接口用于「训练出新片段后手动刷新」。
    """
    try:
        lib = request.match_info['lib']
        ok, err = _safe(lib)
        if not ok:
            return json_error(err)
        if not os.path.isdir(lib_path(lib)):
            return json_error(f"库「{lib}」不存在", code=404)
        done = await _reload_sessions_for_lib_async(lib)
        if not done:
            return json_ok({"lib": lib, "reloaded_sessions": [],
                            "note": "该库当前没有活跃会话；下次连接时自动生效"})
        return json_ok({"lib": lib, "reloaded_sessions": done})
    except Exception as e:
        logger.exception('api_lib_reload exception:')
        return json_error(str(e))


def _ensure_lib_compat(base, first_clip):
    """让库根目录自身也像一份正常素材（指向第一段），
    否则 load_avatar 会先读 base/coords.pkl 失败。用 junction 零拷贝。
    """
    warn = []
    src = os.path.join(base, first_clip)
    if not os.path.isdir(src):
        warn.append(f"首段 {first_clip} 不存在，兼容文件未建立")
        return warn
    for sub in ('full_imgs', 'face_imgs', 'mask'):
        s = os.path.join(src, sub)
        d = os.path.join(base, sub)
        if not os.path.isdir(s) or os.path.isdir(d):
            continue
        err = _make_junction(d, s)
        if err:
            warn.append(f"{sub} 链接未建立: {err}")
    for f in ('coords.pkl', 'mask_coords.pkl', 'latents.pt'):
        s = os.path.join(src, f)
        d = os.path.join(base, f)
        if os.path.isfile(s) and not os.path.exists(d):
            try:
                shutil.copyfile(s, d)
            except Exception as e:
                warn.append(f"复制 {f} 失败: {e}")
    return warn


async def api_lib_detail(request):
    """GET /api/libs/{lib} — 库详情（片段明细 + 接缝校验 + 训练任务状态）"""
    try:
        from server.task_manager import task_manager
        lib = request.match_info['lib']
        ok, err = _safe(lib)
        if not ok:
            return json_error(err)
        base = lib_path(lib)
        if not os.path.isdir(base):
            return json_error(f"库「{lib}」不存在", code=404)

        clips = list_clips(lib)
        pl_path = os.path.join(base, PLAYLIST_NAME)
        playlist = None
        if os.path.isfile(pl_path):
            with open(pl_path, 'r', encoding='utf-8-sig') as f:
                playlist = json.load(f)

        # 接缝校验（直接按库内片段算，不依赖 playlist）
        detail = []
        prev_last = None
        for c in clips:
            cp = os.path.join(base, c['name'])
            first, last = _first_last_frames(None, cp)
            d = dict(c)
            d['seam_from_prev_mae'] = (_frame_mae(prev_last, first)
                                       if (prev_last and first) else None)
            detail.append(d)
            prev_last = last
        if detail and prev_last:
            f0 = _first_last_frames(None, os.path.join(base, clips[0]['name']))[0]
            detail[-1]['seam_to_first_mae'] = (_frame_mae(prev_last, f0)
                                               if f0 else None)

        # 该库相关的训练任务
        tasks = []
        for t in task_manager.list_tasks():
            if t['avatar_id'] in [c['name'] for c in clips] or \
               t['avatar_id'] in [u['stem'] for u in list_uploads(lib)]:
                tasks.append(t)

        return json_ok({
            "lib": lib,
            "clips": detail,
            "uploads": list_uploads(lib),
            "playlist": playlist,
            "tasks": tasks,
        })
    except Exception as e:
        logger.exception('api_lib_detail exception:')
        return json_error(str(e))


async def api_tasks_all(request):
    """GET /api/libs/tasks — 训练队列总览（串行队列状态）"""
    try:
        from server.task_manager import task_manager
        ts = task_manager.list_tasks()
        running = [t for t in ts if t['status'] == 'running']
        pending = [t for t in ts if t['status'] == 'pending']
        return json_ok({
            "tasks": ts,
            "running": len(running),
            "pending": len(pending),
            "serial": True,
            "note": "TaskManager(max_workers=1)：一个训练完成后才处理下一个",
        })
    except Exception as e:
        logger.exception('api_tasks_all exception:')
        return json_error(str(e))


async def api_clip_frame(request):
    """GET /api/libs/{lib}/clipframe?clip=<name>&which=first|last
    返回库内某个片段的首帧/尾帧 PNG（界面缩略图用）。
    """
    try:
        lib = request.match_info['lib']
        clip = request.query.get('clip', '')
        which = request.query.get('which', 'first')
        for n in (lib, clip):
            ok, err = _safe(n)
            if not ok:
                return json_error(err)
        base = os.path.join(lib_path(lib), clip)
        if not os.path.isdir(base):
            return json_error("片段不存在", code=404)
        first, last = _first_last_frames(None, base)
        p = first if which == 'first' else last
        if not p or not os.path.isfile(p):
            return json_error("找不到帧文件", code=404)
        return web.FileResponse(p)
    except Exception as e:
        logger.exception('api_clip_frame exception:')
        return json_error(str(e))


def setup_lib_routes(app):
    """注册库管理路由（注意顺序：静态段必须先于变量段注册）"""
    app.router.add_get("/api/libs", api_libs_list)
    app.router.add_get("/api/libs/tasks", api_tasks_all)          # 必须在 {lib} 之前
    app.router.add_post("/api/libs", api_lib_create)
    app.router.add_get("/api/libs/{lib}", api_lib_detail)
    app.router.add_delete("/api/libs/{lib}", api_lib_delete)
    app.router.add_get("/api/libs/{lib}/clipframe", api_clip_frame)   # 在 clips/{name} 前
    app.router.add_post("/api/libs/{lib}/clips", api_clip_create)
    app.router.add_delete("/api/libs/{lib}/clips/{name}", api_clip_delete)
    app.router.add_post("/api/libs/{lib}/clips/{name}/rename", api_clip_rename)
    app.router.add_post("/api/libs/{lib}/clips/{name}/move", api_clip_move)
    app.router.add_post("/api/libs/{lib}/upload", api_upload)
    app.router.add_delete("/api/libs/{lib}/upload/{name}", api_upload_delete)
    app.router.add_post("/api/libs/{lib}/upload/{name}/rename", api_upload_rename)
    app.router.add_post("/api/libs/{lib}/rename", api_lib_rename)
    app.router.add_post("/api/libs/{lib}/model", api_lib_set_model)   # 设置库的默认训练类型
    app.router.add_post("/api/libs/{lib}/train", api_train)
    app.router.add_post("/api/libs/{lib}/playlist", api_lib_playlist)
    app.router.add_post("/api/libs/{lib}/reload", api_lib_reload)
    logger.info("库管理接口已注册: /api/libs*")
