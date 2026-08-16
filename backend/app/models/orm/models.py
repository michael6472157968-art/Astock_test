"""SQLAlchemy ORM 模型——SQLite兼容，零外部依赖。

所有JSON字段使用Text存储（序列化为JSON字符串）。
"""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import Column, DateTime, Float, Integer, String, Text, Index
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
    tier = Column(Integer, default=0)  # 0=游客 1=注册 2=月会员 3=年会员 99=管理员
    credits = Column(Integer, default=0)  # 积分余额
    is_active = Column(Integer, default=1)  # 1=正常 0=禁用
    member_expire = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class Stock(Base):
    __tablename__ = "stocks"

    ts_code = Column(String(20), primary_key=True)
    symbol = Column(String(10), nullable=False)
    name = Column(String(50), nullable=False)
    industry = Column(String(50), default="", index=True)
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

    __table_args__ = (
        Index("ix_stock_daily_ts_code_trade_date_unique", "ts_code", "trade_date", unique=True),
        Index("ix_stock_daily_trade_date", "trade_date"),
    )
    __mapper_args__ = {"confirm_deleted_rows": False}


class Sector(Base):
    __tablename__ = "sectors"

    code = Column(String(20), primary_key=True)
    name = Column(String(50), nullable=False)
    type = Column(String(20), default="industry")
    updated_at = Column(DateTime, default=_now)


class DailyBasic(Base):
    """每日指标 PE/PB/市值/换手率——全量按日期同步。"""
    __tablename__ = "daily_basic"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ts_code = Column(String(20), nullable=False)
    trade_date = Column(String(8), nullable=False)
    pe = Column(Float, nullable=True)
    pe_ttm = Column(Float, nullable=True)
    pb = Column(Float, nullable=True)
    ps = Column(Float, nullable=True)
    ps_ttm = Column(Float, nullable=True)
    dv_ratio = Column(Float, nullable=True)
    dv_ttm = Column(Float, nullable=True)
    total_mv = Column(Float, nullable=True)
    circ_mv = Column(Float, nullable=True)
    turnover_rate = Column(Float, nullable=True)
    created_at = Column(DateTime, default=_now)

    __table_args__ = (
        Index("ix_daily_basic_td_code", "trade_date", "ts_code", unique=True),
    )


class MoneyflowHsgt(Base):
    """沪深港通北向资金流向——每日全量同步。"""
    __tablename__ = "moneyflow_hsgt"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trade_date = Column(String(8), nullable=False, unique=True)
    north_money = Column(Float, default=0)
    south_money = Column(Float, default=0)
    ggt_ss = Column(Float, default=0)
    ggt_sz = Column(Float, default=0)
    hgt = Column(Float, default=0)
    sgt = Column(Float, default=0)
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


class SectorAnalysisResult(Base):
    __tablename__ = "sector_analysis_results"

    id = Column(Integer, primary_key=True, autoincrement=True)
    calc_date = Column(String(10), nullable=False)
    sector_code = Column(String(20), nullable=False)
    heat_score = Column(Float, default=0)
    differentiation_index = Column(Float, default=0)
    linked_sectors = Column(Text, default="[]")
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


class LimitListRecord(Base):
    __tablename__ = "limit_list_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trade_date = Column(String(8), nullable=False, index=True)
    ts_code = Column(String(20), nullable=False)
    name = Column(String(50), default="")
    price = Column(Float, default=0)
    pct_chg = Column(Float, default=0)
    limit_type = Column(String(10), default="")  # U=涨停, D=跌停, Z=炸板
    open_num = Column(Integer, default=0)
    lu_desc = Column(String(200), default="")
    tag = Column(String(50), default="")
    status = Column(String(50), default="")  # N连板/换手板等
    created_at = Column(DateTime, default=_now)

    __table_args__ = (
        Index("ix_limit_list_td_code", "trade_date", "ts_code", unique=True),
    )


class MarginRecord(Base):
    __tablename__ = "margin_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trade_date = Column(String(8), nullable=False, index=True)
    ts_code = Column(String(20), nullable=False)
    name = Column(String(50), default="")
    rzye = Column(Float, nullable=True)   # 融资余额(元)
    rqye = Column(Float, nullable=True)   # 融券余额(元)
    rzmre = Column(Float, nullable=True)  # 融资买入额
    rqyl = Column(Float, nullable=True)   # 融券余量
    rzche = Column(Float, nullable=True)  # 融资偿还额
    rqchl = Column(Float, nullable=True)  # 融券偿还量
    rqmcl = Column(Float, nullable=True)  # 融券卖出量
    rzrqye = Column(Float, nullable=True) # 融资融券余额
    created_at = Column(DateTime, default=_now)

    __table_args__ = (
        Index("ix_margin_td_code", "trade_date", "ts_code", unique=True),
    )


