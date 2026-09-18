###############################################################################
#  Copyright (C) 2024 LiveTalking@lipku https://github.com/lipku/LiveTalking
#  email: lipku@foxmail.com
# 
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#  
#       http://www.apache.org/licenses/LICENSE-2.0
# 
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
###############################################################################
#
#  Wav2Lip 数字人 — 迁移自 lipreal.py + lipasr.py
#

import math
import torch
import numpy as np

import os
import time
import cv2
import glob
import pickle

import queue
from queue import Queue
from threading import Thread, Event
import torch.multiprocessing as mp

from avatars.audio_features.mel import MelASR
import asyncio
from av import AudioFrame, VideoFrame
from avatars.wav2lip.models import Wav2Lip
from avatars.base_avatar import BaseAvatar

from tqdm import tqdm
from utils.logger import logger
from utils.image import read_imgs, load_frames, probe_frames_bytes, mirror_index
from utils.device import initialize_device
from registry import register

device = initialize_device()
logger.info('Using {} for inference.'.format(device))

def _load(checkpoint_path):
    if device == 'cuda':
        checkpoint = torch.load(checkpoint_path)
    else:
        checkpoint = torch.load(checkpoint_path,
                                map_location=lambda storage, loc: storage)
    return checkpoint

def load_model(path):
    model = Wav2Lip()
    logger.info("Load checkpoint from: {}".format(path))
    checkpoint = _load(path)
    s = checkpoint["state_dict"]
    new_s = {}
    for k, v in s.items():
        new_s[k.replace('module.', '')] = v
    model.load_state_dict(new_s)

    model = model.to(device)
    return model.eval()

def load_avatar(avatar_id):
    """加载素材。
    返回 (frame_list_cycle, face_list_cycle, coord_list_cycle, segments)。

    当avatar目录下存在 playlist.json 时启用「素材链」：segments 为
    多段列表，每段是一个 (frame_list_cycle, face_list_cycle, coord_list_cycle)
    元组；此时前三项返回 segments[0]（向后兼容，仍可单独使用）。
    """
    avatar_path = f"./data/avatars/{avatar_id}"
    full_imgs_path = f"{avatar_path}/full_imgs" 
    face_imgs_path = f"{avatar_path}/face_imgs" 
    coords_path = f"{avatar_path}/coords.pkl"
    
    with open(coords_path, 'rb') as f:
        coord_list_cycle = pickle.load(f)

    # 先看有没有素材链：有的话直接用链里的段，**不再单独加载一份单段素材**。
    # 上游顺序是先加载单段再加载链，素材链头像会把同一份素材读两遍
    # （1080p 长素材就是白读一份 7GB，而且那份立刻被丢弃）。
    segments = _load_segments(avatar_path, avatar_id, 'wav2lip')
    if segments:
        return segments[0][0], segments[0][1], segments[0][2], segments

    input_img_list = _sorted_imgs(full_imgs_path)
    frame_list_cycle = load_frames(input_img_list)
    input_face_list = _sorted_imgs(face_imgs_path)
    face_list_cycle = load_frames(input_face_list)
    return frame_list_cycle, face_list_cycle, coord_list_cycle, None


def _sorted_imgs(d):
    """取目录里的图片并按文件名数字排序（见 utils.image.sorted_imgs）。"""
    from utils.image import sorted_imgs
    return sorted_imgs(d)


