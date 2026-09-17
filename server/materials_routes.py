###############################################################################
#  素材管理 API — 素材扫描 / 校验 / 素材链（playlist.json）读写
#
#  背景
#    LiveTalking 原生只支持「单段素材」：data/avatars/<id>/ 下放一套图，
#    播放头用 mirror_index 在段内循环。
#    本模块为「多段随机拼接」提供管理接口：
#      - 扫描 data/avatars/ 下所有素材，识别类型(wav2lip/musetalk)、帧数、分辨率
#      - 校验「首尾帧一致性」——链式拼接要求 A段尾帧 ≈ B段首帧（同图最佳）
#      - 读写 playlist.json（素材链配置）
#      - 报告当前正在播放的段（供界面实时显示）
#
#  素材链目录结构（以 chain_test 为例）
#    data/avatars/chain_test/
#      playlist.json      <- 本模块管理
#      coords.pkl         <- 兼容用：与 segments[0] 相同（load_avatar 会先读它）
#      full_imgs/          <- 兼容用：junction 或复制自 segments[0]
#      face_imgs/          <- 同上（musetalk 是 mask/）
###############################################################################

import os
import json
import glob
import shutil

from aiohttp import web

from utils.logger import logger


# ─── 扫描结果缓存 ────────────────────────────────────────────────────────────
# 界面每 3 秒轮询 /api/libs（+ /api/materials），每次都要把每个库/片段的帧数、
# 首尾帧、首尾 MAE 算一遍。实测**无缓存时 /api/libs 需 263ms**，其中
# 65% 花在重复解码首尾 PNG 上 —— 这会让界面每次轮询都卡一下、看起来「闪烁」。
# 这里用 (mtime_ns[, size]) 作指纹缓存：帧图生成后不再变动，命中率接近 100%。
_CACHE = {}


AVATARS_ROOT = './data/avatars'
PLAYLIST_NAME = 'playlist.json'

# 各素材类型所需的目录/文件（用于判定"这是哪种模型的素材"）
KIND_SIGNATURES = {
    'wav2lip': {
        'dirs': ['full_imgs', 'face_imgs'],
        'files': ['coords.pkl'],
    },
    'musetalk': {
        'dirs': ['full_imgs', 'mask'],
        'files': ['coords.pkl', 'latents.pt', 'mask_coords.pkl'],
    },
}


def _count_png(d):
    """目录内图片数量（不区分扩展名大小写，与 genavatar 的 glob 保持一致）

    带 mtime 缓存：界面每 3 秒轮询一次 /api/libs，而每个库/片段都要数帧，
    实测无缓存时 /api/libs 要 263ms（大头就是反复 listdir + 解码 PNG）。
    目录 mtime 在增删文件时会变，所以用它做失效判定足够可靠。
    """
    if not os.path.isdir(d):
        return 0
    try:
        mt = os.stat(d).st_mtime_ns
    except OSError:
        mt = None
    ck = ('_count_png', d)
    if mt is not None:
        hit = _CACHE.get(ck)
        if hit and hit[0] == mt:
            return hit[1]
    n = 0
    try:
        for f in os.listdir(d):
            b, e = os.path.splitext(f)
            if e.lower() in ('.png', '.jpg', '.jpeg'):
                n += 1
    except OSError:
        n = 0
    if mt is not None:
        _CACHE[ck] = (mt, n)
    return n


def _probe_image_size(path):
    """读一张图的分辨率（不加载整个文件到内存太久）。
    返回 (w, h) 或 None。用 PIL 避免 opencv 版本差异。
    """
    try:
        from PIL import Image
        with Image.open(path) as im:
            return im.size  # (w, h)
    except Exception:
        return None


