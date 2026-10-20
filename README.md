from pathlib import Path
from shutil import copy2, copytree
src = Path(r"E:\200行的agent\ticket-agent")
dst = Path(r"E:\200行的agent\.repo-upload-yumaiduo-20261020")
exclude = {"data", "audits", "output", "__pycache__", ".pytest_cache", ".ruff_cache", ".playwright-cli", "demos"}
for child in src.iterdir():
    if child.name in exclude or child.name.startswith("release-") or child.name.startswith(".audit-"):
        continue
    target = dst / child.name
    if child.is_dir():
        copytree(child, target, dirs_exist_ok=True, ignore=lambda d,n: {x for x in n if x in exclude or x.startswith("release-") or x.startswith(".audit-") or x in {"__pycache__", ".pytest_cache", ".ruff_cache", ".playwright-cli"}})
    elif child.suffix in {".py", ".md", ".txt"} and child.name not in {"buyer_texts.json", "live-model-replies.json"}:
        copy2(child, target)
''@; $script | python -; @'
# yumaiduo新版票务客服插件

这是新版 `ticket-agent` 插件源码，包含图片识别报价、万达核价、订单事实同步、客服 Agent 和配置界面。

## 本地验证

```powershell
python run_checks.py --current
```

当前测试不得连接生产支付、出票或发货接口。
