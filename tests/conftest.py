import sys
from pathlib import Path

# 确保 tests 目录下的 import 始终能找到项目根目录的 app 包
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
