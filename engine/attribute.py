"""归因 agent 的兼容入口 —— 实现已经搬到 `adsdesk/agents/attribute.py`。

为什么留这个文件：launchd 的 `com.baoyanan.ads-attribute.plist` 指向
`engine/attribute.py`，改 plist 要重载服务、还会在机器上多一个「谁才是真身」的
疑问。所以这里只做一件事：把命令行原样转交给计划书 §8.4 指定的那个文件。

不要在这里写逻辑。两份实现 = 两个写者 = 同一条 decision 被两个口径回填。
老实现留档在 `engine/attribute_engine_v1.py.bak`（501 行），只作对照。

    python3 engine/attribute.py            # 等价于 python3 adsdesk/agents/attribute.py
    python3 engine/attribute.py --dry-run
"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

AGENT = Path(__file__).resolve().parents[1] / "adsdesk" / "agents" / "attribute.py"

if __name__ == "__main__":
    if not AGENT.exists():
        sys.stderr.write(f"找不到归因 agent：{AGENT}\n")
        raise SystemExit(2)
    sys.argv[0] = str(AGENT)          # --help 的 prog 名跟着走
    runpy.run_path(str(AGENT), run_name="__main__")