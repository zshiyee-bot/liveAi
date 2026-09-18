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

# server.py
from flask import Flask, render_template,send_from_directory,request, jsonify
#from flask_sockets import Sockets
import base64
import json
#import gevent
#from gevent import pywsgi
#from geventwebsocket.handler import WebSocketHandler
import re
import os
import numpy as np
from threading import Thread,Event
#import multiprocessing
import torch.multiprocessing as mp

from aiohttp import web
import aiohttp
import aiohttp_cors
from aiortc import RTCPeerConnection, RTCSessionDescription,RTCIceServer,RTCConfiguration
from aiortc.rtcrtpsender import RTCRtpSender
from server.webrtc import HumanPlayer
from avatars.base_avatar import BaseAvatar
from llm import llm_response
import registry
from server.routes import setup_routes
from server.rtc_manager import RTCManager
from server.session_manager import session_manager

import argparse
import random
import shutil
import asyncio
import torch
from io import BytesIO
from typing import Dict
from utils.logger import logger
import copy
import gc
from dotenv import load_dotenv


app = Flask(__name__)
#sockets = Sockets(app)
opt = None
model = None
global_avatars = {} # avatar_id: payload
        

#####webrtc###############################
# rtc_manager replaces the old pcs set and duplicate offer handlers.
rtc_manager = None

def randN(N)->int:
    '''生成长度为 N的随机数 '''
    min = pow(10, N - 1)
    max = pow(10, N)
    return random.randint(min, max - 1)

def build_avatar_session(sessionid:str, params:dict)->BaseAvatar:
    opt_this = copy.deepcopy(opt)
    opt_this.sessionid = sessionid

    avatar_id = params.get('avatar',opt.avatar_id) 
    ref_audio = params.get('refaudio','') #音色
    ref_text = params.get('reftext','')

    # ─── 按素材目录自动判定模型类型，并（按需）加载权重 ────────────────
    # 启动时不再预先加载任何模型；用户连接时由这里根据素材特征自动决定。
    # 这样不需要用户选模型，也避免同时驻留两个模型撑爆显存。
    from avatars.auto_loader import auto_models, detect_model, ModelLoadError

    # 只有显式传了 model 参数时才覆盖自动判定（保留手动能力，便于排查）
    force_kind = params.get('model') or None
    try:
        kind = force_kind or detect_model(avatar_id)
        model_now, mod = auto_models.get(kind, batch_size=opt.batch_size)
    except ModelLoadError as e:
        # 消息是面向用户的中文，原样抛给上层转成 HTTP 错误
        raise
    opt_this.avatar_id = avatar_id
    opt_this.model = kind

    # 素材缓存按 (模型, 素材) 维度隔离 —— 不同模型不能共用同一份缓存
    cache_key = f"{kind}:{avatar_id}"
    if cache_key not in global_avatars:
        global_avatars[cache_key] = mod.load_avatar(avatar_id)
    avatar_this = global_avatars[cache_key]

    if ref_audio: #请求参数配置了参考音频
        opt_this.REF_FILE = ref_audio
        opt_this.REF_TEXT = ref_text
    custom_config=params.get('custom_config','') #动作编排配置
    if custom_config:
        opt_this.customopt = json.loads(custom_config)

    avatar_session = registry.create("avatar", kind, opt=opt_this, model=model_now, avatar=avatar_this)
    return avatar_session

async def offer(request):
    return await rtc_manager.handle_offer(request)

async def whep(request):
    return await rtc_manager.handle_whep(request)

async def on_shutdown(app):
    await rtc_manager.shutdown()

async def download_record(request):
    sessionid = request.match_info.get('sessionid')
    if not sessionid:
        return web.Response(status=400, text="sessionid is required")
    
    record_file = os.path.join('data', 'record', f"{sessionid}.mp4")
    
    if os.path.exists(record_file):
        return web.FileResponse(record_file)
    else:
        return web.Response(status=404, text="Record not found")


