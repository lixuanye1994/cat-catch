"""统一下载管理：内存任务 + journal 持久化 + 恢复/丢弃"""
import asyncio
import itertools
import shutil
from pathlib import Path

from . import journal
from .downloader import DownloadJob


class DownloadManager:
    def __init__(self):
        self.jobs: dict[str, DownloadJob] = {}
        self._bg: set[asyncio.Task] = set()
        # 从 journal 已有最大 id 之后编号，避免重启后复用旧任务的缓存目录
        max_id = 0
        for dl_id in journal.load():
            if dl_id.isdigit():
                max_id = max(max_id, int(dl_id))
        if journal.TMP_DIR.exists():
            for child in journal.TMP_DIR.iterdir():
                if child.name.isdigit():
                    max_id = max(max_id, int(child.name))
        self._seq = itertools.count(max_id + 1)
        self.subscribers: set[asyncio.Queue] = set()

    # ---------- 事件推送 ----------
    def subscribe(self, queue: asyncio.Queue):
        self.subscribers.add(queue)

    def unsubscribe(self, queue: asyncio.Queue):
        self.subscribers.discard(queue)

    def broadcast(self, message: dict):
        for queue in list(self.subscribers):
            queue.put_nowait(message)

    # ---------- 创建 ----------
    async def start(self, *, kind: str, params: dict, headers: dict,
                    title: str, settings: dict,
                    options: dict | None = None) -> dict:
        dl_id = str(next(self._seq))
        job = DownloadJob(
            dl_id, kind=kind, params=params, headers=headers,
            title=title, settings=settings,
            emit=lambda: self._emit(dl_id), options=options)
        self.jobs[dl_id] = job
        self._persist(job)

        bg = asyncio.create_task(job.run())
        self._bg.add(bg)
        bg.add_done_callback(lambda t=bg: (self._bg.discard(t),
                                           self._on_finish(dl_id)))
        self._emit(dl_id)
        return job.state

    def _on_finish(self, dl_id: str):
        job = self.jobs.get(dl_id)
        if job:
            self._persist(job)
            self._emit(dl_id)

    # ---------- 控制 ----------
    def cancel(self, dl_id: str):
        job = self.jobs.get(dl_id)
        if job:
            job.cancel()

    async def resume(self, dl_id: str, settings: dict) -> dict:
        if dl_id in self.jobs and self.jobs[dl_id].state["status"] == "running":
            return self.jobs[dl_id].state

        rec = journal.get(dl_id)
        if not rec:
            raise LookupError("下载记录不存在")
        if rec["status"] == "done":
            return rec

        options = dict(rec.get("options") or {})
        options["_initial_elapsed"] = rec.get("elapsed", 0)
        job = DownloadJob(
            dl_id, kind=rec["kind"], params=rec["params"],
            headers=rec.get("headers") or {}, title=rec["title"],
            settings=settings, emit=lambda: self._emit(dl_id),
            options=options)
        self.jobs[dl_id] = job
        self._persist(job)

        bg = asyncio.create_task(job.run())
        self._bg.add(bg)
        bg.add_done_callback(lambda t=bg: (self._bg.discard(t),
                                           self._on_finish(dl_id)))
        self._emit(dl_id)
        return job.state

    async def discard(self, dl_id: str):
        job = self.jobs.get(dl_id)
        if job:
            job.cancel()
            await asyncio.sleep(0.5)
        # 删除临时文件
        tmp = journal.TMP_DIR / dl_id
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        # 删除 .part 半成品
        for rec_src in [journal.get(dl_id)]:
            pass
        journal.remove(dl_id)
        self.broadcast({"type": "discard", "dl_id": dl_id})

    # ---------- 查询 ----------
    def states(self) -> list[dict]:
        records = journal.load()
        # 内存实时值覆盖（保留 journal 中的 updated 等字段）
        for dl_id, job in self.jobs.items():
            records[dl_id] = {**records.get(dl_id, {}), **self._record(job)}
        return list(records.values())

    def unfinished(self) -> list[dict]:
        records = journal.load()
        for dl_id, job in self.jobs.items():
            records[dl_id] = {**records.get(dl_id, {}), **self._record(job)}
        return [r for r in records.values() if r["status"] in
                ("interrupted", "canceled", "error")]

    # ---------- 持久化 ----------
    def _record(self, job: DownloadJob) -> dict:
        return {
            "dl_id": job.dl_id,
            "kind": job.kind,
            "title": job.title,
            "params": job.params,
            "headers": job.headers,
            "options": job.options,
            **job.state,
        }

    def _persist(self, job: DownloadJob):
        journal.upsert(self._record(job))

    def _emit(self, dl_id: str):
        job = self.jobs.get(dl_id)
        if not job:
            return
        self._persist(job)
        self.broadcast({"type": "download", "title": job.title, **job.state})


manager = DownloadManager()
