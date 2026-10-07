"""Playwright 嗅探任务：等价替代扩展的注入 + webRequest"""
import asyncio
import uuid
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from . import config
from .matcher import MediaMatcher


def _header(headers: dict, name: str) -> str:
    """CDP 头字典大小写不一，忽略大小写取值"""
    if not headers:
        return ""
    target = name.lower()
    for k, v in headers.items():
        if k.lower() == target:
            return str(v)
    return ""


class SniffTask:
    def __init__(self, url: str):
        self.task_id = uuid.uuid4().hex[:12]
        self.url = url
        self.status = "starting"       # starting / running / stopped / error
        self.page_url = url
        self.page_title = ""
        self.error = ""

        self._matcher = MediaMatcher()
        self.items: list[dict] = []
        self._items_by_req: dict[str, dict] = {}
        self._req_headers: dict[str, dict] = {}
        self._subscribers: set[asyncio.Queue] = set()

        self._pw = None
        self._context = None
        self._page = None
        self._stop = asyncio.Event()

    # ---------- 事件总线 ----------
    def subscribe(self, queue: asyncio.Queue):
        self._subscribers.add(queue)

    def unsubscribe(self, queue: asyncio.Queue):
        self._subscribers.discard(queue)

    def broadcast(self, message: dict):
        for queue in list(self._subscribers):
            queue.put_nowait(message)

    def snapshot(self) -> dict:
        return {
            "type": "snapshot",
            "task_id": self.task_id,
            "url": self.url,
            "status": self.status,
            "page_url": self.page_url,
            "page_title": self.page_title,
            "error": self.error,
            "items": self.items,
        }

    # ---------- 生命周期 ----------
    async def run(self):
        try:
            await self._launch()
        except Exception as e:
            self.status = "error"
            self.error = str(e)
            self.broadcast({"type": "status", "status": self.status, "error": str(e)})
            await self._teardown()

    async def stop(self):
        self._stop.set()
        await self._teardown()
        if self.status != "error":
            self.status = "stopped"
        self.broadcast({"type": "status", "status": self.status})

    async def _teardown(self):
        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass
            self._pw = None

    async def _launch(self):
        self._pw = await async_playwright().start()
        profile_dir = config.PROFILES_DIR / "desktop"
        self._context = await self._pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()

        # 1) CDP 网络监听（等价 chrome.webRequest）
        cdp = await self._context.new_cdp_session(self._page)
        await cdp.send("Network.enable")
        cdp.on("Network.requestWillBeSent", self._on_request_will_be_sent)
        cdp.on("Network.responseReceived", self._on_response_received)
        cdp.on("Network.loadingFinished", self._on_loading_finished)
        cdp.on("Network.loadingFailed", self._on_loading_failed)

        # 2) search.js 钩子上报桥（exposeFunction → Python）
        await self._page.expose_function("__catCatchReport", self._on_hook_report)
        # 弹窗新页面同样挂桥（init script 是 context 级，自动生效）
        self._context.on("page", self._on_new_page)

        # 3) 等价扩展的 document_start + MAIN world 注入
        await self._context.add_init_script(path=str(config.SEARCH_JS))

        self.broadcast({"type": "log", "message": f"打开页面：{self.url}"})
        await self._page.goto(self.url, wait_until="domcontentloaded", timeout=60000)
        self.status = "running"
        await self._refresh_page_meta()
        self.broadcast({"type": "status", "status": self.status,
                        "page_url": self.page_url, "page_title": self.page_title})

        await self._trigger_lazy_load()

    async def _refresh_page_meta(self):
        try:
            self.page_url = self._page.url
            self.page_title = await self._page.title()
        except Exception:
            pass

    async def _trigger_lazy_load(self):
        """滚动页面触发懒加载资源（约35秒或直到停止）"""
        for _ in range(14):
            if self._stop.is_set():
                return
            await asyncio.sleep(2.5)
            try:
                await self._page.mouse.wheel(0, 4000)
            except Exception:
                return

    # ---------- CDP 回调 ----------
    def _on_request_will_be_sent(self, params: dict):
        request_id = params["requestId"]
        self._req_headers[request_id] = dict(params["request"].get("headers") or {})

    def _on_response_received(self, params: dict):
        request_id = params["requestId"]
        response = params.get("response") or {}
        url = response.get("url", "")
        mime = response.get("mimeType", "")
        resp_headers = response.get("headers") or {}
        size = None
        length = _header(resp_headers, "content-length")
        if length.isdigit():
            size = int(length)

        item = self._matcher.accept_response(
            url=url, mime=mime, size=size,
            page_url=self.page_url, title=self.page_title,
        )
        if item:
            item["requestId"] = request_id
            self.items.append(item)
            self._items_by_req[request_id] = item
            self.broadcast({"type": "media", "item": item})

    def _on_loading_finished(self, params: dict):
        request_id = params["requestId"]
        item = self._items_by_req.get(request_id)
        encoded = params.get("encodedDataLength", 0)
        if item and not item.get("size") and encoded:
            item["size"] = encoded
            self.broadcast({"type": "update", "item": item})
        self._req_headers.pop(request_id, None)

    def _on_loading_failed(self, params: dict):
        request_id = params["requestId"]
        self._req_headers.pop(request_id, None)
        self._items_by_req.pop(request_id, None)

    # ---------- search.js 回调 ----------
    async def _on_new_page(self, page):
        try:
            await page.expose_function("__catCatchReport", self._on_hook_report)
        except Exception:
            pass

    async def _on_hook_report(self, data: dict):
        try:
            await self._refresh_page_meta()
            item = self._matcher.accept_hook(
                data, page_url=self.page_url, title=self.page_title)
            if item:
                self.items.append(item)
                self.broadcast({"type": "media", "item": item})
        except Exception as e:
            self.broadcast({"type": "log", "message": f"钩子处理异常: {e}"})

    # ---------- 下载所需头信息 ----------
    async def build_download_headers(self, item: dict) -> dict:
        raw = self._req_headers.get(item.get("requestId", ""), {})
        headers = {
            "User-Agent": _header(raw, "user-agent"),
            "Referer": _header(raw, "referer") or self.page_url,
            "Cookie": _header(raw, "cookie"),
        }
        # Cookie 兜底：从浏览器 Cookie 仓拼
        if not headers["Cookie"] and self._context and item["url"]:
            try:
                cookies = await self._context.cookies(item["url"])
                headers["Cookie"] = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
            except Exception:
                pass
        return headers

    def base_title(self, item: dict) -> str:
        if self.page_title:
            return self.page_title
        host = urlparse(item["url"]).hostname or "video"
        return host
