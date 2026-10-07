"""M3U8 解析与并发下载

- probe：读取 master/媒体列表，返回全部画质、分片数、时长
- HlsDownloader：指定画质并发下载分片（AES-128 解密），合并后交 ffmpeg 封装
不支持：直播、DRM、EXT-X-BYTERANGE 字节范围分片、独立音轨/字幕。
"""
import asyncio
import os
import re
from collections import deque
from pathlib import Path
from urllib.parse import urljoin

import httpx


class HlsError(Exception):
    pass


# ================= 列表解析 =================
def _attr(line: str, name: str) -> str:
    m = re.search(rf'{name}=("([^"]*)"|[^,]+)', line)
    if not m:
        return ""
    return m.group(2) if m.group(2) is not None else m.group(1).strip()


def parse_master(text: str, base_url: str) -> list[dict]:
    """返回变体描述 [{url, bandwidth, resolution, codecs}]"""
    variants, pending = [], None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXT-X-I-FRAME-STREAM-INF"):
            pending = None          # 仅关键帧的视角，跳过
            continue
        if line.startswith("#EXT-X-STREAM-INF"):
            pending = {
                "url": "",
                "bandwidth": int(_attr(line, "BANDWIDTH") or 0),
                "resolution": _attr(line, "RESOLUTION"),
                "codecs": _attr(line, "CODECS"),
            }
        elif line and not line.startswith("#"):
            if pending is not None:
                pending["url"] = urljoin(base_url, line)
                variants.append(pending)
                pending = None
    return variants


