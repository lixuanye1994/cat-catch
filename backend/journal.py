"""下载任务持久化日志：服务重启后可识别并恢复未完成下载"""
import json
import time
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
JOURNAL_FILE = DATA_DIR / "journal.json"
TMP_DIR = DATA_DIR / "tmp"

DATA_DIR.mkdir(parents=True, exist_ok=True)
TMP_DIR.mkdir(parents=True, exist_ok=True)

ACTIVE = ("running",)
RESUMABLE = ("running", "interrupted", "error")
FINISHED = ("done", "canceled")


def load() -> dict:
    if not JOURNAL_FILE.is_file():
        return {}
    try:
        data = json.loads(JOURNAL_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    # 上次进程退出时仍在跑的，标记为中断
    changed = False
    for rec in data.values():
        if rec.get("status") == "running":
            rec["status"] = "interrupted"
            changed = True
    if changed:
        save(data)
    return data


def save(data: dict):
    JOURNAL_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def upsert(record: dict):
    data = load()
    record = dict(record)
    record["updated"] = int(time.time())
    data[record["dl_id"]] = record
    save(data)


def get(dl_id: str) -> dict | None:
    return load().get(dl_id)


def remove(dl_id: str):
    data = load()
    if dl_id in data:
        del data[dl_id]
        save(data)


def unfinished() -> list[dict]:
    return [r for r in load().values() if r.get("status") in RESUMABLE]


def job_tmp_dir(dl_id: str) -> Path:
    path = TMP_DIR / dl_id
    path.mkdir(parents=True, exist_ok=True)
    return path
