###############################################################################
#  WebRTC 连接管理 + RTC 音频/视频接收
###############################################################################

import json
import asyncio
import random
import copy
from typing import Dict, Optional
import queue

from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceServer, RTCConfiguration
from aiortc.rtcrtpsender import RTCRtpSender

from utils.logger import logger


# def _rand_session_id(n: int = 6) -> int:
#     """生成 N 位随机 session ID"""
#     return random.randint(10 ** (n - 1), 10 ** n - 1)


from server.session_manager import session_manager
from server.session_manager import MaxSessionError

class RTCManager:
    """
    WebRTC 连接管理器。
    
    管理 PeerConnection 生命周期、音视频轨道收发、DataChannel。
    """

    def __init__(self, opt):
        """
        Args:
            opt: 全局配置
        """
        self.opt = opt
        self.pcs: set = set()

    async def _create_pc_and_answer(self, avatar_session, sessionid, offer):
        """创建 PeerConnection、添加轨道、SDP 交换，返回已完成 answer 的 pc"""
        # 记录创建时间与 pc 引用：session_manager.drop_stale_connecting()
        # 需要它们来判断/清理「卡在 connecting 的僵尸会话」
        import time as _time
        if avatar_session is not None:
            try:
                if getattr(avatar_session, '_created_at', None) is None:
                    avatar_session._created_at = _time.time()
            except Exception:
                pass

        ice_server = RTCIceServer(urls=self.opt.stun)
        pc = RTCPeerConnection(
            configuration=RTCConfiguration(iceServers=[ice_server])
        )
        self.pcs.add(pc)
        if avatar_session is not None:
            try:
                avatar_session._rtc_pc = pc
            except Exception:
                pass

        # ── 会话回收 ────────────────────────────────────────────────
        # 原来只处理 failed/closed。但客户端直接关页面 / 崩溃 / 断网时，pc 往往
        # 停在 disconnected 就不再变化（aiortc 不会很快超时到 failed），会话于是
        # 永远留在 admin 的「活跃会话」里 —— 用户明明关了所有页面，后台还显示有
        # 会话在跑，而且会一直占着模型切换的名额。
        # 给一个宽限期：期间恢复成 connected 就不动，到期仍是断开态才回收。
        _disconnect_grace = 30.0
        _disconnect_task = {"t": None}

        def _cancel_grace_task():
            t = _disconnect_task["t"]
            # 不要取消「自己」：宽限任务到点后会 await pc.close()，那会再次触发本回调，
            # 若在这里把当前任务 cancel 掉，后面的清理语句就会被 CancelledError 跳过。
            if t is not None and not t.done() and t is not asyncio.current_task():
                t.cancel()
            _disconnect_task["t"] = None

        async def _reap_if_still_disconnected():
            try:
                await asyncio.sleep(_disconnect_grace)
            except asyncio.CancelledError:
                return
            if pc.connectionState in ("disconnected", "failed", "closed"):
                logger.warning("会话 %s 断开超过 %.0fs 仍未恢复，自动回收",
                               sessionid[:8], _disconnect_grace)
                # 先清理再关 pc：pc.close() 会再触发一次本回调，顺序反了会让清理被跳过
                self.pcs.discard(pc)
                session_manager.remove_session(sessionid)
                try:
                    await pc.close()
                except Exception:
                    pass

        @pc.on("connectionstatechange")
        async def on_connectionstatechange():
            state = pc.connectionState
            logger.info("Connection state is %s", state)
            if state in ("failed", "closed"):
                _cancel_grace_task()
                # 先把资源清掉，再关 pc（pc.close() 会再次触发本回调，顺序反了会被中断）
                self.pcs.discard(pc)
                session_manager.remove_session(sessionid)
                if state == "failed":
                    try:
                        await pc.close()
                    except Exception:
                        pass
            elif state == "disconnected":
                # 只在没有在跑的宽限任务时起一个，避免重复计时
                t = _disconnect_task["t"]
                if t is None or t.done():
                    _disconnect_task["t"] = asyncio.create_task(_reap_if_still_disconnected())
            elif state == "connected":
                _cancel_grace_task()

        # 添加发送轨道
        from server.webrtc import HumanPlayer
        player = HumanPlayer(avatar_session)
        pc.addTrack(player.audio)
        pc.addTrack(player.video)

        # 设置编解码器偏好
        capabilities = RTCRtpSender.getCapabilities("video")
        preferences = list(filter(lambda x: x.name == "H264", capabilities.codecs))
        preferences += list(filter(lambda x: x.name == "VP8", capabilities.codecs))
        preferences += list(filter(lambda x: x.name == "rtx", capabilities.codecs))
        transceiver = pc.getTransceivers()[1]
        transceiver.setCodecPreferences(preferences)

        await pc.setRemoteDescription(offer)
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)

        return pc

    async def handle_offer(self, request):
        """处理 WebRTC offer 信令"""
        params = await request.json()
        offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

        try:
            sessionid = await session_manager.create_session(params)
        except MaxSessionError as e:
            logger.warning("Rejecting offer: %s", e)
            return web.Response(
                content_type="application/json",
                text=json.dumps({"code": -1, "msg": str(e)}),
            )
        except Exception as e:
            # 模型自动加载/切换失败（素材不完整、显存不足、有活跃会话等）
            # —— 必须给出可读原因，否则前端只会看到 "Failed to fetch" 之类的噪声
            logger.warning("offer 构建会话失败：%s: %s", type(e).__name__, e)
            return web.Response(
                content_type="application/json",
                text=json.dumps({
                    "code": -2,
                    "msg": f"{e}",
                    "error_type": type(e).__name__,
                }, ensure_ascii=False),
            )
        logger.info('offer sessionid=%s', sessionid)

        # 建 PC 失败（例如客户端发了非法 SDP）时必须回收会话，
        # 否则这个会话会永久占住名额，后续连接全报"已达上限"。
        try:
            pc = await self._create_pc_and_answer(
                session_manager.get_session(sessionid), sessionid, offer
            )
        except Exception as e:
            logger.warning("offer 建立 PeerConnection 失败，回收会话 %s：%s: %s",
                           sessionid, type(e).__name__, e)
            try:
                session_manager.remove_session(sessionid)
            except Exception:
                logger.exception("回收会话 %s 时出错", sessionid)
            return web.Response(
                content_type="application/json",
                text=json.dumps({
                    "code": -3,
                    "msg": f"建立 WebRTC 连接失败：{type(e).__name__}: {e}",
                    "error_type": type(e).__name__,
                }, ensure_ascii=False),
            )

        return web.Response(
            content_type="application/json",
            text=json.dumps({
                "sdp": pc.localDescription.sdp,
                "type": pc.localDescription.type,
                "sessionid": sessionid,
            }),
        )

    async def handle_whep(self, request):
        """
        处理 WHEP 信令（WebRTC HTTP Egress Protocol）

        - 请求 body 为裸 SDP offer（Content-Type: application/sdp）
        - 扩展参数通过 query string 传入（avatar, tts, tts_server 等）
        - 返回 SDP answer（Content-Type: application/sdp）
        - sessionid 通过 X-Session-ID 响应头返回
        """
        params = dict(request.query)
        # 客户端可通过 query param 自定义 sessionid，不传则自动生成
        client_sid = params.pop("sessionid", None)

        offer_sdp = await request.text()
        offer = RTCSessionDescription(sdp=offer_sdp, type="offer")

        try:
            sessionid = await session_manager.create_session(params, sessionid=client_sid)
        except MaxSessionError as e:
            logger.warning("Rejecting whep: %s", e)
            return web.Response(
                status=503,
                content_type="text/plain",
                text=str(e),
            )
        logger.info("whep sessionid=%s", sessionid)

        pc = await self._create_pc_and_answer(
            session_manager.get_session(sessionid), sessionid, offer
        )

        return web.Response(
            status=201,
            content_type="application/sdp",
            text=pc.localDescription.sdp,
            headers={"X-Session-ID": sessionid},
        )

    async def handle_rtcpush(self, push_url, sessionid: str):
        """RTCPush 模式：主动推流"""
        import aiohttp
        await session_manager.create_session({}, sessionid)
        avatar_session = session_manager.get_session(sessionid)

        pc = RTCPeerConnection()
        self.pcs.add(pc)

        @pc.on("connectionstatechange")
        async def on_connectionstatechange():
            logger.info("Connection state is %s", pc.connectionState)
            if pc.connectionState == "failed":
                await pc.close()
                self.pcs.discard(pc)

        from server.webrtc import HumanPlayer
        player = HumanPlayer(avatar_session)
        pc.addTrack(player.audio)
        pc.addTrack(player.video)

        await pc.setLocalDescription(await pc.createOffer())

        async with aiohttp.ClientSession() as session:
            async with session.post(push_url, data=pc.localDescription.sdp) as response:
                answer_sdp = await response.text()

        await pc.setRemoteDescription(
            RTCSessionDescription(sdp=answer_sdp, type='answer')
        )

    async def shutdown(self):
        """关闭所有 PeerConnection"""
        coros = [pc.close() for pc in self.pcs]
        await asyncio.gather(*coros)
        self.pcs.clear()
