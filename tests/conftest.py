"""pytest 共享配置：把仓库根加进 sys.path，未 pip install -e 的源码树也能跑测试。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
