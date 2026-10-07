"""本地下载（支持断点续传）

路线：
- http       普通文件：Range 字节级续传
- dash-merge B站 DASH：两条流分别 Range 续传，ffmpeg 合并
- hls        m3u8：并发下载分片（跳过已下载），AES-128 解密，ffmpeg 合并
- dash       mpd：ffmpeg 直连（续传需重新下载）
"""
import asyncio
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from . import journal
from .hls import HlsDownloader, HlsError, _Paused
from .matcher import sanitize_filename

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    n = 1
    while True:
        candidate = parent / f"{stem}_{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


# ================= 字节级续传原语 =================
async def stream_resumable(client: httpx.AsyncClient, url: str, dest: Path,
                           cancel: asyncio.Event, on_progress) -> int:
    """支持 Range 的断点下载，返回总字节数（未知返回0）"""
    have = dest.stat().st_size if dest.exists() else 0
    req_headers = {"Range": f"bytes={have}-"} if have else {}
    async with client.stream("GET", url, headers=req_headers) as resp:
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}")
        if have and resp.status_code == 206:
            rng = resp.headers.get("content-range", "")
            total = int(rng.rsplit("/", 1)[1]) if "/" in rng and rng.rsplit("/", 1)[1].isdigit() else 0
            mode, done = "ab", have
        else:
            # 服务器不支持 Range 或无历史文件：从头开始
            total = int(resp.headers.get("content-length") or 0)
            mode, done = "wb", 0

        last = 0.0
        with open(dest, mode) as f:
            async for chunk in resp.aiter_bytes(1 << 18):
                if cancel.is_set():
                    break
                f.write(chunk)
                done += len(chunk)
                now = time.time()
                if now - last >= 1:
                    on_progress(done, total)
                    last = now
    return total


