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
#  MuseTalk 数字人 — 迁移自 musereal.py + museasr.py
#

import math
import torch
import numpy as np

import subprocess
import os
import time
import torch.nn.functional as F
import cv2
import glob
import pickle

import queue
from queue import Queue
from threading import Thread, Event
import torch.multiprocessing as mp

from avatars.musetalk.utils.utils import get_file_type,get_video_fps,datagen
from avatars.musetalk.myutil import get_image_blending
from avatars.musetalk.utils.utils import load_all_model
from avatars.musetalk.whisper.audio2feature import Audio2Feature

from avatars.audio_features.whisper import WhisperASR
import asyncio
from av import AudioFrame, VideoFrame
from avatars.base_avatar import BaseAvatar

from tqdm import tqdm
from utils.logger import logger
from utils.image import read_imgs, load_frames, mirror_index


def _sorted_imgs(d):
    """取目录里的图片并按文件名数字排序（见 utils.image.sorted_imgs）。"""
    from utils.image import sorted_imgs
    return sorted_imgs(d)
from utils.device import initialize_device
from registry import register

device = initialize_device()
logger.info('Using {} for inference.'.format(device))

def load_model():
    # load model weights
    vae, unet, pe = load_all_model()
    #device = torch.device("cuda" if torch.cuda.is_available() else ("mps" if (hasattr(torch.backends, "mps") and torch.backends.mps.is_available()) else "cpu"))
    timesteps = torch.tensor([0], device=device)
    pe = pe.half().to(device)
    vae.vae = vae.vae.half().to(device)
    unet.model = unet.model.half().to(device)
    # Initialize audio processor and Whisper model
    audio_processor = Audio2Feature(model_path="./models/whisper")
    return vae, unet, pe, timesteps, audio_processor

def load_segment(base, mode=None):
    """加载单段 musetalk 素材，返回 5 元组（供素材链复用）。
    数组顺序与 load_avatar 一致：
      (frame_list_cycle, mask_list_cycle, coord_list_cycle,
       mask_coords_list_cycle, input_latent_list_cycle)
    mode: None=按本段自己判断；'eager'/'lazy'=由素材链整体预算决定（见 _load_segments）
    """
    with open(os.path.join(base, 'coords.pkl'), 'rb') as f:
        coords = pickle.load(f)
    latents = torch.load(os.path.join(base, 'latents.pt'))
    fl = _sorted_imgs(os.path.join(base, 'full_imgs'))
    frames = load_frames(fl, mode=mode)
    with open(os.path.join(base, 'mask_coords.pkl'), 'rb') as f:
        mask_coords = pickle.load(f)
    ml = _sorted_imgs(os.path.join(base, 'mask'))
    masks = load_frames(ml, mode=mode)
    n = min(len(frames), len(masks), len(coords), len(mask_coords), len(latents))
    return (frames[:n], masks[:n], coords[:n], mask_coords[:n], latents[:n])


def load_avatar(avatar_id):
    """加载素材。
    返回 (frame_list_cycle, mask_list_cycle, coord_list_cycle,
          mask_coords_list_cycle, input_latent_list_cycle, segments)。

    当 avatar 目录下存在 playlist.json 时启用「素材链」：segments 为多段列表，
    每段是一个 5 元组；此时前五项返回 segments[0]（向后兼容）。
    """
    avatar_path = f"./data/avatars/{avatar_id}"
    full_imgs_path = f"{avatar_path}/full_imgs" 
    coords_path = f"{avatar_path}/coords.pkl"
    latents_out_path= f"{avatar_path}/latents.pt"
    video_out_path = f"{avatar_path}/vid_output/"
    mask_out_path =f"{avatar_path}/mask"
    mask_coords_path =f"{avatar_path}/mask_coords.pkl"
    avatar_info_path = f"{avatar_path}/avator_info.json"

    from avatars.wav2lip_avatar import _load_segments
    input_latent_list_cycle = torch.load(latents_out_path)
    with open(coords_path, 'rb') as f:
        coord_list_cycle = pickle.load(f)

    # 先看素材链：有链就用链里的段，不再单独加载一份单段素材
    # （上游会先加载单段再加载链，同一份素材读两遍）
    segments = _load_segments(avatar_path, avatar_id, 'musetalk')
    if segments:
        return segments[0][0], segments[0][1], segments[0][2], segments[0][3], segments[0][4], segments

    input_img_list = _sorted_imgs(full_imgs_path)
    frame_list_cycle = load_frames(input_img_list)
    with open(mask_coords_path, 'rb') as f:
        mask_coords_list_cycle = pickle.load(f)
    input_mask_list = _sorted_imgs(mask_out_path)
    mask_list_cycle = load_frames(input_mask_list)
    return frame_list_cycle,mask_list_cycle,coord_list_cycle,mask_coords_list_cycle,input_latent_list_cycle,None


@torch.no_grad()
def warm_up(batch_size,model):
    # 预热函数
    print('warmup model...')
    vae, unet, pe, timesteps, audio_processor = model
    whisper_batch = np.ones((batch_size, 50, 384), dtype=np.uint8)
    latent_batch = torch.ones(batch_size, 8, 32, 32).to(unet.device)

    audio_feature_batch = torch.from_numpy(whisper_batch)
    audio_feature_batch = audio_feature_batch.to(device=unet.device, dtype=unet.model.dtype)
    audio_feature_batch = pe(audio_feature_batch)
    latent_batch = latent_batch.to(dtype=unet.model.dtype)
    pred_latents = unet.model(latent_batch,
                              timesteps,
                              encoder_hidden_states=audio_feature_batch).sample
    vae.decode_latents(pred_latents)    