def _raise_priority():
    """把本进程优先级提高一档，减少被其它程序抢占导致的掉帧。

    背景（实测）：这台机器上 DSH Desktop(约 1.4 核)、火绒 HIPS(约 0.5 核)、dwm、
    GameViewerServer 等长期占用 CPU；一旦它们瞬时抢占，渲染线程被换出，
    日志里的 infer/final fps 会从 25 掉到 6~11（GPU 却只有 7% 利用率、30W）。
    提到 ABOVE_NORMAL 后渲染线程被抢占的概率明显降低。
    可用环境变量 LT_PRIORITY=normal|above|high 覆盖（默认 above）。
    """
    try:
        import ctypes
        mode = (os.environ.get('LT_PRIORITY') or 'above').strip().lower()
        cls = {'normal': 0x20, 'idle': 0x40, 'below': 0x4000,
               'above': 0x8000, 'high': 0x80}.get(mode, 0x8000)
        k32 = ctypes.windll.kernel32
        # 注意：ctypes 默认 restype=c_int，会把 64 位进程句柄截断成 32 位，
        # 导致 SetPriorityClass 直接失败（实测返回 0）。必须显式声明句柄类型。
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        k32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        k32.SetPriorityClass.restype = ctypes.c_int
        if k32.SetPriorityClass(k32.GetCurrentProcess(), cls):
            logger.info("进程优先级已设为 %s（LT_PRIORITY 可改；用于抵抗其它程序抢占导致的掉帧）", mode)
        else:
            logger.warning("设置进程优先级失败，保持默认")
    except Exception as e:
        logger.warning("设置进程优先级出错（忽略）: %s", e)


