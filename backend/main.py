"""FastAPI 入口：API + WebSocket + 托管前端"""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, hls, journal
from .adapters import bilibili
from .manager import manager
from .tasks_manager import task_manager

_NO_WINDOW = getattr(__import__("subprocess"), "CREATE_NO_WINDOW", 0)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    # 进程退出时关闭所有浏览器任务
    for task in list(task_manager.tasks.values()):
        await task.stop()


app = FastAPI(title="cat-sniffer", lifespan=lifespan)
settings = config.load_settings()


@app.middleware("http")
async def no_cache(request: Request, call_next):
    # 前端源码会频繁改动：禁用浏览器缓存，始终校验拿最新文件
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-cache"
    return response


class SniffIn(BaseModel):
    url: str


class DownloadIn(BaseModel):
    item_id: int


class CancelIn(BaseModel):
    dl_id: str


class BiliResolveIn(BaseModel):
    url: str


class BiliPlayurlIn(BaseModel):
    bvid: str
    cid: int


class BiliDownloadIn(BaseModel):
    title: str
    video_url: str
    audio_url: str = ""


def refresh_settings():
    global settings
    settings = config.load_settings()


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/downloads")
def list_downloads():
    return {"items": manager.states()}


@app.post("/api/download/{dl_id}/resume")
async def resume_download(dl_id: str):
    refresh_settings()
    try:
        return await manager.resume(dl_id, settings)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/api/download/{dl_id}/discard")
async def discard_download(dl_id: str):
    await manager.discard(dl_id)
    return {"ok": True}


@app.post("/api/download/{dl_id}/cancel")
def cancel_any(dl_id: str):
    manager.cancel(dl_id)
    return {"ok": True}


@app.get("/api/settings")
def get_settings():
    return settings


@app.post("/api/settings")
def post_settings(payload: dict):
    try:
        global settings
        settings = config.save_settings(payload)
    except (ValueError, OSError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return settings


@app.post("/api/sniff")
async def create_sniff(body: SniffIn):
    url = body.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="网址不能为空")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    # 同一浏览器配置目录不支持两个 context 同时打开
    if any(t.status in ("starting", "running")
           for t in task_manager.tasks.values()):
        raise HTTPException(status_code=409, detail="已有嗅探任务在运行，请先停止")
    refresh_settings()
    task = await task_manager.create_task(url)
    return {"task_id": task.task_id}


@app.get("/api/tasks")
def list_tasks():
    """列出全部嗅探任务摘要，供页面刷新后找回仍在运行的任务"""
    return {"tasks": [
        {
            "task_id": t.task_id,
            "url": t.url,
            "status": t.status,
            "page_url": t.page_url,
            "page_title": t.page_title,
            "error": t.error,
        }
        for t in task_manager.tasks.values()
    ]}


@app.post("/api/task/{task_id}/stop")
async def stop_sniff(task_id: str):
    task = task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    await task_manager.stop_task(task)
    return {"ok": True}


@app.post("/api/task/{task_id}/download")
async def start_download(task_id: str, body: DownloadIn):
    task = task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    refresh_settings()

    item = next((i for i in task.items if i["id"] == body.item_id), None)
    if not item:
        raise HTTPException(status_code=404, detail="资源不存在")

    kind = item["kind"] if item["kind"] in ("hls", "dash") else "http"
    headers = await task.build_download_headers(item)
    title = task.base_title(item)
    return await manager.start(
        kind=kind, params={"url": item["url"]},
        headers=headers, title=title, settings=settings)


@app.post("/api/task/{task_id}/download/cancel")
def cancel_download(task_id: str, body: CancelIn):
    manager.cancel(body.dl_id)
    return {"ok": True}


# ---------- M3U8 专用下载页 ----------
class M3u8ProbeIn(BaseModel):
    url: str
    task_id: str = ""
    item_id: int = 0
    dl_id: str = ""       # 恢复场景：复用该下载记录的请求头


class M3u8DownloadIn(BaseModel):
    url: str
    title: str = ""
    output_dir: str = ""
    concurrency: int = 4
    retries: int = 3
    task_id: str = ""
    item_id: int = 0


@app.post("/api/m3u8/probe")
async def m3u8_probe(body: M3u8ProbeIn):
    url = body.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="M3U8 地址不能为空")
    refresh_settings()

    headers, title_default = {}, ""
    if body.task_id:
        task = task_manager.get_task(body.task_id)
        item = next((i for i in task.items if i["id"] == body.item_id), None)
        if not item:
            raise HTTPException(status_code=404, detail="嗅探资源不存在")
        headers = await task.build_download_headers(item)
        title_default = task.base_title(item)
    elif body.dl_id:
        # 页面刷新后恢复：沿用当初下载时的请求头（Referer/Cookie/UA）
        rec = journal.get(body.dl_id)
        if not rec:
            raise HTTPException(status_code=404, detail="下载记录不存在")
        headers = rec.get("headers") or {}

    try:
        result = await hls.probe(url, headers)
    except hls.HlsError as e:
        raise HTTPException(status_code=400, detail=str(e))
    result["title_default"] = title_default
    return result


