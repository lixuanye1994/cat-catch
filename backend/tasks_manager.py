"""通用嗅探任务管理（与下载通道分离）"""
import asyncio

from .browser import SniffTask


class TaskManager:
    def __init__(self):
        self.tasks: dict[str, SniffTask] = {}
        self._bg: set[asyncio.Task] = set()

    async def create_task(self, url: str) -> SniffTask:
        task = SniffTask(url)
        self.tasks[task.task_id] = task
        bg = asyncio.create_task(task.run())
        self._bg.add(bg)
        bg.add_done_callback(self._bg.discard)
        return task

    def get_task(self, task_id: str) -> SniffTask | None:
        return self.tasks.get(task_id)

    async def stop_task(self, task: SniffTask):
        await task.stop()


task_manager = TaskManager()
