"""Bilibili 定向解析适配器

流程：b23.tv 短链/普通链接 → bvid → view 接口取分P → wbi 签名 playurl 取 DASH 直链。
参考 B 站开放接口与社区 wbi 签名实现。接口可能随站点调整而失效。
"""
import re

import httpx

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
REFERER = "https://www.bilibili.com"

_API = "https://api.bilibili.com"
_RE_BVID = re.compile(r"(BV[0-9A-Za-z]{10})")
_RE_AID = re.compile(r"[?&]?av(\d+)", re.I)

QUALITY_LABELS = {
    127: "8K 超高清",
    126: "杜比视界",
    125: "HDR 1080P",
    120: "4K 超清",
    116: "1080P60",
    112: "1080P+",
    80: "1080P",
    64: "720P",
    32: "480P",
    16: "360P",
}


class BilibiliError(Exception):
    pass


def _client(sessdata: str = "") -> httpx.AsyncClient:
    headers = {"User-Agent": UA, "Referer": REFERER}
    if sessdata:
        headers["Cookie"] = f"SESSDATA={sessdata}"
    return httpx.AsyncClient(
        follow_redirects=True, timeout=httpx.Timeout(15, read=30),
        headers=headers,
    )


async def resolve_url(url: str, sessdata: str = "") -> dict:
    """链接 → 视频元信息 + 分P列表"""
    bvid, aid = await extract_id(url)
    async with _client(sessdata) as client:
        if bvid:
            meta = await _json(client, "/x/web-interface/view", {"bvid": bvid})
        else:
            meta = await _json(client, "/x/web-interface/view", {"aid": aid})

    data = meta["data"]
    return {
        "bvid": data["bvid"],
        "aid": data["aid"],
        "cid": data["cid"],
        "title": data["title"],
        "desc": data.get("desc", ""),
        "owner": data["owner"]["name"],
        "cover": data["pic"],
        "duration": data["duration"],
        "pages": [
            {"page": p["page"], "cid": p["cid"], "part": p["part"]}
            for p in data.get("pages", [])
        ],
    }


async def extract_id(url: str) -> tuple[str, int]:
    """支持 b23.tv 短链、BV、av 链接"""
    m = _RE_BVID.search(url)
    if m:
        return m.group(1), 0
    m = _RE_AID.search(url)
    if m:
        return "", int(m.group(1))

    if "b23.tv" in url or not url.startswith(("http://", "https://")):
        target = url if url.startswith("http") else "https://" + url.lstrip("/")
        async with _client() as client:
            resp = await client.get(target)
        m = _RE_BVID.search(str(resp.url))
        if m:
            return m.group(1), 0
        m = _RE_AID.search(str(resp.url))
        if m:
            return "", int(m.group(1))

    raise BilibiliError("无法从链接中识别 BV 号，请确认是 B 站视频链接")


async def get_playurl(bvid: str, cid: int, sessdata: str = "") -> list[dict]:
    """返回按清晰度去重、按从高到低排列的流列表"""
    async with _client(sessdata) as client:
        # 匿名访问需要 buvid3 访客标识，否则 playurl 返回"账号未登录"
        if not sessdata:
            try:
                spi = await client.get(f"{_API}/x/frontend/finger/spi")
                buvid3 = spi.json().get("data", {}).get("b_3", "")
                if buvid3:
                    client.cookies.set("buvid3", buvid3, domain=".bilibili.com")
            except Exception:
                pass

        # 非 wbi 端点：匿名可得 480P/360P，携带 SESSDATA 自动解锁更高画质
        params = {
            "bvid": bvid, "cid": cid,
            "fnval": 4048, "fnver": 0, "fourk": 1,
            "qn": 112,
        }
        result = await _json(client, "/x/player/playurl", params)

    data = result.get("data", {})
    dash = data.get("dash")
    if not dash:
        # 老视频或受限：尝试 durl / 低清提示
        if data.get("durl"):
            return [{
                "quality_id": 80,
                "label": "MP4 直链",
                "video_url": data["durl"][0]["url"],
                "audio_url": "",
                "width": data.get("quality"),
                "height": None,
                "codecs": "",
                "size": data["durl"][0].get("size"),
            }]
        raise BilibiliError("未获取到播放地址，可能需要在设置中填写 SESSDATA")

    # 选最高带宽音轨
    audio_list = sorted(dash.get("audio") or [],
                        key=lambda a: a.get("bandwidth", 0), reverse=True)
    audio = audio_list[0] if audio_list else None
    if dash.get("dolby", {}).get("audio"):
        audio = dash["dolby"]["audio"][0]

    seen: set[int] = set()
    qualities = []
    for v in sorted(dash.get("video") or [], key=lambda v: v.get("id", 0), reverse=True):
        qid = v["id"]
        if qid in seen:
            continue
        seen.add(qid)
        qualities.append({
            "quality_id": qid,
            "label": QUALITY_LABELS.get(qid, f"清晰度 {qid}"),
            "video_url": v["baseUrl"],
            "audio_url": audio["baseUrl"] if audio else "",
            "width": v.get("width"),
            "height": v.get("height"),
            "codecs": v.get("codecs", ""),
            "bandwidth": v.get("bandwidth"),
            "size": None,
        })
    return qualities


# ---------- HTTP 工具 ----------
async def _json(client: httpx.AsyncClient, path: str, params: dict) -> dict:
    params = {k: v for k, v in params.items() if v not in ("", None)}
    resp = await client.get(_API + path, params=params)
    payload = resp.json()
    if payload.get("code") != 0:
        raise BilibiliError(payload.get("message") or f"接口错误 {payload.get('code')}")
    return payload


def download_headers(sessdata: str = "") -> dict:
    headers = {"User-Agent": UA, "Referer": REFERER}
    if sessdata:
        headers["Cookie"] = f"SESSDATA={sessdata}"
    return headers