def _first_last_frames(kind, base):
    """返回该段的首帧、尾帧绝对路径（用于首尾一致性校验与界面预览）。

    注意：只做 listdir + 排序取首尾，**不解码图片**（解码很贵）。
    带 mtime 缓存以扛住界面轮询。
    """
    d = os.path.join(base, 'full_imgs')
    if not os.path.isdir(d):
        return None, None
    try:
        mt = os.stat(d).st_mtime_ns
    except OSError:
        mt = None
    ck = ('_first_last', d)
    if mt is not None:
        hit = _CACHE.get(ck)
        if hit and hit[0] == mt:
            return hit[1]
    try:
        fs = [os.path.join(d, f) for f in os.listdir(d)
              if os.path.splitext(f)[1].lower() in ('.png', '.jpg', '.jpeg')]
    except OSError:
        return None, None
    if not fs:
        res = (None, None)
    else:

        def key(p):
            b = os.path.splitext(os.path.basename(p))[0]
            try:
                return int(b)
            except ValueError:
                return 0
        fs.sort(key=key)
        res = (fs[0], fs[-1])
    if mt is not None:
        _CACHE[ck] = (mt, res)
    return res


def _file_stamp(p):
    """文件指纹（mtime_ns + size），用于 MAE 缓存失效。"""
    try:
        st = os.stat(p)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _frame_mae(p1, p2, size=(64, 122)):
    """两图的灰度 MAE（缩放后比较，抗压缩噪声）。
    返回 float，越小越像。失败返回 None。

    ⚠️ 这是最贵的一步（每次要解码 2 张 PNG，实测 18 次调用占 0.70s，
    是 /api/libs 慢的主因）。因此按「两张图的 mtime+size」缓存结果 ——
    帧图一旦生成就不再变，缓存命中率接近 100%。
    """
    if not p1 or not p2:
        return None
    s1, s2 = _file_stamp(p1), _file_stamp(p2)
    ck = ('_frame_mae', p1, p2, size)
    if s1 and s2:
        hit = _CACHE.get(ck)
        if hit and hit[0] == (s1, s2):
            return hit[1]
    val = None
    try:
        import numpy as np
        from PIL import Image
        a = Image.open(p1).convert('L').resize(size)
        b = Image.open(p2).convert('L').resize(size)
        x = np.asarray(a, dtype=np.float32)
        y = np.asarray(b, dtype=np.float32)
        val = float(abs(x - y).mean())
    except Exception:
        val = None
    if s1 and s2:
        _CACHE[ck] = ((s1, s2), val)
    return val


def probe_avatar(name):
    """探测单个素材目录，返回描述字典或 None（不是有效素材时）。"""
    base = os.path.join(AVATARS_ROOT, name)
    if not os.path.isdir(base):
        return None

    # 判定类型
    kind = None
    for k, sig in KIND_SIGNATURES.items():
        if all(os.path.isdir(os.path.join(base, d)) for d in sig['dirs']) and \
           all(os.path.isfile(os.path.join(base, f)) for f in sig['files']):
            kind = k
            break
    # full_imgs 是必要条件，即使类型判定不出也算"半成品"
    has_full = os.path.isdir(os.path.join(base, 'full_imgs'))
    if kind is None and not has_full:
        return None

    frames = _count_png(os.path.join(base, 'full_imgs'))
    face_dir = 'face_imgs' if kind == 'wav2lip' else ('mask' if kind == 'musetalk' else None)
    faces = _count_png(os.path.join(base, face_dir)) if face_dir else 0

    # 分辨率：取首帧
    first, last = _first_last_frames(kind, base)
    size = _probe_image_size(first) if first else None
    fsize = _probe_image_size(face_dir and os.path.join(base, face_dir) and
                              _first_png(os.path.join(base, face_dir))) if face_dir else None

    # 首尾帧一致性（链式拼接的关键指标）
    tail_mae = _frame_mae(first, last) if (first and last) else None

    # 是否素材链容器
    pl_path = os.path.join(base, PLAYLIST_NAME)
    is_chain = os.path.isfile(pl_path)
    playlist = None
    if is_chain:
        try:
            with open(pl_path, 'r', encoding='utf-8-sig') as f:
                playlist = json.load(f)
        except Exception as e:
            logger.warning(f"读取 {pl_path} 失败: {e}")

    return {
        'name': name,
        'kind': kind or 'unknown',
        'frames': frames,
        'face_frames': faces,
        'duration_sec': round(frames / 25.0, 2),   # 假设 25fps 播放
        'width': size[0] if size else None,
        'height': size[1] if size else None,
        'face_size': list(fsize) if fsize else None,
        'tail_head_mae': tail_mae,                  # 越大说明首尾越不一致
        'has_coords': os.path.isfile(os.path.join(base, 'coords.pkl')),
        'has_latents': os.path.isfile(os.path.join(base, 'latents.pt')),
        'is_chain': is_chain,
        'playlist': playlist,
        'ready': bool(kind and frames > 0 and (faces > 0 or kind == 'musetalk')),
    }


