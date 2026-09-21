import uuid
import time
import threading
import json
import os
import requests
import cv2
from concurrent.futures import ThreadPoolExecutor
from utils.logger import logger

class AvatarTask:
    def __init__(self, task_id, model_type, avatar_id, params, notify_url=None):
        self.task_id = task_id
        self.model_type = model_type
        self.avatar_id = avatar_id
        self.params = params
        self.status = "pending"  # pending, running, completed, failed
        self.progress = 0
        self.error_msg = ""
        self.notify_url = notify_url
        self.start_time = time.time()
        self.end_time = None

    def to_dict(self):
        return {
            "task_id": self.task_id,
            "model_type": self.model_type,
            "avatar_id": self.avatar_id,
            "status": self.status,
            "progress": self.progress,
            "error_msg": self.error_msg,
            "notify_url": self.notify_url,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration": (self.end_time or time.time()) - self.start_time
        }

class TaskManager:
    def __init__(self, max_workers=1):
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        self.tasks = {}
        self.lock = threading.Lock()

    def add_task(self, model_type, avatar_id, params, task_id=None, notify_url=None):
        if task_id is None:
            task_id = str(uuid.uuid4())

        task = AvatarTask(task_id, model_type, avatar_id, params, notify_url)
        with self.lock:
            self.tasks[task_id] = task

        self._notify(task)
        self.executor.submit(self._run_task, task_id)
        return task_id

    def get_task(self, task_id):
        with self.lock:
            return self.tasks.get(task_id)

    def delete_task(self, task_id):
        with self.lock:
            task = self.tasks.get(task_id)
            if not task:
                return False, "Task not found"

            if task.status != "pending":
                return False, f"Task is in {task.status} state, cannot delete"

            del self.tasks[task_id]
            return True, "Task deleted"

    def list_tasks(self):
        with self.lock:
            return sorted([task.to_dict() for task in self.tasks.values()], key=lambda x: x['start_time'], reverse=True)

    def _run_task(self, task_id):
        task = self.get_task(task_id)
        if not task:
            return

        task.status = "running"
        self._notify(task)
        logger.info(f"Starting task {task_id} for model {task.model_type}, avatar {task.avatar_id}")

        try:
            def progress_callback(p):
                task.progress = p

            if task.model_type == "musetalk":
                from avatars.musetalk.genavatar import generate_avatar
                generate_avatar(
                    video_path=task.params['video_path'],
                    avatar_id=task.avatar_id,
                    save_path=task.params.get('save_path', './data/avatars'),
                    bbox_shift=task.params.get('bbox_shift', 0),
                    extra_margin=task.params.get('extra_margin', 10),
                    parsing_mode=task.params.get('parsing_mode', 'jaw'),
                    version=task.params.get('version', 'v15'),
                    progress_callback=progress_callback
                )
            elif task.model_type == "wav2lip":
                from avatars.wav2lip.genavatar import generate_avatar
                generate_avatar(
                    video_path=task.params['video_path'],
                    avatar_id=task.avatar_id,
                    save_path=task.params.get('save_path', './data/avatars'),
                    img_size=task.params.get('img_size', 96),
                    pads=task.params.get('pads', [0, 10, 0, 0]),
                    nosmooth=task.params.get('nosmooth', False),
                    face_det_batch_size=task.params.get('face_det_batch_size', 16),
                    progress_callback=progress_callback
                )
            else:
                raise ValueError(f"Unsupported model type: {task.model_type}")

            task.status = "completed"
            task.progress = 100
            self._notify(task)
            logger.info(f"Task {task_id} completed successfully")
        except BaseException as e:
            # ⚠️ 这里必须是 BaseException，不能是 Exception。
            # 实测（2026-09-21，Ubuntu + RTX 5060 Ti）：某次 musetalk 训练的任务
            #   只跑了 0.015s、finally 已经写了 end_time，但 status 仍是 "running"，
            #   日志里**既没有 completed 也没有 failed** ——
            #   因为抛出的是 Exception 之外的东西（sys.exit() 之类的 BaseException），
            #   `except Exception` 抓不到，异常逃逸到 ThreadPoolExecutor 被静默吞掉。
            #   用户看到的表现就是「点了训练没反应、进度永远 0、日志一片空白」。
            # 捕获 BaseException 之后，任何失败都会落到日志和任务状态里。
            task.status = "failed"
            # 保留完整 traceback：只记 str(e) 会丢掉出错位置，
            # 排查时只能看到 'NoneType' and 'int' 这种无上下文的信息。
            import traceback as _tb
            task.error_msg = f"{type(e).__name__}: {e}"
            self._notify(task)
            logger.error(f"Task {task_id} failed: {type(e).__name__}: {e}\n"
                         f"{_tb.format_exc()}")
        finally:
            task.end_time = time.time()
            # 兜底：无论上面怎么结束，绝不能让任务停在 "running"。
            # 否则前端永远显示「进度 0 / 进行中」，用户看到的就是「训练没反应」，
            # 而且没有任何线索可查。这里强制落一个终止态 + 日志。
            if task.status == "running":
                task.status = "failed"
                if not task.error_msg:
                    task.error_msg = "任务异常结束（未捕获的 BaseException，详见日志）"
                self._notify(task)
                logger.error(
                    f"Task {task_id} 异常结束：status 仍是 running，已强制标记 failed"
                    f"（耗时 {task.end_time - task.start_time:.3f}s）"
                    f" —— 正常路径不会走到这里，说明异常没被 except 捕获")

    def _notify(self, task):
        if not task.notify_url:
            return

        try:
            payload = task.to_dict()
            logger.info(f"Sending notification for task {task.task_id} to {task.notify_url}")
            requests.post(task.notify_url, json=payload, timeout=5)
        except Exception as e:
            logger.error(f"Failed to send notification for task {task.task_id}: {e}")

task_manager = TaskManager()
