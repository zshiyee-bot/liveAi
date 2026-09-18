from threading import Thread
import queue
from queue import Queue
from io import BytesIO
from enum import Enum

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from avatars.base_avatar import BaseAvatar

from utils.logger import logger

class State(Enum):
    RUNNING = 0
    PAUSE = 1

class BaseTTS:
    def __init__(self, opt, parent: "BaseAvatar"):
        self.opt = opt
        self.parent = parent

        #self.fps = opt.fps # 20 ms per frame
        self.sample_rate = 16000
        self.chunk = self.sample_rate // (opt.fps*2) # 320 samples per chunk (20ms * 16000 / 1000)
        self.input_stream = BytesIO()

        self.msgqueue = Queue()
        self.state = State.RUNNING

    def flush_talk(self):
        self.msgqueue.queue.clear()
        self.state = State.PAUSE

    def put_msg_txt(self, msg: str, datainfo: dict = {}, priority: bool = False): 
        if len(msg) > 0:
            if priority:
                # 插队：排到「还没开始合成」的最前面。
                # 正在合成 / 正在播的那条都不受影响 —— 所以插队不会造成接缝。
                self.msgqueue.queue.appendleft((msg, datainfo))
            else:
                self.msgqueue.put((msg, datainfo))

    def drop_msg(self, utt: str) -> int:
        """撤回「已入队但还没开始合成」的某条话术（按 datainfo['utt'] 匹配），返回撤回条数。
        正在合成的那条早已从队列里取走，不会被这里动到。"""
        if not utt:
            return 0
        keep, dropped = [], 0
        while True:
            try:
                item = self.msgqueue.queue.popleft()
            except IndexError:
                break
            try:
                tag = (item[1] or {}).get('utt')
            except Exception:
                tag = None
            if tag == utt:
                dropped += 1
            else:
                keep.append(item)
        for it in keep:
            self.msgqueue.queue.append(it)
        return dropped

    def render(self, quit_event):
        process_thread = Thread(target=self.process_tts, args=(quit_event,))
        process_thread.start()
    
    def process_tts(self, quit_event):        
        while not quit_event.is_set():
            try:
                msg: tuple[str, dict] = self.msgqueue.get(block=True, timeout=1)
                self.state = State.RUNNING
            except queue.Empty:
                continue
            self.txt_to_audio(msg)
        self.stop_tts()
        logger.info('ttsreal thread stop')
    
    def txt_to_audio(self, msg: tuple[str, dict]):
        pass

    def stop_tts(self):
        pass
