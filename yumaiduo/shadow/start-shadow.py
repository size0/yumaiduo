"""
start-shadow.py —— 一键起影子台（mock 插件 :4100 + V5 影子实例 :8199）

════════════════════════════════════════════════════════════════
★ 为什么用 Python 而不是 .cmd

  我先写了个 start-shadow.cmd，结果连续踩了四个坑，
  每一个都是"Windows 批处理 + 中文路径 + 非 ASCII"这一类的：

    1. ASCII 保存 → 中文全变 `?`
    2. UTF-8 保存 + BOM → cmd 把 BOM 当成 @echo 前面的字符
    3. UTF-8 保存 → 里面的引号变成了全角，start 的参数解析坏掉
    4. ★ rem 行里写了 `|` → cmd 当成管道，把后半行当命令执行：
         'entlyContinue' is not recognized as an internal or external command
       （因为 "Stop-Process -Force" 被拆了）

  而 `chcp 65001` 是在【解析之后】才生效的，
  救不了同一文件里已经被解析坏的中文路径。

  ⇒ 用 Python 起子进程，不经过 cmd 的文本解析。
     路径、编码、引号全都不再是问题。

★ 这个脚本干什么

  就是 subprocess.Popen 两次，然后探一次健康检查。
  没有别的。真正的逻辑在 shadow-run.py / shadow-plugin-mock2.py 里。
════════════════════════════════════════════════════════════════
"""

import argparse
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# ★ Windows 上把输出切成 UTF-8 —— 默认 GBK 打不出 ✓ ✗ ★
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

HERE = Path(__file__).resolve().parent      # ticket-agent/yumaiduo/shadow/
# ★ 往上三级才到工作区：
#     shadow/ → yumaiduo/ → ticket-agent/ → E:\200行的agent
#   上一版写成 parent（只退一级），结果去找
#   ticket-agent/yumaiduo/shadow-run.py —— 找不到。
#   那三个脚本的本体在【工作区根目录】，不在这个目录。
WORKSPACE = HERE.parents[2]
PY = sys.executable

MOCK = WORKSPACE / "shadow-plugin-mock2.py"
RUN = WORKSPACE / "shadow-run.py"

PROCS = []


def spawn(args: list, title: str) -> subprocess.Popen:
    """起一个子进程，日志写文件（用 -u 无缓冲）。"""
    log = HERE / f"{title}.log"
    fh = open(log, "w", encoding="utf-8")           # noqa: SIM115
    p = subprocess.Popen(args, cwd=str(WORKSPACE), stdout=fh, stderr=fh)
    PROCS.append((title, p, fh, log))
    return p


def probe(url: str, timeout: float = 2.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError):
        return None


def wait_up(url: str, name: str, *, seconds: float = 40.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        body = probe(url)
        if body:
            print(f"  ✓ {name}  {body}")
            return True
        time.sleep(0.5)
    print(f"  ✗ {name}  {seconds:.0f} 秒内没起来 —— 看 {name}.log")
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="起影子台")
    ap.add_argument("--stop", action="store_true", help="只停掉正在跑的影子台")
    args = ap.parse_args()

    if args.stop:
        for name, p, fh, _ in PROCS:
            p.terminate()
        print("已停。")
        return 0

    # ---- 前置检查 ----
    missing = [str(p) for p in (MOCK, RUN) if not p.exists()]
    if missing:
        print(f"✗ 找不到脚本：{missing}")
        return 1
    if not (WORKSPACE / ".shadow-data").exists():
        print(f"✗ 数据目录不存在：{WORKSPACE / '.shadow-data'}")
        return 1

    # ★ 端口占用检查（交接文档:199「别抢端口」）——
    #   已经在跑就别再起一个
    for url, name in (("http://127.0.0.1:4100/healthz", "mock:4100"),
                      ("http://127.0.0.1:8199/api/v1/health", "影子:8199")):
        body = probe(url)
        if body:
            print(f"  · {name} 已经在跑：{body}")
            print("    （要重启就先把 python 进程停掉）")
            return 1

    print("=" * 68)
    print("起影子台")
    print("=" * 68)
    print(f"  工作区   {WORKSPACE}")
    print(f"  Python   {PY}")
    print()

    print("--- 1/2 mock 插件 :4100 ---")
    spawn([PY, "-u", str(MOCK), "4100"], "mock")
    if not wait_up("http://127.0.0.1:4100/healthz", "mock"):
        return 1

    print("--- 2/2 V5 影子实例 :8199 ---")
    spawn([PY, str(RUN)], "v5")
    if not wait_up("http://127.0.0.1:8199/api/v1/health", "影子"):
        return 1

    print()
    print("=" * 68)
    print("都起来了。灌轮次：")
    print(f"  {PY} {WORKSPACE / 'shadow-walk.py'}")
    print("=" * 68)
    print("日志：mock.log / v5.log")
    print()
    print("★ 这个脚本起完就退出，子进程【继续在跑】。停止：")
    print("    Get-Process python -EA SilentlyContinue | Stop-Process -Force")
    return 0


if __name__ == "__main__":
    sys.exit(main())
