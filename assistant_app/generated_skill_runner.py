from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        raise RuntimeError("缺少代码技能路径")
    path = Path(sys.argv[1]).resolve()
    specification = importlib.util.spec_from_file_location("maomao_generated_skill", path)
    if specification is None or specification.loader is None:
        raise RuntimeError("无法加载代码技能")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    run = getattr(module, "run", None)
    if not callable(run):
        raise RuntimeError("代码技能缺少 run 函数")
    result = run(sys.stdin.read())
    print(str(result or "技能已执行完成。"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
