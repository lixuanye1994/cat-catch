"""媒体资源过滤：从 background.js findMedia 移植的核心规则

输入两路数据：
1. CDP 网络事件（responseReceived / loadingFinished）
2. search.js 钩子上报（catCatchAddMedia / catCatchAddKey）
输出统一的 item dict。
"""
import re
import time
from urllib.parse import unquote, urlparse

# 与 js/init.js G.OptionLists.Ext 保持一致（ts/srt/vtt 默认不抓取以减少切片噪音）
VIDEO_EXTS = {
    "flv", "hlv", "f4v", "mp4", "webm", "ogg", "ogv", "mov", "mkv",
    "m4s", "mpeg", "avi", "wmv", "asf", "movie", "divx", "mpeg4",
    "vid", "acc", "aac",
}
AUDIO_EXTS = {"mp3", "wma", "wav", "m4a", "ogg", "opus", "weba", "aac"}
HLS_EXTS = {"m3u8", "m3u"}
DASH_EXTS = {"mpd"}

HLS_MIME = {
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "application/mpegurl",
    "application/octet-stream-m3u8",
}
DASH_MIME = {"application/dash+xml"}
AUDIO_MIME_PREFIX = "audio/"
VIDEO_MIME_PREFIX = "video/"

_INVALID_HOSTS = {"", "localhost", "127.0.0.1"}
_RE_DATA_URL = re.compile(r"^data:(application|video|audio)/", re.I)


def fileNameParse(pathname: str) -> tuple[str, str]:
    """移植 function.js fileNameParse：取文件名和扩展名"""
    name = pathname.rsplit("/", 1)[-1]
    if "." not in name:
        return name, ""
    short_name, ext = name.rsplit(".", 1)
    return short_name, ext.lower()


def sanitize_filename(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*~\x00-\x1f]', "_", name).strip(" .") or "video"


class MediaMatcher:
    def __init__(self):
        self._seen: set[str] = set()
        self._seq = 0

    def _new_item(self, **kw) -> dict:
        self._seq += 1
        item = {
            "id": self._seq,
            "url": "",
            "ext": "",
            "mime": "",
            "size": None,
            "kind": "other",          # hls / dash / video / audio / key
            "source": "network",      # network / hook
            "host": "",
            "page_url": "",
            "title": "",
            "time": int(time.time() * 1000),
        }
        item.update(kw)
        return item

    # ---- 分类 ----
    def _classify(self, ext: str, mime: str) -> str | None:
        mime = (mime or "").split(";", 1)[0].strip().lower()
        if ext in HLS_EXTS or mime in HLS_MIME:
            return "hls"
        if ext in DASH_EXTS or mime in DASH_MIME:
            return "dash"
        if mime.startswith(AUDIO_MIME_PREFIX) or ext in AUDIO_EXTS:
            return "audio"
        if mime.startswith(VIDEO_MIME_PREFIX) or ext in VIDEO_EXTS:
            return "video"
        if mime == "application/ogg":
            return "audio"
        return None

    # ---- 网络事件入口 ----
    def accept_response(self, *, url: str, mime: str = "", size: int | None = None,
                        page_url: str = "", title: str = "", source: str = "network"):
        if not url or url.startswith(("chrome:", "chrome-extension:", "devtools:", "data:image")):
            return None
        if _RE_DATA_URL.match(url):
            return None
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https", "blob"):
            return None
        if parsed.scheme != "blob" and parsed.hostname in _INVALID_HOSTS:
            return None

        ext = ""
        if parsed.scheme != "blob":
            _, ext = fileNameParse(parsed.path)
        kind = self._classify(ext, mime)
        if not kind:
            return None
        # m4s/单独切片只有在有明显视频 MIME 时才放行，避免 DASH 切片刷屏
        if ext in ("m4s", "ts") and not mime:
            return None

        key = url.split("#", 1)[0]
        if key in self._seen:
            return None
        self._seen.add(key)

        return self._new_item(
            url=url, ext=ext, mime=mime, size=size, kind=kind, source=source,
            host=parsed.hostname or "blob", page_url=page_url, title=title,
        )

    # ---- search.js 钩子入口 ----
    def accept_hook(self, data: dict, *, page_url: str = "", title: str = ""):
        action = data.get("action")
        if action == "catCatchAddKey":
            key = data.get("key", "")
            if not key:
                return None
            dedup = "key:" + key
            if dedup in self._seen:
                return None
            self._seen.add(dedup)
            return self._new_item(
                url=data.get("url", ""), ext=data.get("ext", "key"),
                kind="key", source="hook", host=urlparse(page_url).hostname or "",
                page_url=page_url, title=title, key=key,
            )

        if action != "catCatchAddMedia":
            return None
        url = data.get("url", "")
        if not url:
            return None
        key = url.split("#", 1)[0]
        if key in self._seen:
            return None
        parsed = urlparse(url) if not url.startswith("blob:") else None
        ext = (data.get("ext") or data.get("extraExt") or "").lower()
        if not ext and parsed:
            _, ext = fileNameParse(parsed.path)
        mime = (data.get("mime") or "").lower()
        kind = self._classify(ext, mime)
        if not kind:
            # 钩子上报但无法分类的，按原始视频资源保留
            kind = "video"
        self._seen.add(key)
        return self._new_item(
            url=url, ext=ext, mime=mime, size=None, kind=kind, source="hook",
            host=(parsed.hostname if parsed else "blob") or "blob",
            page_url=data.get("href") or page_url, title=title,
        )

    def has(self, url: str) -> bool:
        return url.split("#", 1)[0] in self._seen
