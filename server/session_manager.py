###############################################################################
#  全局会话管理器 (Session Manager)
###############################################################################

import asyncio
import uuid
from typing import Dict, Optional
from utils.logger import logger
from avatars.base_avatar import BaseAvatar


class MaxSessionError(Exception):
    """会话数达到上限时抛出"""
    pass

def _rand_session_id() -> str:
    """生成 UUID session ID"""
    return str(uuid.uuid4())

class SessionManager:
    """
    全局数字人会话管理器。
    
    统一管理 avatar_sessions 生命周期，并在脱离 WebRTC 时依然保持服务可用。
    """
    _instance = None
    
    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if not hasattr(self, "initialized"):
            self.sessions: Dict[str, BaseAvatar] = {}
            self.build_session_fn = None
            self.max_session = 1   # default, override via set_max_session()
            self.initialized = True

    def set_max_session(self, n: int):
        """设置最大并发会话数"""
        self.max_session = max(1, n)

    def init_builder(self, build_session_fn):
        """配置用于构建 avatar_session 的工厂函数"""
        self.build_session_fn = build_session_fn
        
    def get_session(self, sessionid: str) -> Optional[BaseAvatar]:
        """获取已存活的会话"""
        return self.sessions.get(sessionid)

    def has_session(self, sessionid: str) -> bool:
        """检查会话是否存在"""
        return sessionid in self.sessions and self.sessions[sessionid] is not None
        
    async def create_session(self, params: dict, sessionid: str = None) -> str:
        """
        在异步环境中创建一个新会话
        如果 sessionid 为 None，则自动生成。
        """
        if self.build_session_fn is None:
            raise Exception("SessionManager builder not initialized")
            
        if sessionid is None:
            sessionid = _rand_session_id()
            
        # 检查是否达到最大会话数
        active_count = sum(1 for s in self.sessions.values() if s is not None)
        if active_count >= self.max_session:
            raise MaxSessionError(
                f"Maximum session limit reached ({active_count}/{self.max_session})"
            )

        logger.info('Creating sessionid=%s, current session num=%d', sessionid, active_count)
        # 预先占位防止重复
        self.sessions[sessionid] = None

        # 在线程池中构建 session（加载模型非常耗时）
        # 注意：构建失败（例如素材不完整、模型切换被拒）时必须回收占位，
        # 否则这个 None 会永久占住一个会话名额，后续连接全部报"已达上限"。
        try:
            avatar_session = await asyncio.get_event_loop().run_in_executor(
                None, self.build_session_fn, sessionid, params
            )
        except Exception:
            self.sessions.pop(sessionid, None)
            logger.warning(f"Session {sessionid} 构建失败，已回收占位名额")
            raise
        self.sessions[sessionid] = avatar_session
        return sessionid
        
    def add_session(self, sessionid: str, avatar_session: BaseAvatar):
        """同步添加静态或外部管理的会话（供非服务端入口调用）"""
        self.sessions[sessionid] = avatar_session

    def drop_stale_connecting(self, max_age_sec: float = 45.0) -> list:
        """回收长时间停留在 connecting 状态的「僵尸会话」。

        背景：`server/rtc_manager.py` 只在 connectionState 变成 failed/closed 时
        才清理会话。但若客户端在 ICE 完成前就消失（关掉页面、崩溃、网络断开），
        连接会永远停在 `connecting`，会话也就永远留在表里。

        这不是新问题，但会带来一个实际后果：模型自动切换时会认为
        「还有会话在用旧模型」而一直拒绝切换，用户就必须重启服务。

        做法：把「从未 ready 且创建超过 max_age_sec」的会话清掉。
        只处理 connecting/new 状态的会话 —— 已经 connected 的（哪怕暂时
        无数据）不动，避免误杀正在推流的连接。

        返回被回收的 sessionid 列表。
        """
        import time as _time
        dropped = []
        # 先快照，避免迭代中修改 dict
        for sid, sess in list(self.sessions.items()):
            if sess is None:
                continue                      # 占位由 create_session 自己回收
            pc = getattr(sess, '_rtc_pc', None)
            if pc is None:
                continue                      # 非 webrtc 会话（virtualcam 等）不管
            state = getattr(pc, 'connectionState', None)
            if state not in ('new', 'connecting'):
                continue
            created = getattr(sess, '_created_at', None)
            if created is None:
                continue
            if _time.time() - created < max_age_sec:
                continue
            logger.warning("回收僵尸会话 %s（停留在 %s 超过 %.0fs）",
                           sid[:8], state, max_age_sec)
            try:
                self.remove_session(sid)
            except Exception:
                logger.exception("回收僵尸会话 %s 出错", sid)
            dropped.append(sid)
        return dropped

    def remove_session(self, sessionid: str):
        """销毁会话资源：置位 quit_event，级联停掉 render/推理/合帧/TTS 线程"""
        if sessionid in self.sessions:
            logger.info(f"Removing session {sessionid}")
            avatar_session = self.sessions.pop(sessionid, None)
            try:
                if avatar_session is not None and getattr(avatar_session, 'quit_event', None) is not None:
                    avatar_session.flush_talk()
                    avatar_session.quit_event.set()
            except Exception:
                logger.exception(f"session {sessionid} cleanup error")

# 单例抛出
session_manager = SessionManager()
