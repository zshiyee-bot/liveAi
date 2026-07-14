###############################################################################
#  LiveTalkingClient — LiveTalking HTTP + WebSocket 客户端
#
#  Session 选择流程:
#    1. 前端调用 GET /api/livetalking/sessions → 后端转发到 LiveTalking
#    2. 用户在 Dashboard 从下拉列表选择 sessionid
#    3. 启动直播时将 sessionid 传入
#
#  LiveTalking API:
#    POST /human       — 发送文字（type: "echo" 直接 TTS）
#    POST /humanaudio  — 上传音频文件
#    POST /is_speaking — 查询是否正在说话
#    POST /interrupt_talk — 打断
#    GET  /ws          — WebSocket（接收 status: start/end 事件）
###############################################################################

import asyncio
import json
import os
from typing import Callable, Optional

import httpx
import aiohttp
from app.utils.logger import logger


class LiveTalkingClient:
    """管理与 LiveTalking 服务器的连接"""

    def __init__(self, base_url: str = "http://127.0.0.1:8010"):
        self.base_url = base_url.rstrip("/")
        self.session_id: Optional[str] = None
        self._http: Optional[httpx.AsyncClient] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._ws_session: Optional[aiohttp.ClientSession] = None
        self._ws_task: Optional[asyncio.Task] = None
        self._playback_callbacks: list[Callable] = []
        self._running = False

    # ── Session 管理 ──────────────────────────────────────────────

    async def fetch_sessions(self) -> list[dict]:
        """获取 LiveTalking 活跃 session 列表"""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{self.base_url}/api/admin/sessions")
                data = resp.json()
                if data.get("code") == 0:
                    sessions = data.get("data", {}).get("sessions", [])
                    logger.info(f"Fetched {len(sessions)} LiveTalking sessions")
                    return sessions
                else:
                    logger.warning(f"LiveTalking sessions API returned error: {data}")
                    return []
        except Exception as e:
            logger.error(f"Failed to fetch LiveTalking sessions: {e}")
            return []

    def set_session(self, session_id: str):
        """设置要使用的 session"""
        self.session_id = session_id
        logger.info(f"LiveTalking session set: {session_id}")

    # ── 连接 ──────────────────────────────────────────────────────

    async def connect(self):
        """建立 HTTP 客户端和 WebSocket 连接"""
        if not self.session_id:
            raise ValueError("session_id not set, call set_session() first")

        self._http = httpx.AsyncClient(timeout=30.0)
        self._running = True

        # 启动 WebSocket 监听
        self._ws_task = asyncio.create_task(self._ws_connect())
        logger.info(f"LiveTalkingClient connected (session={self.session_id})")

    async def disconnect(self):
        """断开连接"""
        self._running = False

        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
            self._ws_task = None

        if self._ws_session:
            await self._ws_session.close()
            self._ws_session = None

        if self._http:
            await self._http.aclose()
            self._http = None

        logger.info("LiveTalkingClient disconnected")

    # ── WebSocket 监听（播放状态）─────────────────────────────────

    async def _ws_connect(self):
        """WebSocket 客户端：监听播放 start/end 事件"""
        ws_url = f"{self.base_url}/ws"
        # 将 http:// 替换为 ws://
        ws_url = ws_url.replace("http://", "ws://").replace("https://", "wss://")

        retry_delay = 1
        max_retry_delay = 30

        while self._running:
            try:
                self._ws_session = aiohttp.ClientSession()
                async with self._ws_session.ws_connect(ws_url) as ws:
                    self._ws = ws
                    retry_delay = 1  # 重置重试延迟

                    # 登录
                    await ws.send_json({"cmd": "login", "sessionid": self.session_id})
                    login_msg = await ws.receive_json()
                    logger.info(f"LiveTalking WS login: {login_msg}")

                    # 监听事件
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                data = json.loads(msg.data)
                                status = data.get("status")
                                if status == "end":
                                    logger.info("LiveTalking playback ended")
                                    for cb in self._playback_callbacks:
                                        try:
                                            if asyncio.iscoroutinefunction(cb):
                                                await cb()
                                            else:
                                                cb()
                                        except Exception as exc:
                                            logger.warning(f"Playback callback error: {exc}")
                                elif status == "start":
                                    logger.info(f"LiveTalking playback started: {data.get('text', '')[:50]}")
                            except json.JSONDecodeError:
                                pass
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            logger.error(f"LiveTalking WS error: {ws.exception()}")
                            break
                        elif msg.type == aiohttp.WSMsgType.CLOSED:
                            logger.info("LiveTalking WS closed")
                            break

            except asyncio.CancelledError:
                break
            except aiohttp.ClientError as e:
                logger.warning(f"LiveTalking WS connection failed: {e}")
            except Exception as e:
                logger.error(f"LiveTalking WS unexpected error: {e}")

            if self._running:
                logger.info(f"LiveTalking WS reconnecting in {retry_delay}s...")
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, max_retry_delay)

        logger.info("LiveTalking WS listener stopped")

    # ── 发送 ──────────────────────────────────────────────────────

    async def send_text(self, text: str):
        """通过 /human 接口发送文字（echo 模式直接 TTS）"""
        if not self._http:
            raise RuntimeError("Not connected")

        resp = await self._http.post(
            f"{self.base_url}/human",
            json={
                "sessionid": self.session_id,
                "text": text,
                "type": "echo",
            },
        )
        data = resp.json()
        if data.get("code") != 0:
            logger.warning(f"LiveTalking /human returned error: {data}")
        return data

    async def send_audio(self, audio_path: str):
        """通过 /humanaudio 接口上传音频文件"""
        if not self._http:
            raise RuntimeError("Not connected")

        if not os.path.exists(audio_path):
            logger.error(f"Audio file not found: {audio_path}")
            return {"code": -1, "msg": "File not found"}

        with open(audio_path, "rb") as f:
            resp = await self._http.post(
                f"{self.base_url}/humanaudio",
                data={"sessionid": self.session_id},
                files={"file": (os.path.basename(audio_path), f)},
            )
        data = resp.json()
        return data

    async def interrupt(self):
        """打断当前说话"""
        if not self._http:
            return
        resp = await self._http.post(
            f"{self.base_url}/interrupt_talk",
            json={"sessionid": self.session_id},
        )
        return resp.json()

    async def is_speaking(self) -> bool:
        """查询数字人是否正在说话（轮询降级方案）"""
        if not self._http:
            return False
        try:
            resp = await self._http.post(
                f"{self.base_url}/is_speaking",
                json={"sessionid": self.session_id},
            )
            data = resp.json()
            return data.get("data", False)
        except Exception:
            return False

    # ── 回调 ──────────────────────────────────────────────────────

    def on_playback_ended(self, callback: Callable):
        """注册播放结束回调"""
        self._playback_callbacks.append(callback)