@app.post("/api/m3u8/download")
async def m3u8_download(body: M3u8DownloadIn):
    url = body.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="请先选择画质")
    refresh_settings()

    headers = {}
    if body.task_id:
        task = task_manager.get_task(body.task_id)
        item = next((i for i in task.items if i["id"] == body.item_id), None)
        if not item:
            raise HTTPException(status_code=404, detail="嗅探资源不存在")
        headers = await task.build_download_headers(item)

    title = (body.title or "").strip() or "video"
    output_dir = (body.output_dir or "").strip() or settings["output_dir"]
    options = {
        "concurrency": body.concurrency,
        "retries": body.retries,
        "output_dir": output_dir,
    }
    return await manager.start(
        kind="hls", params={"url": url}, headers=headers,
        title=title, settings=settings, options=options)


# ---------- B 站适配器 ----------
@app.post("/api/bilibili/resolve")
async def bili_resolve(body: BiliResolveIn):
    refresh_settings()
    try:
        return await bilibili.resolve_url(body.url.strip(),
                                          settings.get("bilibili_sessdata", ""))
    except (bilibili.BilibiliError, httpx.HTTPError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/bilibili/playurl")
async def bili_playurl(body: BiliPlayurlIn):
    refresh_settings()
    try:
        return {"qualities": await bilibili.get_playurl(
            body.bvid, body.cid, settings.get("bilibili_sessdata", ""))}
    except (bilibili.BilibiliError, httpx.HTTPError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/bilibili/download")
async def bili_download(body: BiliDownloadIn):
    refresh_settings()
    headers = bilibili.download_headers(settings.get("bilibili_sessdata", ""))
    # B 站视频统一保存到 下载根目录/B站
    bili_dir = str(Path(settings["output_dir"]) / "B站")
    return await manager.start(
        kind="dash-merge",
        params={"video_url": body.video_url, "audio_url": body.audio_url},
        headers=headers, title=body.title, settings=settings,
        options={"output_dir": bili_dir})


@app.post("/api/bilibili/download/cancel")
def bili_cancel(body: CancelIn):
    manager.cancel(body.dl_id)
    return {"ok": True}


@app.get("/api/bilibili/preview")
async def bili_preview(request: Request, video_url: str, audio_url: str = "",
                       transcode: int = 0):
    """实时合成 DASH 双流为流式 MP4，供页面预览（带 UA/Referer）"""
    if not video_url:
        raise HTTPException(status_code=400, detail="缺少视频流地址")
    refresh_settings()
    ffmpeg = settings.get("ffmpeg_path") or "ffmpeg"
    ua = bilibili.UA
    headers = f"Referer: {bilibili.REFERER}\r\n"

    args = [ffmpeg, "-user_agent", ua, "-headers", headers,
            "-i", video_url]
    if audio_url:
        args += ["-user_agent", ua, "-headers", headers, "-i", audio_url]
    if transcode:
        # HEVC/AV1 等浏览器不一定支持的编码：转 H.264，宽度上限 1280
        args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                 "-vf", "scale='min(1280,iw)':-2",
                 "-c:a", "copy"]
    else:
        args += ["-c", "copy"]
    args += ["-movflags", "frag_keyframe+empty_moov+default_base_moof",
             "-f", "mp4", "pipe:1"]

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        creationflags=_NO_WINDOW,
    )

    async def gen():
        try:
            while True:
                if await request.is_disconnected():
                    break
                chunk = await proc.stdout.read(1 << 16)
                if not chunk:
                    break
                yield chunk
        finally:
            if proc.returncode is None:
                try:
                    proc.terminate()
                except Exception:
                    pass

    return StreamingResponse(
        gen(), media_type="video/mp4",
        headers={"Cache-Control": "no-store"})


@app.websocket("/ws/downloads")
async def ws_downloads(ws: WebSocket):
    await ws.accept()
    queue: asyncio.Queue = asyncio.Queue()
    manager.subscribe(queue)
    await ws.send_json({"type": "downloads", "items": manager.states()})

    async def pump():
        while True:
            await ws.send_json(await queue.get())

    pump_task = asyncio.create_task(pump())
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        pump_task.cancel()
        manager.unsubscribe(queue)


@app.websocket("/ws/task/{task_id}")
async def ws_task(ws: WebSocket, task_id: str):
    task = task_manager.get_task(task_id)
    if not task:
        await ws.close(code=1008)
        return
    await ws.accept()
    queue: asyncio.Queue = asyncio.Queue()
    task.subscribe(queue)
    await ws.send_json(task.snapshot())

    async def pump():
        while True:
            await ws.send_json(await queue.get())

    pump_task = asyncio.create_task(pump())
    try:
        while True:
            await ws.receive_text()       # 仅用于感知客户端断开
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        pump_task.cancel()
        task.unsubscribe(queue)


# 前端静态站点（放最后，避免吞掉 /api 路由）
app.mount("/", StaticFiles(directory=str(config.FRONTEND_DIR), html=True), name="frontend")
