"""路径与全局配置：ffmpeg 探测、配置持久化"""
import json
import os
import shutil
import subprocess
from pathlib import Path

STANDALONE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = STANDALONE_DIR / "data"
PROFILES_DIR = DATA_DIR / "profiles"
CONFIG_FILE = DATA_DIR / "config.json"
SEARCH_JS = STANDALONE_DIR / "backend" / "injected" / "search.js"
FRONTEND_DIR = STANDALONE_DIR / "frontend"
DEFAULT_OUTPUT_DIR = STANDALONE_DIR / "downloads"

DATA_DIR.mkdir(parents=True, exist_ok=True)
DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# 常见 ffmpeg 放置位置（Windows / 通用）
FFMPEG_CANDIDATES = [
    os.environ.get("FFMPEG"),
    shutil.which("ffmpeg"),
    STANDALONE_DIR / "ffmpeg" / "bin" / "ffmpeg.exe",   # 用户自行放入 ffmpeg/bin
    STANDALONE_DIR / "ffmpeg.exe",
    STANDALONE_DIR / "bin" / "ffmpeg.exe",
    Path(r"C:\ffmpeg\bin\ffmpeg.exe"),
    Path(r"C:\Program Files\ffmpeg\bin\ffmpeg.exe"),
]

DEFAULT_SETTINGS = {
    "ffmpeg_path": "",
    "ffprobe_path": "",
    "output_dir": str(DEFAULT_OUTPUT_DIR),
    "host": "127.0.0.1",
    "port": 9800,
    "bilibili_sessdata": "",
}


def _is_executable(path: str) -> bool:
    try:
        proc = subprocess.run(
            [str(path), "-version"],
            capture_output=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return proc.returncode == 0
    except Exception:
        return False


def autodetect_ffmpeg(explicit: str = "") -> tuple[str, str]:
    """返回 (ffmpeg_path, ffprobe_path)，找不到返回空串"""
    candidates = [explicit] + [str(c) if c else "" for c in FFMPEG_CANDIDATES]
    for candidate in filter(None, candidates):
        if Path(candidate).is_file() and _is_executable(candidate):
            ffprobe = str(Path(candidate).with_name(
                "ffprobe.exe" if os.name == "nt" else "ffprobe"))
            if not Path(ffprobe).is_file():
                ffprobe = shutil.which("ffprobe") or ""
            return candidate, ffprobe
    return "", ""


def load_settings() -> dict:
    settings = dict(DEFAULT_SETTINGS)
    if CONFIG_FILE.is_file():
        try:
            settings.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
    if not settings.get("ffmpeg_path"):
        ffmpeg, ffprobe = autodetect_ffmpeg()
        settings["ffmpeg_path"] = ffmpeg
        settings["ffprobe_path"] = ffprobe
    return settings


def save_settings(settings: dict) -> dict:
    current = load_settings()
    current.update({k: v for k, v in settings.items() if k in DEFAULT_SETTINGS})
    # 用户显式指定 ffmpeg 时校验，并自动配对同目录 ffprobe
    if settings.get("ffmpeg_path"):
        if not _is_executable(settings["ffmpeg_path"]):
            raise ValueError("ffmpeg 无法运行，请检查路径")
        if not current.get("ffprobe_path") or \
                Path(current["ffprobe_path"]).parent != Path(settings["ffmpeg_path"]).parent:
            ffprobe = str(Path(settings["ffmpeg_path"]).with_name(
                "ffprobe.exe" if os.name == "nt" else "ffprobe"))
            current["ffprobe_path"] = ffprobe if Path(ffprobe).is_file() else ""
    Path(current["output_dir"]).mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(
        json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    return current