def _load_segments(avatar_path, avatar_id, kind):
    """读取 playlist.json 并加载各段素材，返回段列表或 None。

    playlist.json 格式（放在 avatar 目录下）：
      {"segments": ["new111_wav", "new444_wav", "new555_wav"], "entry": 0}
    - segments: 段来源，可以是本 avatar 的子目录名，也可以是
                data/avatars/<其他avatar_id>（便于复用已生成的素材）
    - entry   : 入口段下标（默认 0，即"a1 先播"）
    """
    import json as _json
    pl_path = os.path.join(avatar_path, 'playlist.json')
    if not os.path.isfile(pl_path):
        return None
    with open(pl_path, 'r', encoding='utf-8-sig') as f:
        cfg = _json.load(f)
    # ── 链格式 ────────────────────────────────────────────────────
    # v1: {"segments":["a","b"], "mode":"shuffle"}                  —— 单组（= 一个库）
    # v2: {"groups":[{"lib":"库A","mode":"sequence","clips":["1","2"]}], "mode":"shuffle"}
    #     顶层 mode = 【组间模式】，各组 mode = 【组内模式】→ 两级自由组合
    # 段名统一成「库名/片段名」表达跨库；组没写 lib（或就是本 avatar）时用裸片段名。
    groups_cfg = cfg.get('groups') or None
    names, gidx = [], []
    if groups_cfg:
        for gi, g in enumerate(groups_cfg):
            if not isinstance(g, dict):
                continue
            glib = str(g.get('lib') or '').strip()
            for c in (g.get('clips') or []):
                c = str(c or '').strip()
                if not c:
                    continue
                names.append(c if (not glib or glib == avatar_id) else f"{glib}/{c}")
                gidx.append(gi)
    else:
        names = [str(x) for x in (cfg.get('segments') or [])]
        gidx = [0] * len(names)
    if len(names) < 1:
        return None

    # 两阶段：先把各段的路径列表都探出来，用**整条链的总字节数**决定一次
    # eager/lazy（load_frames_batch 里解释为什么不能每段各判各的），再统一加载。
    resolved = []          # [(name, base, fl, fa, coords)]
    names_used = []
    gidx_used = []         # 每段所属组号（与 names_used 平行）
    for _i, name in enumerate(names):
        # 先看本 avatar 子目录，再退到 data/avatars/<name>
        cand = [os.path.join(avatar_path, name), os.path.join('./data/avatars', name)]
        base = next((c for c in cand if os.path.isdir(c)), None)
        if base is None:
            logger.error(f"素材链：段 '{name}' 目录不存在，已跳过（候选：{cand}）")
            continue
        if kind == 'wav2lip':
            with open(os.path.join(base, 'coords.pkl'), 'rb') as f:
                coords = pickle.load(f)
            fl = _sorted_imgs(os.path.join(base, 'full_imgs'))
            fa = _sorted_imgs(os.path.join(base, 'face_imgs'))
            resolved.append((name, base, fl, fa, coords))
        else:
            resolved.append((name, base, None, None, None))
        names_used.append(name)
        gidx_used.append(gidx[_i])

    if not resolved:
        return None

    # 一次性算出「整条链要占多少内存」：只解码每段首帧，不解码全部
    probe_lists = []
    for _n, base, fl, fa, _c in resolved:
        if fl is not None:
            probe_lists.append(fl)
            probe_lists.append(fa)
        else:
            probe_lists.append(_sorted_imgs(os.path.join(base, 'full_imgs')))
            probe_lists.append(_sorted_imgs(os.path.join(base, 'mask')))
    budget = int(os.environ.get('LT_EAGER_BUDGET_MB', '') or 4096) * 1024 * 1024
    total = sum(probe_frames_bytes(p)[1] for p in probe_lists)
    mode = 'eager' if total <= budget else 'lazy'
    if mode == 'lazy':
        logger.info(f"素材链整体 {total / 2 ** 30:.2f}GB 超过 {budget / 2 ** 30:.2f}GB 预算"
                    f" -> {len(probe_lists)} 个图片序列全部改用惰性加载")

    segs = []
    for name, base, fl, fa, coords in resolved:
        if kind == 'wav2lip':
            frames = load_frames(fl, mode=mode)
            faces = load_frames(fa, mode=mode)
            n = min(len(frames), len(faces), len(coords))
            segs.append((frames[:n], faces[:n], coords[:n]))
        else:
            from avatars.musetalk_avatar import load_segment as _ms_seg
            segs.append(_ms_seg(base, mode=mode))
        logger.info(f"素材链：已加载段 '{name}' -> {len(segs[-1][0])} 帧")

    if not segs:
        return None
    # ── 入口轮转 ──────────────────────────────────────────────────
    # v1：按平铺段号轮转（与改动前逐字一致）
    # v2：按【组/项】轮转 —— 入口段所在项排到最前，且该项从入口段开始；
    #     否则入口会落到别的项里，项内语义就错乱了。
    entry = int(cfg.get('entry', 0))
    if entry and 0 < entry < len(segs):
        if groups_cfg:
            order = []
            for _g in gidx_used:
                if _g not in order:
                    order.append(_g)
            g_entry = gidx_used[entry]
            k = order.index(g_entry)
            order = order[k:] + order[:k]
            perm = []
            for _g in order:
                idxs = [i for i, gg in enumerate(gidx_used) if gg == _g]
                if _g == g_entry:
                    j = idxs.index(entry) if entry in idxs else 0
                    idxs = idxs[j:] + idxs[:j]
                perm.extend(idxs)
        else:
            perm = list(range(len(segs)))[entry:] + list(range(len(segs)))[:entry]
        segs = [segs[i] for i in perm]
        names_used = [names_used[i] for i in perm]
        gidx_used = [gidx_used[i] for i in perm]

    # 项/组元数据（供两级 pick_next 用）：start/end 是**轮转后**的平铺段号
    groups_meta = None
    if groups_cfg:
        groups_meta = []
        for _g in sorted(set(gidx_used)):
            idxs = [i for i, gg in enumerate(gidx_used) if gg == _g]
            src = groups_cfg[_g] if (_g < len(groups_cfg) and isinstance(groups_cfg[_g], dict)) else {}
            meta = {
                'lib': str(src.get('lib') or '').strip() or avatar_id,
                'name': str(src.get('name') or src.get('lib') or '').strip() or avatar_id,
                'mode': src.get('mode') or 'sequence',
                'start': min(idxs), 'end': max(idxs) + 1,
                'clips': [names_used[i] for i in idxs],
            }
            # 逐项可调参数：repeat=重复几轮 / weight=被抽中的权重 / 将来可再加嵌套子组
            for _k, _v in src.items():
                if _k not in ('lib', 'name', 'mode', 'clips', 'start', 'end'):
                    meta[_k] = _v
            groups_meta.append(meta)
        groups_meta.sort(key=lambda x: x['start'])

    # 段名 / 项信息挂在函数属性上，供 init_playlist 取用
    try:
        _load_segments.last_names = names_used
        # 顶层 mode = 项间模式（v1 时它就是唯一模式）
        _load_segments.last_mode = cfg.get('mode') or 'shuffle'
        _load_segments.last_groups = groups_meta
    except Exception:
        pass
    return segs



