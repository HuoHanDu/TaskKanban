"""应用配置：从环境变量 / .env 文件读取数据库连接参数。"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_user: str = "taskkanban"
    mysql_password: str = ""
    mysql_database: str = "taskkanban"


@lru_cache
def get_settings() -> Settings:
    """返回缓存后的配置实例，避免重复读取 .env。"""
    return Settings()