def _first_png(d):
    if not os.path.isdir(d):
        return None
    fs = [os.path.join(d, f) for f in os.listdir(d)
          if os.path.splitext(f)[1].lower() in ('.png', '.jpg', '.jpeg')]
    if not fs:
        return None

    def key(p):
        b = os.path.splitext(os.path.basename(p))[0]
        try:
            return int(b)
        except ValueError:
            return 0
    fs.sort(key=key)
    return fs[0]


def scan_all():
    """扫描所有素材目录。"""
    if not os.path.isdir(AVATARS_ROOT):
        return []
    out = []
    for name in sorted(os.listdir(AVATARS_ROOT)):
        if name.startswith('.'):
            continue
        info = probe_avatar(name)
        if info:
            out.append(info)
    return out


# ─── HTTP 处理 ────────────────────────────────────────────────────────────

def json_ok(data=None):
    body = {"code": 0, "msg": "ok"}
    if data is not None:
        body["data"] = data
    return web.Response(content_type="application/json",
                        text=json.dumps(body, ensure_ascii=False))


def json_error(msg, code=-1):
    return web.Response(content_type="application/json",
                        text=json.dumps({"code": code, "msg": str(msg)}, ensure_ascii=False))


async def api_materials_list(request):
    """GET /api/materials
    列出所有素材。可选 ?kind=wav2lip|musetalk 过滤。
    ?verify=1 时计算首尾帧一致性（慢，默认不算）。
    """
    try:
        verify = request.query.get('verify', '0') in ('1', 'true', 'yes')
        kind_filter = request.query.get('kind')
        items = scan_all()
        if not verify:
            # 不算 MAE，省时间
            for it in items:
                it.pop('tail_head_mae', None)
        else:
            # 只在需要时逐段精算（含链容器）
            for it in items:
                it['segments_detail'] = _chain_segments_detail(it)
        if kind_filter:
            items = [x for x in items if x['kind'] == kind_filter]
        return json_ok({"materials": items,
                        "root": os.path.abspath(AVATARS_ROOT),
                        "count": len(items)})
    except Exception as e:
        logger.exception('api_materials_list exception:')
        return json_error(str(e))


def _chain_segments_detail(it):
    """对素材链容器，逐个段算首尾 MAE 与段间接缝 MAE，供界面判断接缝质量。"""
    pl = it.get('playlist') or {}
    names = pl.get('segments') or []
    detail = []
    prev_last = None
    for nm in names:
        base = os.path.join(AVATARS_ROOT, nm)
        if not os.path.isdir(base):
            base = os.path.join(AVATARS_ROOT, it['name'], nm)
        if not os.path.isdir(base):
            detail.append({'name': nm, 'error': '目录不存在'})
            continue
        first, last = _first_last_frames(None, base)
        d = {
            'name': nm,
            'frames': _count_png(os.path.join(base, 'full_imgs')),
            'tail_head_mae': _frame_mae(first, last) if (first and last) else None,
            'seam_from_prev_mae': _frame_mae(prev_last, first) if (prev_last and first) else None,
        }
        detail.append(d)
        prev_last = last
    if detail and prev_last:
        # 闭环：最后一段尾帧 -> 第一段首帧
        first0 = _first_last_frames(None, os.path.join(AVATARS_ROOT, names[0]))[0]
        detail[-1]['seam_to_first_mae'] = _frame_mae(prev_last, first0) if first0 else None
    return detail


async def api_materials_get(request):
    """GET /api/materials/{name} — 单个素材详情"""
    try:
        name = request.match_info['name']
        info = probe_avatar(name)
        if not info:
            return json_error(f"素材 '{name}' 不存在或不是有效素材", code=404)
        info['segments_detail'] = _chain_segments_detail(info)
        return json_ok(info)
    except Exception as e:
        logger.exception('api_materials_get exception:')
        return json_error(str(e))