@register("avatar", "musetalk")
class MuseReal(BaseAvatar):
    @torch.no_grad()
    def __init__(self, opt, model, avatar):
        super().__init__(opt)

        #self.fps = opt.fps # 20 ms per frame

        # self.batch_size = opt.batch_size
        # self.idx = 0
        # self.res_frame_queue = mp.Queue(self.batch_size*2)

        self.vae, self.unet, self.pe, self.timesteps, self.audio_processor = model

        # avatar 可能是 5 元组（单段，向后兼容）或 6 元组（末位是素材链 segments）
        if len(avatar) == 6:
            (self.frame_list_cycle, self.mask_list_cycle, self.coord_list_cycle,
             self.mask_coords_list_cycle, self.input_latent_list_cycle, segs) = avatar
            # ⚠️ 元数据优先取「返回对象自带的那份」（thread-safe）；
            # 曾经用 _load_segments.last_* 全局属性 → 并发/交错加载会串味，
            # 表现为 groups 跨度只覆盖段0，播放头永远回段0（已复现的线上 bug）。
            _mode = getattr(segs, 'mode', None)
            _groups = getattr(segs, 'groups', None)
            if _mode is None or _groups is None:
                try:
                    from avatars.wav2lip_avatar import _load_segments
                    if _mode is None:
                        _mode = getattr(_load_segments, 'last_mode', None)
                    if _groups is None:
                        _groups = getattr(_load_segments, 'last_groups', None)
                except Exception:
                    pass
            self.init_playlist(segs, mode=_mode, groups=_groups)
        else:
            (self.frame_list_cycle, self.mask_list_cycle, self.coord_list_cycle,
             self.mask_coords_list_cycle, self.input_latent_list_cycle) = avatar
            self.init_playlist(None)
        if self.use_playlist:
            (self.frame_list_cycle, self.mask_list_cycle, self.coord_list_cycle,
             self.mask_coords_list_cycle, self.input_latent_list_cycle) = self.playlist[0]

        self.asr = WhisperASR(opt,self,self.audio_processor)
        self.asr.warm_up()

    def reload_playlist(self, segments, mode=None, groups=None):
        """热重载素材链（musetalk 版：需要同步 5 个数组，base 版只同步 3 个）。
        逐项分组信息（groups）一并透传给基类，否则热重载会退化成单组。"""
        super().reload_playlist(segments, mode=mode, groups=groups)
        try:
            if self.playlist:
                (self.frame_list_cycle, self.mask_list_cycle, self.coord_list_cycle,
                 self.mask_coords_list_cycle, self.input_latent_list_cycle) = self.playlist[0]
        except Exception:
            pass

    def _seg_arrays(self, idx):
        """按 idx 取 (frames, masks, coords, mask_coords, latents, frame_i)。
        idx 为 tuple(段号, 段内帧号) 时从素材链取；为 int 时用单段数组。"""
        if isinstance(idx, tuple):
            seg_i, frame_i = idx
            seg = self.playlist[seg_i]
            return seg[0], seg[1], seg[2], seg[3], seg[4], frame_i
        return (self.frame_list_cycle, self.mask_list_cycle, self.coord_list_cycle,
                self.mask_coords_list_cycle, self.input_latent_list_cycle, idx)
    

    @torch.no_grad()
    def inference_batch(self, idx_list, audiofeat_batch):
        # idx_list: 本批每帧的索引，元素为 int（单段）或 tuple(段号,帧号)（素材链）
        whisper_batch = np.stack(audiofeat_batch)
        latent_batch = []
        for idx in idx_list:
            _, _, _, _, latents, frame_i = self._seg_arrays(idx)
            latent_batch.append(latents[frame_i])
        latent_batch = torch.cat(latent_batch, dim=0)
        
        audio_feature_batch = torch.from_numpy(whisper_batch)
        audio_feature_batch = audio_feature_batch.to(device=self.unet.device,
                                                        dtype=self.unet.model.dtype)
        audio_feature_batch = self.pe(audio_feature_batch)
        latent_batch = latent_batch.to(dtype=self.unet.model.dtype)

        pred_latents = self.unet.model(latent_batch, 
                                    self.timesteps, 
                                    encoder_hidden_states=audio_feature_batch).sample
        pred = self.vae.decode_latents(pred_latents)
        return pred

    def paste_back_frame(self,pred_frame,idx):
        frames, masks, coords, mask_coords_all, _, frame_i = self._seg_arrays(idx)
        bbox = coords[frame_i]
        ori_frame = frames[frame_i].copy()
        x1, y1, x2, y2 = bbox

        res_frame = cv2.resize(pred_frame.astype(np.uint8),(x2-x1,y2-y1))
        mask = masks[frame_i]
        mask_crop_box = mask_coords_all[frame_i]

        combine_frame = get_image_blending(ori_frame,res_frame,bbox,mask,mask_crop_box)
        return combine_frame

