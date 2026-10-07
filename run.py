"""cat-sniffer 一键启动

用法（在项目根目录 cat-sniffer/ 下）：
    python run.py
首次使用需先安装依赖：
    pip install -r requirements.txt
    python -m playwright install chromium
"""
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import uvicorn

from backend import config


def _get_lan_ip() -> str:
    """获取本机局域网 IP，取不到返回空串"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return ""


def main():
    settings = config.load_settings()
    print("=" * 56)
    print("  cat-sniffer 视频嗅探器")
    print("=" * 56)
    if settings.get("ffmpeg_path"):
        print(f"  ffmpeg : {settings['ffmpeg_path']}")
    else:
        print("  ffmpeg : 未找到（m3u8/mpd 下载前请在页面设置中指定 ffmpeg.exe）")
    print(f"  输出到 : {settings['output_dir']}")
    url = f"http://{settings['host']}:{settings['port']}"
    print(f"  本机访问  : http://127.0.0.1:{settings['port']}")
    if settings["host"] == "0.0.0.0":
        lan_ip = _get_lan_ip()
        if lan_ip:
            print(f"  局域网访问: http://{lan_ip}:{settings['port']}")
    else:
        print(f"  请在浏览器打开: {url}")
    print("=" * 56)
    uvicorn.run("backend.main:app",
                host=settings["host"], port=settings["port"], log_level="warning")


if __name__ == "__main__":
    main()
