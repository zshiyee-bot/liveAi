###############################################################################
#  MultiPlatformCollector — 多平台弹幕聚合器
###############################################################################

import json
from app.services.danmaku.base import BaseDanmakuCollector, DanmakuMessage
from app.utils.logger import logger


class MultiPlatformCollector:
    """管理多个平台的弹幕采集器，统一消息流"""

    def __init__(self):
        self._collectors: dict[str, BaseDanmakuCollector] = {}
        self._callbacks: list = []

    def add_collector(self, platform: str, collector: BaseDanmakuCollector):
        """注册一个平台采集器"""
        self._collectors[platform] = collector
        # 将 MultiPlatform 的回调转发给该 collector
        collector.on_message(self._forward_message)
        logger.info(f"Registered collector for platform: {platform}")

    def remove_collector(self, platform: str):
        """移除一个平台采集器"""
        if platform in self._collectors:
            self._collectors.pop(platform)
            logger.info(f"Removed collector for platform: {platform}")

    def on_message(self, callback):
        """注册统一的回调"""
        self._callbacks.append(callback)

    async def _forward_message(self, msg: DanmakuMessage):
        """转发消息给所有统一回调"""
        for cb in self._callbacks:
            try:
                await cb(msg)
            except Exception:
                pass

    async def connect(self, room_config: str | dict):
        """
        连接到各平台直播间。

        room_config 格式:
          单平台: "12345" (Bilibili room ID)
          多平台: {"bilibili": "12345", "douyin": "67890"}
        """
        if isinstance(room_config, str):
            # 默认 B 站
            bilibili_id = room_config
            from app.services.danmaku.bilibili import BilibiliCollector
            collector = BilibiliCollector(bilibili_id)
            self.add_collector("bilibili", collector)
        else:
            for platform, room_id in room_config.items():
                if platform == "bilibili" and room_id:
                    from app.services.danmaku.bilibili import BilibiliCollector
                    collector = BilibiliCollector(room_id)
                    self.add_collector("bilibili", collector)
                elif platform == "douyin":
                    logger.warning("Douyin collector not yet implemented")
                elif platform == "kuaishou":
                    logger.warning("Kuaishou collector not yet implemented")

        # 并行连接所有平台
        import asyncio
        tasks = [c.connect("") for c in self._collectors.values()]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for (platform, _), result in zip(self._collectors.items(), results):
            if isinstance(result, Exception):
                logger.error(f"Failed to connect {platform}: {result}")

        logger.info(f"Multi-platform collector connected: {list(self._collectors.keys())}")

    async def disconnect(self):
        """断开所有平台"""
        import asyncio
        tasks = [c.disconnect() for c in self._collectors.values()]
        await asyncio.gather(*tasks, return_exceptions=True)
        self._collectors.clear()
        logger.info("Multi-platform collector disconnected")