# ================= 下载任务 =================
class DownloadJob:
    def __init__(self, dl_id: str, *, kind: str, params: dict, headers: dict,
                 title: str, settings: dict, emit, options: dict | None = None):
        self.dl_id = dl_id
        self.kind = kind            # http / dash-merge / hls / dash
        self.params = params
        self.headers = headers
        self.options = options or {}
        self.title = sanitize_filename(title)
        out_dir = self.options.get("output_dir") or settings["output_dir"]
        self.output_dir = Path(out_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.ffmpeg = settings.get("ffmpeg_path") or "ffmpeg"
        self._emit_cb = emit
        self._proc: asyncio.subprocess.Process | None = None
        self._cancel = asyncio.Event()
        self.tmp_dir = journal.job_tmp_dir(dl_id)
        # 累计已用时间（多次续传累加）
        self._elapsed_base = float(self.options.get("_initial_elapsed") or 0)
        self._t0 = time.time()
        self.state = {
            "dl_id": dl_id,
            "kind": kind,
            "filename": "",
            "percent": 0,
            "phase": "starting",
            "status": "running",
            "message": "",
            "elapsed": 0,
            # HLS 专用统计
            "segments_done": 0,
            "segments_total": 0,
            "bytes_downloaded": 0,
            "speed": 0,
            "current_segments": [],
        }

    def _emit(self):
        if self.state["status"] == "running":
            self.state["elapsed"] = int(
                self._elapsed_base + time.time() - self._t0)
        self._emit_cb()

    def cancel(self):
        self._cancel.set()
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.terminate()
            except Exception:
                pass

    async def run(self):
        self._t0 = time.time()
        try:
            if self.kind == "hls":
                await self._run_hls()
            elif self.kind == "dash-merge":
                await self._run_dash_merge()
            elif self.kind == "dash":
                await self._run_ffmpeg_url(".mkv")
            else:
                await self._run_http()
        except _Paused:
            self._freeze_elapsed()
            self._set("canceled", "已暂停，可继续下载")
        except Exception as e:
            self._freeze_elapsed()
            if self._cancel.is_set():
                self._set("canceled", "已停止，可继续下载")
            else:
                self._set("error", str(e))

    def _freeze_elapsed(self):
        self._elapsed_base += time.time() - self._t0
        self.state["elapsed"] = int(self._elapsed_base)

    # ---------- http ----------
    async def _run_http(self):
        url = self.params["url"]
        out_path = unique_path(self.output_dir / f"{self.title}.part")
        self.state["filename"] = out_path.name
        self._set_phase("download")
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=httpx.Timeout(30, read=120),
            headers=self.headers,
        ) as client:
            total = await stream_resumable(
                client, url, out_path, self._cancel,
                lambda d, t: self._progress(d / t * 100 if t else None))

        if self._cancel.is_set():
            self._freeze_elapsed()
            self._set("canceled", "已停止，可继续下载")
            return
        suffix = Path(urlparse(self.params["url"]).path).suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{2,5}", suffix):
            suffix = ".mp4"
        final = unique_path(self.output_dir / f"{self.title}{suffix}")
        out_path.rename(final)
        self.state["filename"] = final.name
        self._set_done(final.name)

    # ---------- dash-merge ----------
    async def _run_dash_merge(self):
        out_path = unique_path(self.output_dir / f"{self.title}.mp4")
        self.state["filename"] = out_path.name
        video_part = self.tmp_dir / "video.m4s.part"
        audio_part = self.tmp_dir / "audio.m4s.part"
        has_audio = bool(self.params.get("audio_url"))
        self._set_phase("download")

        async with httpx.AsyncClient(
            follow_redirects=True, timeout=httpx.Timeout(30, read=120),
            headers=self.headers,
        ) as client:
            parts = [(self.params["video_url"], video_part)]
            if has_audio:
                parts.append((self.params["audio_url"], audio_part))

            totals = [0] * len(parts)
            dones = [0] * len(parts)

            for idx, (url, dest) in enumerate(parts):
                if not (self._cancel.is_set() and dest.exists() and dest.stat().st_size > 0):
                    totals[idx] = await stream_resumable(
                        client, url, dest, self._cancel,
                        lambda d, t, i=idx: self._dash_progress(i, d, t, totals, dones))
                    dones[idx] = dest.stat().st_size
                if self._cancel.is_set():
                    break

            if self._cancel.is_set():
                self._freeze_elapsed()
                self._set("canceled", "已停止，可继续下载")
                return

            self._set_phase("merge")
            args = [self.ffmpeg, "-y", "-i", str(video_part)]
            if has_audio:
                args += ["-i", str(audio_part)]
            args += ["-c", "copy", str(out_path)]
            if not await self._ffmpeg_merge(args):
                self._set("error", "ffmpeg 合并失败")
                return

        self._set_done(out_path.name)

    def _dash_progress(self, idx, done, total, totals, dones):
        dones[idx] = done
        if total:
            totals[idx] = total
        grand_t = sum(totals)
        if grand_t:
            self._progress(sum(dones) / grand_t * 100)

    # ---------- hls ----------
    async def _run_hls(self):
        self._set_phase("download")
        concurrency = int(self.options.get("concurrency", 4))
        retries = int(self.options.get("retries", 3))
        dl = HlsDownloader(
            self.params["url"], self.headers, self.tmp_dir,
            concurrency=concurrency, retries=retries)

        last_emit = [0.0]

        def on_stats(s):
            self.state["segments_done"] = s["segments_done"]
            self.state["segments_total"] = s["segments_total"]
            self.state["bytes_downloaded"] = s["bytes_downloaded"]
            self.state["speed"] = s["speed"]
            self.state["current_segments"] = s["current_segments"]
            now = time.time()
            # 统计 0.5s 推一次；分片名变化立即推
            if (now - last_emit[0] >= 0.5
                    or s["current_segments"] != self._last_current):
                total = s["segments_total"]
                if total:
                    self.state["percent"] = round(
                        min(99.5, s["segments_done"] / total * 100), 1)
                self._emit()
                last_emit[0] = now
                self._last_current = s["current_segments"]

        self._last_current = []
        combined = await dl.run(self._cancel, on_stats)

        self._set_phase("merge")
        out_path = unique_path(self.output_dir / f"{self.title}.mp4")
        self.state["filename"] = out_path.name
        self._emit()

        args = [self.ffmpeg, "-y", "-i", str(combined), "-c", "copy"]
        if not dl.is_fmp4:
            args += ["-bsf:a", "aac_adtstoasc"]
        args.append(str(out_path))

        ok = await self._ffmpeg_merge(args)
        if not ok and not dl.is_fmp4:
            # 非 AAC 音频时去掉 bitstream filter 重试
            args = [self.ffmpeg, "-y", "-i", str(combined),
                    "-c", "copy", str(out_path)]
            ok = await self._ffmpeg_merge(args)
        if not ok:
            self._set("error", "ffmpeg 合并失败")
            return
        self._set_done(out_path.name)

    # ---------- ffmpeg 直连（mpd） ----------
    async def _run_ffmpeg_url(self, suffix: str):
        if not Path(self.ffmpeg).is_file():
            self._set("error", "未配置 ffmpeg，请先在设置中指定 ffmpeg.exe 路径")
            return
        url = self.params["url"]
        out_path = unique_path(self.output_dir / f"{self.title}{suffix}")
        self.state["filename"] = out_path.name
        self._set_phase("download")

        self._proc = await asyncio.create_subprocess_exec(
            self.ffmpeg, "-y", "-i", url,
            "-c", "copy", str(out_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            creationflags=_NO_WINDOW,
        )
        _, stderr = await self._proc.communicate()
        if self._cancel.is_set():
            self._freeze_elapsed()
            self._set("canceled", "已停止（mpd 续传需重新下载）")
            return
        if self._proc.returncode != 0:
            tail = stderr.decode(errors="ignore").strip().splitlines()
            out_path.unlink(missing_ok=True)
            self._set("error", tail[-1] if tail else "ffmpeg 失败")
            return
        self._set_done(out_path.name)

    # ---------- 公共 ----------
    async def _ffmpeg_merge(self, args: list[str]) -> bool:
        self._proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            creationflags=_NO_WINDOW,
        )
        _, stderr = await self._proc.communicate()
        if self._proc.returncode != 0:
            print(stderr.decode(errors="ignore")[-500:])
            return False
        return True

    def _set_phase(self, phase: str):
        self.state["phase"] = phase
        self._emit()

    def _progress(self, percent):
        if percent is not None:
            self.state["percent"] = round(min(99.5, percent), 1)
            self._emit()

    def _set(self, status: str, message: str):
        if self.state["status"] == "running":
            self._freeze_elapsed()
        self.state["status"] = status
        self.state["message"] = message
        self._emit()

    def _set_done(self, filename: str):
        self._elapsed_base += time.time() - self._t0
        self.state["elapsed"] = int(self._elapsed_base)
        self.state["percent"] =100
        self.state["phase"] = "done"
        self._set("done", filename)