def parse_media(text: str, base_url: str) -> dict:
    """解析媒体列表：分片、密钥、fMP4 初始化段、总时长"""
    if "#EXT-X-ENDLIST" not in text:
        raise HlsError("这是直播流，暂不支持下载（仅支持已结束的点播视频）")

    segments: list[dict] = []
    seq = 0
    key = None
    is_fmp4 = False
    init_uri = ""
    duration = 0.0
    pending_dur = None

    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXT-X-MEDIA-SEQUENCE"):
            seq = int(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-KEY"):
            if "METHOD=NONE" in line:
                key = None
            else:
                method = _attr(line, "METHOD")
                uri = _attr(line, "URI")
                if method != "AES-128" or not uri:
                    raise HlsError(f"暂不支持的加密方式：{method or '未知'}")
                iv_m = re.search(r'IV=0x([0-9a-fA-F]+)', line)
                key = {"uri": urljoin(base_url, uri),
                       "iv": bytes.fromhex(iv_m.group(1)) if iv_m else None}
        elif line.startswith("#EXT-X-MAP"):
            uri = _attr(line, "URI")
            if not uri:
                raise HlsError("无法解析 fMP4 初始化段")
            is_fmp4 = True
            init_uri = urljoin(base_url, uri)
        elif line.startswith("#EXT-X-BYTERANGE"):
            raise HlsError("暂不支持字节范围（BYTE-RANGE）分片")
        elif line.startswith("#EXTINF"):
            m = re.search(r"EXTINF:([\d.]+)", line)
            pending_dur = float(m.group(1)) if m else 0.0
        elif line and not line.startswith("#") and pending_dur is not None:
            segments.append({
                "index": len(segments),
                "seq": seq + len(segments),
                "uri": urljoin(base_url, line),
                "key": key,
            })
            duration += pending_dur
            pending_dur = None

    if not segments:
        raise HlsError("播放列表中没有分片")
    return {"segments": segments, "is_fmp4": is_fmp4,
            "init_uri": init_uri, "duration": duration}


# ================= 探测（只解析不下载） =================
async def probe(url: str, headers: dict) -> dict:
    async with httpx.AsyncClient(
        follow_redirects=True, timeout=30, headers=headers,
    ) as client:
        master_text = await _get_text(client, url)

    if "#EXT-X-STREAM-INF" in master_text:
        variants = parse_master(master_text, url)
        if not variants:
            raise HlsError("Master 列表中没有可用画质")
    else:
        # 本身就是媒体列表：单一画质
        variants = [{"url": url, "bandwidth": 0, "resolution": "", "codecs": ""}]

    results = []
    async with httpx.AsyncClient(
        follow_redirects=True, timeout=30, headers=headers,
    ) as client:
        for v in variants:
            text = await _get_text(client, v["url"])
            info = parse_media(text, v["url"])
            segs = info["segments"]
            results.append({
                "url": v["url"],
                "bandwidth": v["bandwidth"],
                "resolution": v["resolution"],
                "codecs": v["codecs"],
                "segments": len(segs),
                "duration": round(info["duration"], 1),
                "fmp4": info["is_fmp4"],
                "encrypted": any(s["key"] for s in segs),
            })

    results.sort(key=lambda x: x["bandwidth"], reverse=True)
    for i, r in enumerate(results):
        r["id"] = i
    return {"variants": results, "duration": results[0]["duration"]}


async def _get_text(client: httpx.AsyncClient, url: str) -> str:
    resp = await client.get(url)
    if resp.status_code >= 400:
        raise HlsError(f"播放列表获取失败 HTTP {resp.status_code}")
    ctype = resp.headers.get("content-type", "")
    if "json" in ctype:
        raise HlsError("返回的不是 M3U8 播放列表")
    return resp.text


# ================= 并发下载器 =================
def _seg_name(index: int) -> str:
    return f"seg_{index:06d}"


class HlsDownloader:
    def __init__(self, url: str, headers: dict, tmp_dir: Path, *,
                 concurrency: int = 4, retries: int = 3):
        self.url = url
        self.headers = headers
        self.tmp_dir = tmp_dir
        self.seg_dir = tmp_dir / "segs"
        self.seg_dir.mkdir(parents=True, exist_ok=True)
        self.concurrency = max(1, min(16, concurrency))
        self.retries = max(0, min(10, retries))
        self._keys: dict[str, bytes] = {}
        self.is_fmp4 = False

    async def run(self, cancel: asyncio.Event, on_stats) -> Path:
        limits = httpx.Limits(max_connections=self.concurrency * 2 + 4)
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(30, read=120),
            headers=self.headers, limits=limits,
        ) as client:
            text = await _get_text(client, self.url)
            info = parse_media(text, self.url)
            segments = info["segments"]
            total = len(segments)
            self.is_fmp4 = info["is_fmp4"]

            # fMP4 初始化段（原子写入）
            if info["init_uri"]:
                init_path = self.tmp_dir / "init.mp4"
                if not init_path.exists():
                    await self._atomic(client, info["init_uri"], init_path, None, 0)

            # 清理中断残留的半截分片
            for stale in self.seg_dir.glob("seg_*.part"):
                stale.unlink()

            # 已完成的分片直接计入进度（.part 为中断残留，需重下）
            done = 0
            bytes_total = 0
            for s in segments:
                p = self.seg_dir / _seg_name(s["index"])
                if p.exists() and p.stat().st_size > 0:
                    done += 1
                    bytes_total += p.stat().st_size
            init_p = self.tmp_dir / "init.mp4"
            if init_p.exists():
                bytes_total += init_p.stat().st_size

            speed_win: deque = deque()
            inflight: set[str] = set()
            failed: list[dict] = []
            sem = asyncio.Semaphore(self.concurrency)

            def emit():
                now = asyncio.get_event_loop().time()
                speed_win.append((now, bytes_total))
                while speed_win and now - speed_win[0][0] > 3:
                    speed_win.popleft()
                speed = 0
                if len(speed_win) >= 2:
                    dt = speed_win[-1][0] - speed_win[0][0]
                    speed = int((speed_win[-1][1] - speed_win[0][1]) / dt) if dt > 0 else 0
                on_stats({
                    "segments_done": done,
                    "segments_total": total,
                    "bytes_downloaded": bytes_total,
                    "speed": speed,
                    "current_segments": sorted(inflight),
                })

            async def worker(seg: dict):
                nonlocal done, bytes_total
                async with sem:
                    if cancel.is_set():
                        return
                    name = _seg_name(seg["index"])
                    inflight.add(name)
                    try:
                        for attempt in range(self.retries + 1):
                            if cancel.is_set():
                                return
                            try:
                                n = await self._atomic(
                                    client, seg["uri"],
                                    self.seg_dir / name, seg["key"], seg["seq"])
                                done += 1
                                bytes_total += n
                                emit()
                                return
                            except Exception:
                                if attempt >= self.retries:
                                    failed.append(seg)
                                    return
                                await asyncio.sleep(min(0.5 * (2 ** attempt), 10))
                    finally:
                        inflight.discard(name)

            emit()
            tasks = [asyncio.create_task(worker(s))
                     for s in segments
                     if not (self.seg_dir / _seg_name(s["index"])).exists()]
            await asyncio.gather(*tasks)

            if cancel.is_set():
                # 主动暂停，不合并
                raise _Paused()
            if failed:
                raise HlsError(f"{len(failed)} 个分片下载失败，可点继续重试")

        return await asyncio.to_thread(self._combine)

    async def _atomic(self, client: httpx.AsyncClient, url: str,
                      dest: Path, key_info, seq: int) -> int:
        """下载→解密→写 .part→改名，返回写入字节数"""
        resp = await client.get(url)
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}")
        data = resp.content
        if key_info:
            data = await self._decrypt(client, data, key_info, seq)
        part = dest.with_name(dest.name + ".part")
        part.write_bytes(data)
        os.replace(part, dest)
        return len(data)

    async def _decrypt(self, client: httpx.AsyncClient, data: bytes,
                       key_info: dict, seq: int) -> bytes:
        try:
            from Crypto.Cipher import AES
        except ImportError as e:
            raise HlsError("加密 m3u8 需要 pycryptodome：pip install pycryptodome") from e
        if key_info["uri"] not in self._keys:
            resp = await client.get(key_info["uri"])
            if resp.status_code >= 400:
                raise HlsError(f"密钥获取失败 HTTP {resp.status_code}")
            self._keys[key_info["uri"]] = resp.content
        iv = key_info["iv"] or seq.to_bytes(16, "big")
        return AES.new(self._keys[key_info["uri"]], AES.MODE_CBC, iv).decrypt(data)

    def _combine(self) -> Path:
        seg_files = sorted(p for p in self.seg_dir.glob("seg_*")
                           if not p.name.endswith(".part"))
        combined = self.tmp_dir / ("combined.m4s" if self.is_fmp4 else "combined.ts")
        if not combined.exists():
            with open(combined, "wb") as out:
                init_p = self.tmp_dir / "init.mp4"
                if self.is_fmp4 and init_p.exists():
                    out.write(init_p.read_bytes())
                for sf in seg_files:
                    out.write(sf.read_bytes())
        return combined


class _Paused(Exception):
    """主动暂停，用于跳出下载流程"""
    pass
