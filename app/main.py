"""FastAPI 启动入口。

用法：
    uvicorn app.main:app --reload
    或
    python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from app.api import app

__all__ = ["app"]
