"""Application settings — Tushare Token唯一入口，所有配置集中管理。

从 backend/.env 读取环境变量，禁止在业务代码中硬编码配置值。
"""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """全局配置。零硬编码——所有魔法数字、URL、阈值均在此定义。"""

    # ── 应用 ──
    app_name: str = "A股量化分析助手"
    debug: bool = False
    data_dir: str = "data"
    user_data_dir: str = "data/user_data"  # 用户专属自选股JSON存储目录

    # ── Tushare（唯一数据源）──
    tushare_token: str = ""

    # ── 数据库 ──
    @property
    def database_url(self) -> str:
        os.makedirs(self.data_dir, exist_ok=True)
        db_path = os.path.join(self.data_dir, "stock_analyzer.db")
        return f"sqlite+aiosqlite:///{db_path}"

    # ── JWT ──
    jwt_secret_key: str = ""
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 7

    # ── DeepSeek AI ──
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"  # DeepSeek-V4-Flash via deepseek-chat endpoint
    ai_analysis_cost: int = 2  # 每次AI分析扣2积分

    # ── CORS ──
    cors_origins: str = "*"

    # ── 管理员种子账户（仅首次启动时创建，上线后建议删除此配置）──
    admin_seed_phone: str = ""
    admin_seed_password: str = ""

    # ── Tushare 频率限制（安全阈值 < 官方限额）──
    tushare_general_rate: int = 180   # 通用接口 200次/min → 安全180
    tushare_financial_rate: int = 70  # 财务接口 80次/min → 安全70
    tushare_daily_credit_limit: int = 2000

    # ── 缓存 TTL（秒）──
    cache_daily_ttl: int = 300         # 实时日线 5min
    cache_diagnosis_ttl: int = 86400   # 诊股报告 24h
    cache_stock_basic_ttl: int = 0     # 股票列表 永久(0=不过期)
    cache_offline_ttl: int = 86400     # 离线计算结果 当日有效

    # ── 默认分页 ──
    default_page_size: int = 20
    max_page_size: int = 100

    # ── 自选股配额（按 tier）──
    favorite_quota: dict = {0: 0, 1: 10, 2: 20, 3: 30, 99: 999}
    favorite_page_size: int = 10

    @model_validator(mode="after")
    def _validate_production(self):
        if not self.debug and not self.jwt_secret_key:
            raise ValueError("JWT_SECRET_KEY is required in production (set DEBUG=true for dev)")
        return self

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
