###############################################################################
#  DouyinCollector — 抖音弹幕采集
#
#  依赖 DouyinBarrageGrab (https://github.com/ape-byte/DouyinBarrageGrab)
#  运行后监听 ws://localhost:8888，解码 protobuf 并以 JSON 广播。
#  本项目连接到该本地 WebSocket 获取弹幕事件。
###############################################################################

import asyncio
import json

import aiohttp
from server.livestream.services.danmaku.base import BaseDanmakuCollector, DanmakuMessage
from utils.logger import logger


class DouyinCollector(BaseDanmakuCollector):
    """抖音直播间弹幕采集器（连接本地 DouyinBarrageGrab）"""

    def __init__(self, room_id: str = "",
                 ws_url: str = "ws://127.0.0.1:8888"):
        super().__init__()
        self._room_id = room_id
        self._ws_url = ws_url
        self._running = False
        self._ws_task: asyncio.Task | None = None

    # ── 消息解析 ──────────────────────────────────────────────────

    def _parse_msg(self, raw: dict) -> DanmakuMessage | None:
        """将 DouyinBarrageGrab JSON 转为统一的 DanmakuMessage"""
        # 兼容 PascalCase（老版）和 camelCase（新版）
        msg_type = raw.get("Type") or raw.get("type", 0)
        data_raw = raw.get("Data") or raw.get("data", {})
        process = raw.get("ProcessName") or raw.get("processName", "")

        # Data 可能是 JSON 字符串（老版）或已是 dict（新版）
        if isinstance(data_raw, str):
            try:
                data = json.loads(data_raw)
            except json.JSONDecodeError:
                return None
        else:
            data = data_raw

        user = data.get("User") or {}
        nickname = user.get("Nickname") or user.get("nickname") or "未知"

        if msg_type == 1:  # 弹幕
            content = data.get("Content") or data.get("content") or ""
            return DanmakuMessage(
                platform="douyin", sender=nickname, content=content,
                msg_type="danmaku", raw_data=data,
            )
        elif msg_type == 3:  # 进入直播间
            return DanmakuMessage(
                platform="douyin", sender=nickname, content="进入了直播间",
                msg_type="enter", raw_data=data,
            )
        elif msg_type == 4:  # 关注
            return DanmakuMessage(
                platform="douyin", sender=nickname, content="关注了直播间",
                msg_type="follow", raw_data=data,
            )
        elif msg_type == 5:  # 礼物
            gift_name = data.get("GiftName") or data.get("giftName") or "礼物"
            count = data.get("GiftCount") or data.get("giftCount") or 1
            repeat = data.get("RepeatCount") or data.get("repeatCount") or 1
            return DanmakuMessage(
                platform="douyin", sender=nickname,
                content=f"{gift_name} x{count}x{repeat}",
                msg_type="gift", raw_data=data,
            )
        # elif msg_type == 2:  # 点赞
        #     count = data.get("Count") or data.get("count") or 0
        #     return DanmakuMessage(
        #         platform="douyin", sender=nickname,
        #         content=f"点赞 x{count}",
        #         msg_type="danmaku", raw_data=data,  # 点赞按弹幕处理
        #     )
        return None

    # ── 连接管理 ──────────────────────────────────────────────────

    async def connect(self, room_id: str):
        """连接本地 DouyinBarrageGrab WebSocket"""
        self._room_id = room_id
        self._running = True
        self._ws_task = asyncio.create_task(self._ws_loop())

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
        logger.info("Douyin collector disconnected")

    async def _ws_loop(self):
        """WebSocket 主循环"""
        retry_delay = 1
        fails = 0                      # 连续失败次数：用于收敛日志，避免每 30s 刷屏
        while self._running:
            session = None
            try:
                session = aiohttp.ClientSession()
                async with session.ws_connect(self._ws_url) as ws:
                    logger.info(f"Douyin collector connected to {self._ws_url}")
                    retry_delay = 1
                    fails = 0
                    async for msg in ws:
                        if not self._running:
                            break
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                raw = json.loads(msg.data)
                                dm = self._parse_msg(raw)
                                if dm:
                                    await self._dispatch(dm)
                            except json.JSONDecodeError:
                                pass
                            except Exception as e:
                                logger.warning(f"Douyin parse error: {e}")
                        elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                            break
            except asyncio.CancelledError:
                break
            except aiohttp.ClientError as e:
                fails += 1
                if fails == 1:
                    logger.warning(
                        "抖音弹幕中继未连接（%s）：%s\n"
                        "  → 抖音弹幕依赖第三方中继 DouyinBarrageGrab（默认监听 127.0.0.1:8888）。\n"
                        "  → 它只影响「抖音弹幕 / 礼物 / 关注」的采集，**不影响数字人画面、素材轮换与声音**。\n"
                        "  → 不需要弹幕时：直接用 index.html 连接即可（不要在运营后台点“开始直播”）；\n"
                        "     需要弹幕时：先启动 DouyinBarrageGrab，再在运营后台点开始直播。\n"
                        "  → 后续连接失败将静默重试（不再刷屏），每 20 次提示一次。",
                        self._ws_url, e)
                elif fails % 20 == 0:
                    logger.info("抖音弹幕中继仍未连接（已失败 %d 次），继续静默重试中", fails)
                else:
                    logger.debug("Douyin WS error: %s", e)
            except Exception as e:
                logger.error(f"Douyin unexpected error: {e}")
            finally:
                if session and not session.closed:
                    await session.close()

            if self._running:
                logger.debug("Douyin reconnecting in %ss...", retry_delay)
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 30)
        logger.info("Douyin WS loop stopped")