async def api_materials_save_playlist(request):
    """POST /api/materials/playlist
    body: {"name": "chain_test", "segments": ["a","b","c"], "entry": 0}
    写 data/avatars/<name>/playlist.json，并确保兼容文件就位
    （coords.pkl / full_imgs / face_imgs 指向 segments[0]，否则 load_avatar 会先读失败）。
    """
    try:
        params = await request.json()
        name = params.get('name')
        segments = params.get('segments') or []
        entry = int(params.get('entry', 0))
        if not name:
            return json_error("name 必填")
        # 安全：只允许简单目录名，防路径穿越
        if os.path.sep in name or '/' in name or '..' in name:
            return json_error("name 只能是目录名，不含路径分隔符")

        base = os.path.join(AVATARS_ROOT, name)
        if not os.path.isdir(base):
            os.makedirs(base, exist_ok=True)

        # 校验每个段都存在且可用
        bad = []
        for nm in segments:
            cand = os.path.join(AVATARS_ROOT, nm)
            cand_local = os.path.join(base, nm)
            if not (os.path.isdir(cand) or os.path.isdir(cand_local)):
                bad.append(nm)
        if bad:
            return json_error(f"以下段目录不存在：{bad}")
        if not segments:
            return json_error("segments 不能为空")

        if entry < 0 or entry >= len(segments):
            return json_error(f"entry 越界：{entry}，范围 0..{len(segments)-1}")

        cfg = {"segments": segments, "entry": entry}
        pl_path = os.path.join(base, PLAYLIST_NAME)
        with open(pl_path, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        logger.info(f"素材链已保存: {pl_path} -> {cfg}")

        # 兼容文件：load_avatar 会先读 base/coords.pkl 等
        warn = _ensure_compat_files(base, segments[0])
        return json_ok({"name": name, "playlist": cfg, "warnings": warn})
    except Exception as e:
        logger.exception('api_materials_save_playlist exception:')
        return json_error(str(e))


def _make_junction(dst, src):
    """创建目录链接（Windows 用 junction / 其他平台用 symlink）。

    ⚠️ 两个必须注意的坑（踩过）：
      1. **必须传绝对路径**。mklink 是 cmd 内建命令，若路径含正斜杠
         （如 './data/avatars/x'）会被解析成命令开关，报
         `Parameter format not correct - "data"` 并失败（exit=1）。
         => 统一用 os.path.abspath + os.path.normpath。
      2. **必须检查 returncode**。subprocess.run 默认不会因为命令失败抛异常，
         若只 capture_output 不检查，会静默失败（表现为"函数跑了但链接没建"）。
    """
    dst = os.path.normpath(os.path.abspath(dst))
    src = os.path.normpath(os.path.abspath(src))
    try:
        if os.name == 'nt':
            import subprocess
            r = subprocess.run(['cmd', '/c', 'mklink', '/J', dst, src],
                               capture_output=True, text=True, shell=False)
            if r.returncode != 0:
                return (f"mklink 失败(exit={r.returncode}): "
                        f"{(r.stderr or r.stdout or '').strip()}")
            return None
        os.symlink(src, dst)
        return None
    except Exception as e:
        return f"创建链接异常: {e}"


def _ensure_compat_files(base, first_seg):
    """让素材链容器的 base 目录自身也像一份正常素材（指向第一段）。
    load_avatar 会先尝试读 base/coords.pkl、base/full_imgs 等，
    之后才去读 playlist.json。用 junction（Windows）或软链避免复制大文件。
    """
    warn = []
    src = os.path.join(AVATARS_ROOT, first_seg)
    if not os.path.isdir(src):
        src = os.path.join(base, first_seg)
    if not os.path.isdir(src):
        warn.append(f"首段 {first_seg} 目录不存在，兼容文件未建立")
        return warn

    # 目录：junction（Windows 免管理员，不占额外空间）
    for sub in ('full_imgs', 'face_imgs', 'mask'):
        s = os.path.join(src, sub)
        d = os.path.join(base, sub)
        if not os.path.isdir(s):
            continue
        if os.path.isdir(d):
            continue
        err = _make_junction(d, s)
        if err:
            warn.append(f"{sub} 链接未建立: {err}")
    # 文件：复制（很小）
    for f in ('coords.pkl', 'mask_coords.pkl', 'latents.pt'):
        s = os.path.join(src, f)
        d = os.path.join(base, f)
        if os.path.isfile(s) and not os.path.exists(d):
            try:
                shutil.copyfile(s, d)
            except Exception as e:
                warn.append(f"复制 {f} 失败: {e}")
    return warn


async def api_materials_delete(request):
    """DELETE /api/materials/{name} — 删除素材（仅限非链容器，防误删复用段）
    链容器只删 playlist.json（解散链），保留素材本体。
    """
    try:
        name = request.match_info['name']
        if os.path.sep in name or '/' in name or '..' in name:
            return json_error("非法 name")
        base = os.path.join(AVATARS_ROOT, name)
        if not os.path.isdir(base):
            return json_error(f"素材 '{name}' 不存在", code=404)
        pl_path = os.path.join(base, PLAYLIST_NAME)
        if os.path.isfile(pl_path):
            os.remove(pl_path)
            logger.info(f"已解散素材链（删除 {pl_path}）")
            return json_ok({"name": name, "action": "dissolve_chain"})
        return json_error("为安全起见，仅支持删除素材链配置，不删除素材文件。"
                          "如需删除素材，请手工删除目录。", code=403)
    except Exception as e:
        logger.exception('api_materials_delete exception:')
        return json_error(str(e))


async def api_materials_preview(request):
    """GET /api/materials/{name}/frame?which=first|last
    返回首帧/尾帧的 PNG 图片，供界面预览（判断首尾是否一致）。
    """
    try:
        name = request.match_info['name']
        which = request.query.get('which', 'first')
        if os.path.sep in name or '/' in name or '..' in name:
            return json_error("非法 name")
        base = os.path.join(AVATARS_ROOT, name)
        if not os.path.isdir(base):
            return json_error("素材不存在", code=404)
        first, last = _first_last_frames(None, base)
        p = first if which == 'first' else last
        if not p or not os.path.isfile(p):
            return json_error("找不到帧文件", code=404)
        return web.FileResponse(p)
    except Exception as e:
        logger.exception('api_materials_preview exception:')
        return json_error(str(e))


async def api_materials_active(request):
    """GET /api/materials/active — 当前各会话正在播放的段（界面实时显示用）。
    从 session_manager 取会话，读其 avatar 的 playlist 状态。
    """
    try:
        from server.session_manager import session_manager
        out = []
        for sid, av in session_manager.sessions.items():
            if not av:
                continue
            item = {"sessionid": sid,
                    "avatar_id": getattr(getattr(av, 'opt', None), 'avatar_id', ''),
                    "use_playlist": bool(getattr(av, 'use_playlist', False))}
            if item["use_playlist"]:
                pl = getattr(av, 'playlist', [])
                item.update({
                    "playlist_index": getattr(av, 'playlist_index', 0),
                    "seg_cursor": getattr(av, '_seg_cursor', 0),
                    "seg_len": len(pl[getattr(av, 'playlist_index', 0)][0]) if pl else 0,
                    "cycle_count": getattr(av, '_cycle_count', 0),
                    "segments": getattr(av, 'playlist_names', None),
                    "mode": getattr(av, 'playlist_mode', 'shuffle'),
                    "seg_count": len(pl),
                })
            out.append(item)
        return json_ok({"active": out})
    except Exception as e:
        logger.exception('api_materials_active exception:')
        return json_error(str(e))


def setup_materials_routes(app):
    """注册素材管理路由"""
    app.router.add_get("/api/materials", api_materials_list)
    app.router.add_get("/api/materials/active", api_materials_active)
    app.router.add_post("/api/materials/playlist", api_materials_save_playlist)
    app.router.add_get("/api/materials/{name}", api_materials_get)
    app.router.add_delete("/api/materials/{name}", api_materials_delete)
    app.router.add_get("/api/materials/{name}/frame", api_materials_preview)
    logger.info("素材管理接口已注册: /api/materials*")