class MoneyflowRecord(Base):
    """个股资金流向——主力/超大单/大单/中单/小单净流入。2000积分解锁。
    按交易日全市场批量同步，金额单位：万元。"""
    __tablename__ = "moneyflow_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ts_code = Column(String(20), nullable=False)
    trade_date = Column(String(8), nullable=False)
    net_mf_amount = Column(Float, nullable=True)  # 主力净流入额(万元)
    net_mf_vol = Column(Float, nullable=True)     # 主力净流入量(手)
    buy_elg_amount = Column(Float, nullable=True) # 超大单买入额(万元)
    sell_elg_amount = Column(Float, nullable=True)
    buy_lg_amount = Column(Float, nullable=True)  # 大单买入额(万元)
    sell_lg_amount = Column(Float, nullable=True)
    buy_md_amount = Column(Float, nullable=True)  # 中单买入额(万元)
    sell_md_amount = Column(Float, nullable=True)
    buy_sm_amount = Column(Float, nullable=True)  # 小单买入额(万元)
    sell_sm_amount = Column(Float, nullable=True)
    created_at = Column(DateTime, default=_now)

    __table_args__ = (
        Index("ix_moneyflow_td_code", "trade_date", "ts_code", unique=True),
    )


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


class UserFavoriteGroup(Base):
    __tablename__ = "user_favorite_groups"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False, index=True)
    name = Column(String(30), nullable=False)
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime, default=_now)

    __table_args__ = (
        Index("ix_fav_groups_user_order", "user_id", "sort_order"),
    )


class UserFavorite(Base):
    __tablename__ = "user_favorites"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False, index=True)
    ts_code = Column(String(20), nullable=False)
    stock_name = Column(String(50), default="")
    group_id = Column(Integer, nullable=True, default=None)
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime, default=_now)

    __table_args__ = (
        Index("ix_user_favorites_user_stock_unique", "user_id", "ts_code", unique=True),
        Index("ix_user_favorites_user_order", "user_id", "sort_order"),
        Index("ix_user_favorites_group", "user_id", "group_id"),
    )


class UserAlertConfig(Base):
    __tablename__ = "user_alert_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False, index=True)
    ts_code = Column(String(20), nullable=False)
    alert_types = Column(Text, default="[]")
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


class AccessLog(Base):
    __tablename__ = "access_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=True, index=True)
    endpoint = Column(String(200), nullable=False)
    ip_address = Column(String(45), default="")
    user_agent = Column(Text, default="")
    created_at = Column(DateTime, default=_now)


class CreditLedger(Base):
    __tablename__ = "credit_ledger"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False, index=True)
    amount = Column(Integer, nullable=False)  # 正=获得, 负=消耗
    type = Column(String(20), nullable=False)  # register/checkin/activation/guess/diagnosis/ai_analysis/admin
    ref_id = Column(String(64), default="")    # 关联业务ID(stock_code/guess_date)
    balance_after = Column(Integer, nullable=False)
    note = Column(String(200), default="")
    created_at = Column(DateTime, default=_now)


class CheckinRecord(Base):
    __tablename__ = "checkin_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False, index=True)
    date = Column(String(10), nullable=False)  # YYYY-MM-DD
    streak = Column(Integer, default=1)  # 连续签到天数
    credits = Column(Integer, default=0)  # 本次获得积分
    created_at = Column(DateTime, default=_now)

    __table_args__ = (
        Index("ix_checkin_user_date_unique", "user_id", "date", unique=True),
    )


class MarketGuess(Base):
    __tablename__ = "market_guesses"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False, index=True)
    guess_date = Column(String(10), nullable=False)  # 竞猜目标交易日
    direction = Column(String(5), nullable=False)  # up / down
    score_change = Column(Integer, nullable=True)  # null=待结算, 值=结算积分 (±5/±1)
    created_at = Column(DateTime, default=_now)

    __table_args__ = (
        Index("ix_guess_user_date_unique", "user_id", "guess_date", unique=True),
    )
