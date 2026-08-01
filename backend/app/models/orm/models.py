"""SQLAlchemy ORM 模型——SQLite兼容，零外部依赖。

所有JSON字段使用Text存储（序列化为JSON字符串）。
"""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import Column, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


def _now():
    return datetime.now()


def _json_default(obj):
    """将Python对象序列化为JSON字符串，用于Text字段存储。"""
    if isinstance(obj, str):
        return obj
    return json.dumps(obj, ensure_ascii=False, default=str)


def _json_load(value):
    """从Text字段反序列化JSON。"""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return {}


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    phone = Column(String(20), unique=True, nullable=False, index=True)
    password_hash = Column(String(128), nullable=False)
    tier = Column(Integer, default=0)  # 0=游客 1=注册 2=会员
    member_expire = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class Stock(Base):
    __tablename__ = "stocks"

    ts_code = Column(String(20), primary_key=True)
    symbol = Column(String(10), nullable=False)
    name = Column(String(50), nullable=False)
    industry = Column(String(50), default="")
    area = Column(String(20), default="")
    market = Column(String(10), default="")
    list_date = Column(String(8), default="")
    updated_at = Column(DateTime, default=_now)


class StockDaily(Base):
    __tablename__ = "stock_daily"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ts_code = Column(String(20), nullable=False, index=True)
    trade_date = Column(String(8), nullable=False)
    open = Column(Float, default=0)
    high = Column(Float, default=0)
    low = Column(Float, default=0)
    close = Column(Float, default=0)
    pre_close = Column(Float, default=0)
    change = Column(Float, default=0)
    pct_chg = Column(Float, default=0)
    volume = Column(Float, default=0)
    amount = Column(Float, default=0)
    created_at = Column(DateTime, default=_now)

    __table_args__ = ()
    __mapper_args__ = {"confirm_deleted_rows": False}


class StockFinancial(Base):
    __tablename__ = "stock_financials"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ts_code = Column(String(20), nullable=False, index=True)
    report_date = Column(String(10), nullable=False)
    report_type = Column(String(10), default="")  # income / balance / cashflow
    data_json = Column(Text, default="{}")  # 财务报表原始数据
    created_at = Column(DateTime, default=_now)


class Sector(Base):
    __tablename__ = "sectors"

    code = Column(String(20), primary_key=True)
    name = Column(String(50), nullable=False)
    type = Column(String(20), default="industry")
    updated_at = Column(DateTime, default=_now)


class SectorDaily(Base):
    __tablename__ = "sector_daily"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(20), nullable=False, index=True)
    trade_date = Column(String(8), nullable=False)
    close = Column(Float, default=0)
    pct_chg = Column(Float, default=0)
    volume = Column(Float, default=0)
    created_at = Column(DateTime, default=_now)


class StockPoolResult(Base):
    __tablename__ = "stock_pool_results"

    id = Column(Integer, primary_key=True, autoincrement=True)
    calc_date = Column(String(10), nullable=False)
    pool_type = Column(String(30), nullable=False)
    rank_in_pool = Column(Integer, default=0)
    ts_code = Column(String(20), nullable=False)
    stock_name = Column(String(50), default="")
    market_data_json = Column(Text, default="{}")
    inclusion_reason = Column(Text, default="")
    created_at = Column(DateTime, default=_now)


class DiagnosisReport(Base):
    __tablename__ = "diagnosis_reports"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ts_code = Column(String(20), nullable=False, index=True)
    calc_date = Column(String(10), nullable=False)
    tech_score = Column(Float, default=0)
    fundamental_score = Column(Float, nullable=True)
    composite_score = Column(Float, default=0)
    report_json = Column(Text, default="{}")
    created_at = Column(DateTime, default=_now)


class SectorAnalysisResult(Base):
    __tablename__ = "sector_analysis_results"

    id = Column(Integer, primary_key=True, autoincrement=True)
    calc_date = Column(String(10), nullable=False)
    sector_code = Column(String(20), nullable=False)
    heat_score = Column(Float, default=0)
    differentiation_index = Column(Float, default=0)
    linked_sectors = Column(Text, default="[]")
    created_at = Column(DateTime, default=_now)


class DailyReview(Base):
    __tablename__ = "daily_reviews"

    id = Column(Integer, primary_key=True, autoincrement=True)
    review_date = Column(String(10), nullable=False, unique=True)
    content_json = Column(Text, default="{}")
    created_at = Column(DateTime, default=_now)


class RiskListResult(Base):
    __tablename__ = "risk_list_results"

    id = Column(Integer, primary_key=True, autoincrement=True)
    calc_date = Column(String(10), nullable=False)
    risk_category = Column(String(30), nullable=False)
    ts_code = Column(String(20), nullable=False)
    stock_name = Column(String(50), default="")
    risk_detail = Column(Text, default="{}")
    created_at = Column(DateTime, default=_now)


class BacktestResult(Base):
    __tablename__ = "backtest_results"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False, index=True)
    strategy_name = Column(String(50), default="")
    strategy_params = Column(Text, default="{}")
    result_json = Column(Text, default="{}")
    created_at = Column(DateTime, default=_now)


class MembershipCode(Base):
    """会员激活码——管理员批量生成，用户兑换激活。"""
    __tablename__ = "membership_codes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(20), unique=True, nullable=False, index=True)
    code_type = Column(String(10), default="monthly")  # monthly / annual
    is_used = Column(Integer, default=0)
    used_by = Column(Integer, nullable=True)
    created_by = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=_now)
    used_at = Column(DateTime, nullable=True)


class UserFavorite(Base):
    __tablename__ = "user_favorites"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False, index=True)
    ts_code = Column(String(20), nullable=False)
    created_at = Column(DateTime, default=_now)


class UserAlertConfig(Base):
    __tablename__ = "user_alert_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False, index=True)
    ts_code = Column(String(20), nullable=False)
    alert_types = Column(Text, default="[]")  # JSON数组：技术面信号类型
    is_active = Column(Integer, default=1)
    created_at = Column(DateTime, default=_now)


class AlertNotification(Base):
    __tablename__ = "alert_notifications"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False, index=True)
    alert_config_id = Column(Integer, nullable=True)
    ts_code = Column(String(20), default="")
    stock_name = Column(String(50), default="")
    alert_type = Column(String(50), default="")
    content = Column(Text, default="")
    is_read = Column(Integer, default=0)
    created_at = Column(DateTime, default=_now)
