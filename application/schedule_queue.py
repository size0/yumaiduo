"""共用的「到点队列」。

★★ 为什么要抽
   退款时序（refund_schedule）已经有一份"记一条到点时间 + 由
   handle_run_due 推进"的实现。二次报价要的是同一个形状。
   ⇒ 如果在二次报价里再写一份，就会出现：
        · 两份落盘格式不同、清理逻辑不同
        · 修了一处忘了另一处
     这正是我这几轮反复修的老毛病（时间窗、话术、弹窗样式都栽过）。
   ⇒ 抽成这个模块，两边共用。

★ 落盘：data/schedule-queue.json
     {"items": [{"kind": "...", "key": "...", "due_at": "...", ...}]}
   · kind  哪一类任务（refund / second_quote …）
   · key   幂等键（同一个 key 重复排【不刷新时间】——
           否则"买家每说一句话就把倒计时重来"，永远不到点）
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_FILE = Path(__file__).resolve().parent.parent / "data" / "schedule-queue.json"


def load() -> dict:
    if not _FILE.exists():
        return {"items": []}
    try:
        d = json.loads(_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"items": []}
    if not isinstance(d, dict) or not isinstance(d.get("items"), list):
        return {"items": []}
    return d


def save(data: dict) -> None:
    _FILE.parent.mkdir(parents=True, exist_ok=True)
    _FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                     encoding="utf-8")


def schedule(kind: str, key: str, *, delay_seconds: int = 0,
             now: datetime | None = None, **extra) -> str:
    """
    排一条到点任务。返回该任务的到点时刻（ISO）。

    ★ 幂等：同一个 (kind, key) 已存在 ⇒ **保留原来的时刻**，不刷新。
      理由很重要：买家在等待期间又发了一句话，
      如果刷新时刻，"等 30 秒"就永远等不到。
    """
    now = now or datetime.now()
    due = now + timedelta(seconds=max(0, int(delay_seconds or 0)))
    d = load()
    for it in d["items"]:
        if it.get("kind") == kind and str(it.get("key")) == str(key):
            return str(it.get("due_at") or "")
    item = {"kind": str(kind), "key": str(key),
            "due_at": due.isoformat(timespec="seconds")}
    item.update(extra or {})
    d["items"].append(item)
    save(d)
    return item["due_at"]


def cancel(kind: str, key: str) -> bool:
    """撤掉一条。返回 True 表示确实撤掉了一条。"""
    d = load()
    before = len(d["items"])
    d["items"] = [x for x in d["items"]
                  if not (x.get("kind") == kind
                          and str(x.get("key")) == str(key))]
    if len(d["items"]) != before:
        save(d)
        return True
    return False


def find(kind: str, key: str) -> dict | None:
    for it in load()["items"]:
        if it.get("kind") == kind and str(it.get("key")) == str(key):
            return it
    return None


def due(kind: str | None = None, *, now: datetime | None = None) -> list:
    """到点的任务（可按 kind 过滤）。★ 过滤在【取之前】做，避免误取别的类。"""
    now = now or datetime.now()
    out = []
    for it in load()["items"]:
        if kind is not None and it.get("kind") != kind:
            continue
        try:
            d = datetime.fromisoformat(str(it.get("due_at") or ""))
        except ValueError:
            continue
        if d <= now:
            out.append(it)
    return out


def drop(kind: str, key: str) -> None:
    """静默移除（处理完之后调用）。"""
    cancel(kind, key)