def main():
    global rtc_manager, opt, model
    # 解析命令行参数
    from config import parse_args
    opt = parse_args()
    _raise_priority()

    # ─── 注册 avatar 插件（触发 @register），但**不加载任何权重** ────────
    # 依用户要求：启动时不确定用哪个模型，等用户连接时按素材目录自动判定。
    # 好处：① 启动快（省掉 5~25s 权重加载）；② 显存 0 占用，直到真正要用；
    #       ③ 不需要用户选模型，也不需要 --model 参数。
    import importlib
    from avatars.auto_loader import auto_models, scan_available, MODULE_OF

    _avatar_modules = {
        'musetalk':   'avatars.musetalk_avatar',
        'wav2lip':    'avatars.wav2lip_avatar',
        'ultralight': 'avatars.ultralight_avatar',
    }
    # 全部 import 一次，让 @register 把三个 avatar 类都登记好；
    # 真正加载权重推迟到连接时（auto_models.get）。
    for _name, _path in _avatar_modules.items():
        try:
            importlib.import_module(_path)
        except Exception as _e:
            logger.warning(f"[auto] 跳过 avatar 模块 {_path}：{type(_e).__name__}: {_e}")

    # 有活跃会话时拒绝切换模型。
    # 注意 1：必须只数**已真正建好**的会话（值非 None）——
    #   session_manager.create_session 会先插入一个 None 占位再调用本函数，
    #   若把占位也算进去，那么"第一个连接"会把自己当成活跃会话，
    #   于是永远无法从上一个模型切过来（切换永远被自己拒绝）。
    # 注意 2：判据是「有没有别的会话正活着」，而不是「它用的模型是否等于当前模型」——
    #   切换的目的就是要把**当前模型**卸载掉，所以正在用当前模型的那些会话
    #   恰恰是必须阻止切换的对象。
    auto_models.set_busy_check(lambda: [
        s for s in session_manager.sessions.values() if s is not None
    ])
    # 僵尸会话回收：客户端在 ICE 完成前消失会留下永远 connecting 的会话，
    # 若不清理会一直挡住模型切换（用户只能重启服务）。
    auto_models.set_sweeper(lambda: session_manager.drop_stale_connecting(45.0))

    # 启动时扫描可用素材，把「连哪个 ID 会加载哪个模型」打出来，便于用户对照
    try:
        avail = scan_available()
        logger.info("=" * 62)
        logger.info("[auto] 启动完成，未加载任何模型权重（连接时按素材自动加载）")
        logger.info(f"[auto] 可用的 musetalk 素材（{len(avail['musetalk'])}）："
                    + (", ".join(avail['musetalk'][:12])
                       + (" ..." if len(avail['musetalk']) > 12 else "")
                       if avail['musetalk'] else "（无）"))
        logger.info(f"[auto] 可用的 wav2lip256 素材（{len(avail['wav2lip'])}）："
                    + (", ".join(avail['wav2lip'][:12])
                       + (" ..." if len(avail['wav2lip']) > 12 else "")
                       if avail['wav2lip'] else "（无）"))
        if avail['broken']:
            logger.warning(f"[auto] 有 {len(avail['broken'])} 个目录不可用（缺文件或非 256 素材），"
                           f"列举前 5 个：")
            for _n, _why in list(avail['broken'].items())[:5]:
                logger.warning(f"[auto]   · {_n}：{_why}")
        logger.info("[auto] 网页里「角色 ID」填上面的素材名即可，模型会自动匹配")
        logger.info("=" * 62)
    except Exception as _e:
        logger.warning(f"[auto] 素材扫描失败（不影响启动）：{type(_e).__name__}: {_e}")

    # init rtc manager
    session_manager.set_max_session(opt.max_session)
    session_manager.init_builder(build_avatar_session)
    rtc_manager = RTCManager(opt)
    # share avatar_sessions (RTCManager handles it but routes.py expects it)

    # 虚拟摄像头或 RTMP 模式：启动后台渲染线程
    if opt.transport == 'virtualcam' or opt.transport == 'rtmp':
        thread_quit = Event()
        params = {}
        # session 0 for virtualcam
        session_manager.add_session('0', build_avatar_session('0', params))
        rendthrd = Thread(target=session_manager.get_session('0').render, args=(thread_quit,))
        rendthrd.start()
        if opt.transport == 'virtualcam':
            logger.info("[VirtualCam] Virtual camera output enabled - digital human will be rendered to virtual camera")

    #############################################################################
    appasync = web.Application(client_max_size=1024**2*100)
    appasync["llm_response"] = llm_response
    appasync["opt"] = opt
    appasync["rtc_manager"] = rtc_manager

    appasync.on_shutdown.append(on_shutdown)
    appasync.router.add_post("/offer", offer)
    appasync.router.add_get("/record/{sessionid}", download_record)

    # 注册 server/routes.py 中的通用 API 路由
    setup_routes(appasync)

    # Configure default CORS settings.
    cors = aiohttp_cors.setup(appasync, defaults={
            "*": aiohttp_cors.ResourceOptions(
                allow_credentials=True,
                expose_headers="*",
                allow_headers="*",
            )
        })
    # Configure CORS on all routes.
    for route in list(appasync.router.routes()):
        cors.add(route)

    # /whep 注册在 CORS 之后：自行管理 OPTIONS，避免与 aiohttp_cors 冲突
    whep_resource = appasync.router.add_resource('/whep')
    whep_resource.add_route('POST', whep)
    whep_resource.add_route('OPTIONS', lambda _: web.Response(status=200))

    logger.info('start http server; http://<serverip>:'+str(opt.listenport))
    # logger.info('如果使用webrtc，推荐访问webrtc集成前端: http://<serverip>:'+str(opt.listenport)+'/dashboard.html')
    def run_server(runner):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(runner.setup())
        site = web.TCPSite(runner, '0.0.0.0', opt.listenport)
        loop.run_until_complete(site.start())
        if opt.transport=='rtcpush':
            for k in range(opt.max_session):
                push_url = opt.push_url
                if k!=0:
                    push_url = opt.push_url+str(k)
                loop.run_until_complete(rtc_manager.handle_rtcpush(push_url, str(k)))
        loop.run_forever()    
    #Thread(target=run_server, args=(web.AppRunner(appasync),)).start()
    run_server(web.AppRunner(appasync))

    #app.on_shutdown.append(on_shutdown)
    #app.router.add_post("/offer", offer)

    # print('start websocket server')
    # server = pywsgi.WSGIServer(('0.0.0.0', 8000), app, handler_class=WebSocketHandler)
    # server.serve_forever()


# os.environ['MKL_SERVICE_FORCE_INTEL'] = '1'
# os.environ['MULTIPROCESSING_METHOD'] = 'forkserver'                                                    
if __name__ == '__main__':
    mp.set_start_method('spawn')
    load_dotenv()  # Load environment variables from .env file, if it exists
    main()
    
    
    
