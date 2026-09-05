"""数据库连接管理：每个调用方获取独立 MySQL 连接。

项目约定：
- Worker / API 各自持有自己的连接，不要跨进程共享连接。
- 事务由调用方（repository 函数）负责 commit / rollback。
"""

from __future__ import annotations

import mysql.connector
from mysql.connector.abstracts import MySQLConnectionAbstract

from app.config import get_settings


def create_connection() -> MySQLConnectionAbstract:
    """创建一个新的 MySQL 连接（autocommit=False）。

    关闭事务控制由调用方负责；默认关闭 autocommit，
    便于认领任务等操作使用显式事务。
    """
    settings = get_settings()
    return mysql.connector.connect(
        host=settings.mysql_host,
        port=settings.mysql_port,
        user=settings.mysql_user,
        password=settings.mysql_password,
        database=settings.mysql_database,
        autocommit=False,
        charset="utf8mb4",
    )