@torch.no_grad()
def warm_up(batch_size,model,modelres):
    # 预热函数
    logger.info('warmup model...')
    img_batch = torch.ones(batch_size, 6, modelres, modelres).to(device)
    mel_batch = torch.ones(batch_size, 1, 80, 16).to(device)
    model(mel_batch, img_batch)

@register("avatar", "wav2lip")
class LipReal(BaseAvatar):
    @torch.no_grad()
    def __init__(self, opt, model, avatar):
        super().__init__(opt)

        #self.fps = opt.fps # 20 ms per frame
        
        # self.batch_size = opt.batch_size
        # self.idx = 0
        # self.res_frame_queue = Queue(self.batch_size*2)
        self.model = model

        # avatar 可能是 3 元组（单段，向后兼容）或 4 元组（末位是素材链 segments）
        if len(avatar) == 4:
            self.frame_list_cycle, self.face_list_cycle, self.coord_list_cycle, segs = avatar
            _mode = getattr(_load_segments, 'last_mode', None)
            self.init_playlist(segs, mode=_mode,
                               groups=getattr(_load_segments, 'last_groups', None))
        else:
            self.frame_list_cycle, self.face_list_cycle, self.coord_list_cycle = avatar
            self.init_playlist(None)
        # 素材链启用时，播放头跨段，face/coord 必须按段取
        if self.use_playlist:
            self.frame_list_cycle, self.face_list_cycle, self.coord_list_cycle = self.playlist[0]

        self.asr = MelASR(opt,self)
        self.asr.warm_up()
    
    def _seg_arrays(self, idx):
        """按 idx 取 (frames, faces, coords)。
        idx 为 tuple(段号, 段内帧号) 时从素材链取；为 int 时用单段数组。"""
        if isinstance(idx, tuple):
            seg_i, frame_i = idx
            seg = self.playlist[seg_i]
            return seg[0], seg[1], seg[2], frame_i
        return self.frame_list_cycle, self.face_list_cycle, self.coord_list_cycle, idx

    def inference_batch(self, idx_list, audiofeat_batch):
        # idx_list: 本批每帧的索引，元素为 int（单段）或 tuple(段号,帧号)（素材链）
        img_batch = []
        for idx in idx_list:
            _, faces, _, frame_i = self._seg_arrays(idx)
            img_batch.append(faces[frame_i])
        img_batch, audiofeat_batch = np.asarray(img_batch), np.asarray(audiofeat_batch)

        img_masked = img_batch.copy()
        # 遮住下半张脸（wav2lip 输入的惯例）：用 batch 内每帧自身的半高，
        # 与上游 face.shape[0]//2 等价（同一 avatar 的 face 图尺寸一致）。
        _half_h = img_batch.shape[1] // 2
        img_masked[:, _half_h:] = 0

        img_batch = np.concatenate((img_masked, img_batch), axis=3) / 255.
        audiofeat_batch = np.reshape(audiofeat_batch, [len(audiofeat_batch), audiofeat_batch.shape[1], audiofeat_batch.shape[2], 1])
        
        img_batch = torch.FloatTensor(np.transpose(img_batch, (0, 3, 1, 2))).to(device)
        audiofeat_batch = torch.FloatTensor(np.transpose(audiofeat_batch, (0, 3, 1, 2))).to(device)

        with torch.no_grad():
            pred = self.model(audiofeat_batch, img_batch)
        pred = pred.cpu().numpy().transpose(0, 2, 3, 1) * 255.
        return pred

    def paste_back_frame(self,pred_frame,idx):
        frames, _, coords, frame_i = self._seg_arrays(idx)
        bbox = coords[frame_i]
        combine_frame = frames[frame_i].copy()
        y1, y2, x1, x2 = bbox
        res_frame = cv2.resize(pred_frame.astype(np.uint8),(x2-x1,y2-y1))
        combine_frame[y1:y2, x1:x2] = res_frame
        return combine_frame


