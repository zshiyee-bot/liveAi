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
#  Avatar 基类 — 合并自 basereal.py，集成到 Async Pipeline
#

import math
from numpy.typing import NDArray
import torch
import numpy as np
import subprocess
import os
import time
import cv2
import glob
import resampy
import queue
from queue import Queue
from threading import Thread, Event
from io import BytesIO
import soundfile as sf
import asyncio
from enum import Enum
import json
import importlib
import registry

import torch.multiprocessing as mp
from dataclasses import dataclass, field

from av import AudioFrame, VideoFrame
from fractions import Fraction

from utils.logger import logger
from utils.image import read_imgs,mirror_index

# class State(Enum):
#     INIT=0
#     WAIT=1
#     QUESTION=2
#     ANSWER=3

@dataclass
class AudioFrameData:
    data: NDArray[np.float32]
    type: int = 0  # 默认值
    userdata: dict = field(default_factory=dict)

class BaseAvatar:
    def __init__(self, opt):
        self.opt = opt
        self.sample_rate = 16000
        self.chunk = self.sample_rate // (opt.fps*2) # 320 samples per chunk (20ms)
        self.sessionid = self.opt.sessionid

        self.speaking = False
        self.recording = False
        self._record_video_pipe = None
        self._record_audio_pipe = None
        self.width = self.height = 0

        self.custom_audiotype = 0 # 0: normal, 1: sinlence, >1: custom audio
        self.custom_img_cycle = {}
        self.custom_audio_cycle = {}
        self.custom_audio_index = {}
        self.custom_index = {}
        self.msgqueues = []
        # self.custom_opt = {}
        self.__loadcustom()

        self.batch_size = opt.batch_size
        self.res_frame_queue = Queue(self.batch_size*2)
        self.render_event = Event()

        _tts_modules = {
            'edgetts': 'tts.edge',
            'gpt-sovits': 'tts.sovits',
            'xtts': 'tts.xtts',
            # 'cosyvoice': 'tts.cosyvoice',
            # 'fishtts': 'tts.fish',
            'tencent': 'tts.tencent',
            'doubao': 'tts.doubao',
            # 'indextts2': 'tts.indextts2',
            'azuretts': 'tts.azure',
            'qwentts': 'tts.qwentts',
            'omnitts': 'tts.omnitts'
        }

        if opt.tts in _tts_modules:
            importlib.import_module(_tts_modules[opt.tts])
            self.tts = registry.create("tts", opt.tts, opt=opt, parent=self)
        else:
            logger.error(f"TTS module {opt.tts} not found.")

        _output_modules = {
            'webrtc': 'streamout.webrtc',
            'rtcpush': 'streamout.webrtc',
            'rtmp': 'streamout.rtmp',
            'virtualcam': 'streamout.virtualcam'
        }

        # 初始化 Output 模块
        if opt.transport in _output_modules:
            try:
                importlib.import_module(_output_modules[opt.transport])
                self.output = registry.create("streamout", opt.transport, opt=opt, parent=self)
            except ModuleNotFoundError:
                logger.error(f"Output transport module {_output_modules[opt.transport]} not found.")
        else:
            logger.error(f"Output transport {opt.transport} not found in map.")

    # 如果系统没有使用 pipeline，或者为了向后兼容原来的 ttsreal.py
    def put_msg_txt(self, msg, datainfo:dict={}):
        if hasattr(self, 'tts'):
            self.tts.put_msg_txt(msg, datainfo)
    
    def put_audio_frame(self, audio_chunk:NDArray[np.float32], datainfo:dict={}): # 16khz 20ms pcm
        if hasattr(self, 'asr'):
            self.asr.put_audio_frame(audio_chunk, datainfo)

    def put_audio_file(self, filebyte, datainfo:dict={}): 
        input_stream = BytesIO(filebyte)
        stream = self.__create_bytes_stream(input_stream)
        streamlen = stream.shape[0]
        idx = 0
        first = True
        while streamlen >= self.chunk:
            eventpoint = {}
            if first:
                eventpoint = {'status': 'start'}
                first = False
            if streamlen - self.chunk < self.chunk:
                eventpoint = {'status': 'end'}
            eventpoint.update(**datainfo) 
            self.put_audio_frame(stream[idx:idx+self.chunk], eventpoint)
            streamlen -= self.chunk
            idx += self.chunk

    def put_audio_filepath(self, filepath, datainfo:dict={}): 
        stream = self.__create_bytes_stream(filepath)
        streamlen = stream.shape[0]
        idx = 0
        first = True
        while streamlen >= self.chunk:
            eventpoint = {}
            if first:
                eventpoint = {'status': 'start'}
                first = False
            if streamlen - self.chunk < self.chunk:
                eventpoint = {'status': 'end'}
            eventpoint.update(**datainfo) 
            self.put_audio_frame(stream[idx:idx+self.chunk], eventpoint)
            streamlen -= self.chunk
            idx += self.chunk
    
    def __create_bytes_stream(self, byte_stream):
        stream, sample_rate = sf.read(byte_stream) # [T*sample_rate,] float64
        logger.info(f'[INFO]put audio stream {sample_rate}: {stream.shape}')
        stream = stream.astype(np.float32)

        if stream.ndim > 1:
            logger.info(f'[WARN] audio has {stream.shape[1]} channels, only use the first.')
            stream = stream[:, 0]
    
        if sample_rate != self.sample_rate and stream.shape[0] > 0:
            logger.info(f'[WARN] audio sample rate is {sample_rate}, resampling into {self.sample_rate}.')
            stream = resampy.resample(x=stream, sr_orig=sample_rate, sr_new=self.sample_rate)

        return stream

    def flush_talk(self):
        if hasattr(self, 'tts') and hasattr(self.tts, 'flush_talk'):
            self.tts.flush_talk()
        if hasattr(self, 'asr') and hasattr(self.asr, 'flush_talk'):
            self.asr.flush_talk()
        self.custom_audiotype = 0  

    # def flush(self):
    #     self.flush_talk()

    def is_speaking(self) -> bool:
        return self.speaking
    
    def __loadcustom(self):
        if not hasattr(self.opt, 'customopt') or not self.opt.customopt:
            return
        for item in self.opt.customopt:
            logger.info(item)
            input_img_list = glob.glob(os.path.join(item['imgpath'], '*.[jpJP][pnPN]*[gG]'))
            input_img_list = sorted(input_img_list, key=lambda x: int(os.path.splitext(os.path.basename(x))[0]))
            self.custom_img_cycle[item['audiotype']] = read_imgs(input_img_list)
            if item.get('audiopath'):
                self.custom_audio_cycle[item['audiotype']], sample_rate = sf.read(item['audiopath'], dtype='float32')
                self.custom_audio_index[item['audiotype']] = 0
            self.custom_index[item['audiotype']] = 0
            # self.custom_opt[item['audiotype']] = item

    def init_customindex(self):
        self.custom_audiotype = 0
        for key in self.custom_audio_index:
            self.custom_audio_index[key] = 0
        for key in self.custom_index:
            self.custom_index[key] = 0

    def add_msgqueue(self, msgqueue):
        self.msgqueues.append(msgqueue)

    def send_msg(self, msg):
        for q in self.msgqueues:
            q.put(msg)

    def notify(self, eventpoint:dict):
        if eventpoint and eventpoint.get('status'):
            logger.info("notify:%s", eventpoint)
            self.send_msg(json.dumps(eventpoint))

    def start_recording(self):
        if self.recording:
            return
        command = ['ffmpeg',
                    '-y', '-an',
                    '-f', 'rawvideo',
                    '-vcodec','rawvideo',
                    '-pix_fmt', 'bgr24',
                    '-s', "{}x{}".format(self.width, self.height),
                    '-r', str(25),
                    '-i', '-',
                    '-pix_fmt', 'yuv420p', 
                    '-vcodec', "h264",
                    f'temp{self.opt.sessionid}.mp4']
        self._record_video_pipe = subprocess.Popen(command, shell=False, stdin=subprocess.PIPE)

        acommand = ['ffmpeg',
                    '-y', '-vn',
                    '-f', 's16le',
                    '-ac', '1',
                    '-ar', '16000',
                    '-i', '-',
                    '-acodec', 'aac',
                    f'temp{self.opt.sessionid}.aac']
        self._record_audio_pipe = subprocess.Popen(acommand, shell=False, stdin=subprocess.PIPE)

        self.recording = True
    
    def record_video_data(self, image):
        if self.width == 0:
            self.height, self.width, _ = image.shape
        if self.recording:
            self._record_video_pipe.stdin.write(image.tobytes()) #tostring()

    def record_audio_data(self, frame):
        if self.recording:
            self._record_audio_pipe.stdin.write(frame.tobytes())
		
    def stop_recording(self):
        if not self.recording:
            return
        self.recording = False 
        self._record_video_pipe.stdin.close()
        self._record_video_pipe.wait()
        self._record_audio_pipe.stdin.close()
        self._record_audio_pipe.wait()
        
        record_path = os.path.join('data', 'record')
        os.makedirs(record_path, exist_ok=True)
        output_file = os.path.join(record_path, f"{self.opt.sessionid}.mp4")
        
        temp_aac = f"temp{self.opt.sessionid}.aac"
        temp_mp4 = f"temp{self.opt.sessionid}.mp4"
        
        # 用 subprocess 列表形式调用，不用 os.system + 字符串拼接：
        #   ① 路径含空格/中文时字符串拼接会被 shell 撕碎（Windows 命令行走 ANSI 代码页）；
        #   ② 列表形式由 CreateProcessW 以 UTF-16 传参，中文/空格/括号都安全；
        #   ③ 顺带能拿到返回码，失败可落日志（原 os.system 失败是静默的）。
        cmd_combine_audio = ['ffmpeg', '-y', '-i', temp_aac, '-i', temp_mp4,
                             '-c:v', 'copy', '-c:a', 'copy', output_file]
        try:
            _rc = subprocess.run(cmd_combine_audio, check=False).returncode
            if _rc != 0:
                logger.error(f"合并音视频失败 (ffmpeg 返回 {_rc}): {output_file}")
        except Exception as e:
            logger.error(f"合并音视频失败（ffmpeg 不可用？）: {e}")
        
        # 删除临时文件
        try:
            os.remove(temp_aac)
            os.remove(temp_mp4)
        except Exception as e:
            logger.error(f"Error removing temp files: {e}")

    # def mirror_index(self, size, index):
    #     turn = index // size
    #     res = index % size
    #     if turn % 2 == 0:
    #         return res
    #     else:
    #         return size - res - 1 
    
    def get_custom_audio_stream(self, audiotype):
        idx = self.custom_audio_index[audiotype]
        stream = self.custom_audio_cycle[audiotype][idx:idx+self.chunk]
        self.custom_audio_index[audiotype] += self.chunk
        if self.custom_audio_index[audiotype] >= self.custom_audio_cycle[audiotype].shape[0]:
            self.custom_audiotype = 1
        return stream
    
    def set_custom_state(self, audiotype, reinit=True):
        print('set_custom_state:', audiotype)
        if self.custom_audio_index.get(audiotype) is None:
            return
        self.custom_audiotype = audiotype
        if reinit:
            self.custom_audio_index[audiotype] = 0
            self.custom_index[audiotype] = 0

    # ====================== 素材链调度（多段随机拼接） ======================
    # 设计说明
    #   需求：所有素材段共用同一张首尾图 -> 任意段可跳任意段，切换无接缝；
    #        播完一段后随机挑下一段（排除刚播过的那段）；
    #        切换可发生在句子中间（音频不断、嘴型不断，只换"身体来自哪段"）。
    #
    #   实现：本调度器只负责回答一个问题 ——
    #        "当前这一帧应该取哪一段的第几帧？"（返回 (段号, 段内帧号)）
    #   音频与嘴型的连续性由既有链路天然保证：
    #        - 音频走 asr.output_queue，与素材段无关，切换时一帧不断；
    #        - 嘴型由当前音频块驱动 inference_batch，切换不影响。
    #
    #   段列表长度为 1 时（官方单段素材），pick_next 恒返回 0，
    #   行为与改动前完全一致（见 self._seg_cursor 的注释）。

    def init_playlist(self, segments, mode=None):
        """注册素材段列表。segments: list，元素为「一段的全部数组元组」。
        每个段必须自带自己的 frame_list_cycle / face_list_cycle / coord_list_cycle
        （musetalk 另有 mask_list_cycle / input_latent_list_cycle），
        且各数组长度相同 —— 这样 (段号, 段内帧号) 就能同步索引所有数组。
        传入 None 或空表示不使用素材链（回退到原单段行为）。

        mode:
          'sequence' — 顺序循环（0→1→…→n-1→0），永不越界
          'shuffle'  — 随机挑下一段（排除刚播过的那段），等概率
          'weight'   — 随机，但「本轮还没播过的段优先」（时间点越靠后概率越大），
                       保证一轮内每段都出现一次
        """
        self.playlist = list(segments) if segments else []
        self.playlist_index = 0      # 当前段号
        self._seg_cursor = 0         # 段内帧号，只前进不回退
        self._last_seg = None        # 刚播过的段号，下一轮随机时排除
        self._cycle_count = 0        # 完整播完的段数（仅用于日志/统计）
        self._played_round = set()   # 本轮已播过的段号（weight 模式用）
        self.playlist_mode = mode or 'shuffle'
        if self.playlist:
            # 段名列表（供界面显示"当前正在播哪一段"）
            try:
                from avatars.wav2lip_avatar import _load_segments
                self.playlist_names = getattr(_load_segments, 'last_names', None)
            except Exception:
                self.playlist_names = None
            logger.info(f"素材链已启用：共 {len(self.playlist)} 段，模式={self.playlist_mode}，"
                        f"入口=段0，各段帧数={[self._seg_len(i) for i in range(len(self.playlist))]}")


    def reload_playlist(self, segments, mode=None):
        """热重载素材链（免重启、不断音频）。

        只替换「素材段数组 + 播放头 + 模式」，**不重建** ASR / TTS / 输出管线，
        因此当前正在播的音频与嘴型链条不中断。
        与 render 线程的竞争：下面三行赋值都是原子引用替换（GIL 保证），
        render/inference 线程读到的要么是旧列表要么是新列表，不会读到半截状态。
        """
        if segments:
            self.playlist = list(segments)
        if mode:
            self.playlist_mode = mode
        # 播放头重置到入口段第 0 帧（接着播新链）
        self.playlist_index = 0
        self._seg_cursor = 0
        self._last_seg = None
        self._played_round = set()
        # 旧数组引用换掉，让 GC 回收
        try:
            if self.playlist:
                self.frame_list_cycle, self.face_list_cycle, self.coord_list_cycle = self.playlist[0][:3]
        except Exception:
            pass
        logger.info(f"素材链热重载完成：共 {len(self.playlist)} 段，"
                    f"模式={getattr(self, 'playlist_mode', 'shuffle')}")

    def set_playlist_mode(self, mode):
        """切换播放模式（sequence / shuffle / weight），立即生效。"""
        if mode in ('sequence', 'shuffle', 'weight'):
            self.playlist_mode = mode
            self._played_round = set()
            return True
        return False


    def _seg_len(self, seg_index):
        """某段的帧数。以该段自身数组的长度为准。"""
        seg = self.playlist[seg_index]
        # 段元素是数组元组，取第一个数组的长度（各数组长度一致）
        return len(seg[0])

    @property
    def use_playlist(self):
        return bool(getattr(self, 'playlist', None))

    def pick_next(self, cur):
        """按 self.playlist_mode 挑下一段。

        sequence : (cur+1) % n —— 顺序循环，永不越界（"走到尽头"= 回到第一段）
        shuffle  : 在「非 cur」里等概率随机（无记忆）
        weight   : 在「非 cur」里随机，但优先挑本轮还没播过的段 ——
                   等价于「播得越少、概率越大」，保证一轮内每段都出现一次
        段数==1 时恒返回 0（单段素材：行为等价于原来的顺序循环）。
        """
        n = len(self.playlist)
        if n <= 1:
            return 0

        mode = getattr(self, 'playlist_mode', 'shuffle')

        if mode == 'sequence':
            return (cur + 1) % n

        cand = [i for i in range(n) if i != cur]
        if not cand:            # 理论不可达（n>=2 时 cand 非空）
            return cur

        if mode == 'weight':
            # 本轮还没播过的优先
            fresh = [i for i in cand if i not in self._played_round]
            pool = fresh if fresh else cand
            if not fresh:
                # 一轮播完 -> 重开一轮（把当前段记为已播，避免立刻回到它）
                self._played_round = {cur}
            return int(np.random.choice(pool))

        # shuffle：等概率、无记忆
        return int(np.random.choice(cand))

    def get_frame_index(self):
        """取当前帧的 (段号, 段内帧号)，并推进播放头。
        调用一次 = 产出一帧。静音分支与推理分支都调用本方法，
        保证两条路径的播放头行为一致。
        """
        if not self.use_playlist:
            return 0, None      # 回退：调用方用原来的 mirror_index

        # 段播完 -> 挑下一段，重新从该段第 0 帧开始
        if self._seg_cursor >= self._seg_len(self.playlist_index):
            self._last_seg = self.playlist_index
            self._played_round.add(self.playlist_index)
            self._cycle_count += 1
            nxt = self.pick_next(self.playlist_index)
            logger.info(f"素材段切换：段{self.playlist_index} 播完({self._seg_cursor}帧) "
                        f"-> 段{nxt}（模式={getattr(self, 'playlist_mode', 'shuffle')}，"
                        f"已播完 {self._cycle_count} 段）")
            self.playlist_index = nxt
            self._seg_cursor = 0

        seg_i, frame_i = self.playlist_index, self._seg_cursor
        self._seg_cursor += 1
        return seg_i, frame_i

    def get_current_segment(self):
        """当前段的所有数组（元组）。供 inference/paste_back 按段取数组。"""
        if not self.use_playlist:
            return None
        return self.playlist[self.playlist_index]

    # ========================== 核心渲染及 Pipeline 桥接 ==========================
    def _resolve_length(self):
        """播放头用的长度：优先素材链当前段，否则用原有 frame_list_cycle。"""
        if self.use_playlist:
            return self._seg_len(self.playlist_index)
        if hasattr(self, 'frame_list_cycle'):
            return len(self.frame_list_cycle)
        return 1

    def get_avatar_length(self):
        if hasattr(self, 'frame_list_cycle'):
            return len(self.frame_list_cycle)
        return 1
        
    def inference(self, quit_event):
        length = self.get_avatar_length()
        index = 0
        count = 0
        counttime = 0
        last_speaking = False

        # syncnet_T = 12  # 时间步
        # weight_dtype = torch.float16  # 数据类型
        # infernum = 0
        logger.info('start inference')
        while not quit_event.is_set():
            starttime = time.perf_counter()
            audiofeat_batch = []
            try:
                audiofeat_batch = self.asr.feat_queue.get(block=True, timeout=1)
            except queue.Empty:
                continue
                
            is_all_silence = True
            audio_frames: list[AudioFrameData] = []
            for _ in range(self.batch_size * 2):
                audioframe:AudioFrameData = self.asr.output_queue.get()
                if audioframe.type == 0:
                    is_all_silence = False               
                audio_frames.append(audioframe)

             # 检测状态变化
            current_speaking = not is_all_silence

            if is_all_silence: #全为静音数据，只需要取fullimg，不需要推理
                for i in range(self.batch_size):
                    if self.use_playlist:
                        # 素材链：静音期同样推进播放头（架构上保留，
                        # 当前直播全程说话，此分支通常不触发）
                        seg_i, frame_i = self.get_frame_index()
                        seg = self.get_current_segment()
                        idx = (seg_i, frame_i)
                        self.res_frame_queue.put((None, audio_frames[i*2:i*2+2], idx))
                    else:
                        idx = mirror_index(length, index)
                        self.res_frame_queue.put((None, audio_frames[i*2:i*2+2], idx))
                        index = index + 1
            else:
                if current_speaking and not last_speaking and self.custom_index.get(1) is not None: #从静音到说话切换,并且有自定义静态视频
                    index = 0
                t = time.perf_counter()

                if self.use_playlist:
                    # 素材链：本批要产出的帧，逐帧问调度器"取哪段第几帧"
                    idx_list = [self.get_frame_index() for _ in range(len(audiofeat_batch))]
                else:
                    idx_list = [mirror_index(length, index + i) for i in range(len(audiofeat_batch))]

                pred = self.inference_batch(idx_list, audiofeat_batch)

                counttime += (time.perf_counter() - t)
                count += self.batch_size
                if count >= 100:
                    logger.info(f"------actual avg infer fps:{count/counttime:.4f}")
                    count = 0
                    counttime = 0
                for i, res_frame in enumerate(pred):
                    self.res_frame_queue.put((res_frame, audio_frames[i*2:i*2+2], idx_list[i]))
                    index = index + 1
                    
            if current_speaking != last_speaking:
                logger.info(f"inference 状态切换：{'说话' if last_speaking else '静音'} → {'说话' if current_speaking else '静音'}")
                last_speaking = current_speaking         
        logger.info('baseavatar inference thread stop')

    def process_frames(self,quit_event):
        enable_transition = False  # 设置为False禁用过渡效果，True启用
        
        _last_speaking = False
        _transition_start = time.time()
        if enable_transition:
            _transition_duration = 0.1  # 过渡时间
            _last_silent_frame = None  # 静音帧缓存
            _last_speaking_frame = None  # 说话帧缓存

        self.output.start()
        
        while not quit_event.is_set():
            try:
                audio_frames: list[AudioFrameData]
                res_frame,audio_frames,idx = self.res_frame_queue.get(block=True, timeout=1)
            except queue.Empty:
                continue
            
            # 检测状态变化
            current_speaking = not (audio_frames[0].type!=0 and audio_frames[1].type!=0)
            if current_speaking != _last_speaking:
                logger.info(f"状态切换：{'说话' if _last_speaking else '静音'} → {'说话' if current_speaking else '静音'}")
                _transition_start = time.time()
            _last_speaking = current_speaking

            if audio_frames[0].type!=0 and audio_frames[1].type!=0: #全为静音数据，只需要取fullimg
                self.speaking = False
                audiotype = audio_frames[0].type
                if self.custom_index.get(audiotype) is not None: #有自定义视频
                    mirindex = mirror_index(len(self.custom_img_cycle[audiotype]),self.custom_index[audiotype])
                    target_frame = self.custom_img_cycle[audiotype][mirindex]
                    self.custom_index[audiotype] += 1
                elif self.use_playlist and isinstance(idx, tuple):
                    # 素材链：静音期取当前段的全身图（架构上保留该路径）
                    target_frame = self.get_current_segment()[0][idx[1]]
                else:
                    target_frame = self.frame_list_cycle[idx]
                
                if enable_transition:
                    # 说话→静音过渡
                    if time.time() - _transition_start < _transition_duration and _last_speaking_frame is not None:
                        alpha = min(1.0, (time.time() - _transition_start) / _transition_duration)
                        combine_frame = cv2.addWeighted(_last_speaking_frame, 1-alpha, target_frame, alpha, 0)
                    else:
                        combine_frame = target_frame
                    # 缓存静音帧
                    _last_silent_frame = combine_frame.copy()
                else:
                    combine_frame = target_frame
            else:
                self.speaking = True
                try:
                    current_frame = self.paste_back_frame(res_frame,idx)
                except Exception as e:
                    logger.warning(f"paste_back_frame error: {e}")
                    continue
                if enable_transition:
                    # 静音→说话过渡
                    if time.time() - _transition_start < _transition_duration and _last_silent_frame is not None:
                        alpha = min(1.0, (time.time() - _transition_start) / _transition_duration)
                        combine_frame = cv2.addWeighted(_last_silent_frame, 1-alpha, current_frame, alpha, 0)
                    else:
                        combine_frame = current_frame
                    # 缓存说话帧
                    _last_speaking_frame = combine_frame.copy()
                else:
                    combine_frame = current_frame

            # 原上游在此处用 cv2.putText 往每帧左上角烧录 "LiveTalking" 水印：
            #     cv2.putText(combine_frame, "LiveTalking", (10, 20),
            #                 cv2.FONT_HERSHEY_SIMPLEX, 0.3, (128,128,128), 1)
            # 已按需求移除（水印会直接出现在 WebRTC/RTMP/虚拟摄像头推流画面上）。
            # 如需恢复，取消上面两行注释即可。
            # 注：README.md L220 / README-EN.md L211 声明"发布在 B站/视频号/抖音等平台
            #     的视频需带上 LiveTalking 水印和标识"——移除水印仅用于本地调试/自用，
            #     对外发布请自行遵守该声明，或自行叠加自己的水印。

            # 使用统一输出接口推送视频帧
            self.output.push_video_frame(combine_frame)
            self.record_video_data(combine_frame)

            for audio_frame in audio_frames:
                #frame,type,eventpoint = audio_frame
                frame = (audio_frame.data * 32767).astype(np.int16)

                # 使用统一输出接口推送音频帧
                self.output.push_audio_frame(frame, audio_frame.userdata)
                self.record_audio_data(frame)
                
            # if self.opt.transport == 'virtualcam' and hasattr(self.output, '_cam') and self.output._cam:
            #     self.output._cam.sleep_until_next_frame()

        self.output.stop()
        logger.info('baseavatar process_frames thread stop') 

    def render(self,quit_event):
        self.quit_event = quit_event
        
        self.init_customindex()
        self.tts.render(quit_event)

        infer_quit_event = mp.Event()
        infer_thread = Thread(target=self.inference, args=(infer_quit_event,))
        infer_thread.start()
        
        process_quit_event = Event()
        process_thread = Thread(target=self.process_frames, args=(process_quit_event,))
        process_thread.start()

        count=0
        totaltime=0
        _starttime=time.perf_counter()
        _totalframe=0
        while not quit_event.is_set(): 
            t = time.perf_counter()
            self.asr.run_step()

            buffer_size = self.output.get_buffer_size() if hasattr(self.output, 'get_buffer_size') else 0
            if buffer_size >= 5:
                logger.debug('sleep qsize=%d', buffer_size)
                time.sleep(0.04 * buffer_size * 0.8)
        logger.info('baseavatar render thread stop')

        infer_quit_event.set()
        infer_thread.join()

        process_quit_event.set()
        process_thread.join()

