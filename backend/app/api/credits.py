"""积分 API——签到、流水、余额查询。"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func, desc

from app.core.database import async_session
from app.core.settings import get_settings
from app.middleware.auth_middleware import require_auth
from app.models.orm.models import CheckinRecord, CreditLedger, MarketGuess, User
from app.models.schemas.common import APIResponse

router = APIRouter(prefix="/api/v1/credits", tags=["积分"])
logger = logging.getLogger("credits")
SIGNIN_REWARD_FREE = 3
SIGNIN_REWARD_VIP = 5
SIGNIN_STREAK_BONUS = 5  # 连续7天额外奖励


_CREDIT_TYPE_LABEL = {
    "register": "注册赠送",
    "checkin": "每日签到",
    "activation": "会员激活",
    "guess": "大盘竞猜",
    "diagnosis": "诊股消耗",
    "ai_analysis": "AI分析",
    "admin": "管理员调整",
}


@router.get("/balance")
async def get_balance(user: dict = Depends(require_auth)):
    """当前用户积分余额。"""
    user_id = user["user_id"]
    async with async_session() as session:
        result = await session.execute(select(User.credits).where(User.id == user_id))
        credits = result.scalar()
        if credits is None:
            raise HTTPException(status_code=404, detail="用户不存在")
    return APIResponse(data={"credits": credits}, timestamp=int(time.time()))


@router.get("/ledger")
async def get_ledger(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user: dict = Depends(require_auth),
):
    """积分流水（分页），按时间倒序。"""
    user_id = user["user_id"]
    async with async_session() as session:
        total_q = select(func.count()).select_from(CreditLedger).where(
            CreditLedger.user_id == user_id
        )
        total = (await session.execute(total_q)).scalar() or 0

        items_q = (
            select(CreditLedger)
            .where(CreditLedger.user_id == user_id)
            .order_by(desc(CreditLedger.created_at))
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        rows = (await session.execute(items_q)).scalars().all()

        items = []
        for r in rows:
            items.append({
                "id": r.id,
                "amount": r.amount,
                "type": r.type,
                "type_label": _CREDIT_TYPE_LABEL.get(r.type, r.type),
                "ref_id": r.ref_id,
                "balance_after": r.balance_after,
                "note": r.note,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            })

    return APIResponse(
        data={"total": total, "page": page, "page_size": page_size, "items": items},
        timestamp=int(time.time()),
    )


@router.post("/checkin")
async def do_checkin(user: dict = Depends(require_auth)):
    """每日签到——免费+3/VIP+5，连续7天额外+5。"""
    user_id, tier = user["user_id"], user["tier"]
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    async with async_session() as session:
        # 防重复
        existing = await session.execute(
            select(CheckinRecord).where(
                CheckinRecord.user_id == user_id, CheckinRecord.date == today
            )
        )
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="今日已签到")

        # 连续天数
        prev = await session.execute(
            select(CheckinRecord)
            .where(CheckinRecord.user_id == user_id, CheckinRecord.date == yesterday)
        )
        prev_record = prev.scalar_one_or_none()
        streak = (prev_record.streak if prev_record else 0) + 1

        # 计算积分
        base = SIGNIN_REWARD_VIP if tier >= 2 else SIGNIN_REWARD_FREE
        bonus = SIGNIN_STREAK_BONUS if streak == 7 else 0
        total_credits = base + bonus

        # 获取用户
        u_result = await session.execute(select(User).where(User.id == user_id))
        u = u_result.scalar_one_or_none()
        if not u:
            raise HTTPException(status_code=404, detail="用户不存在")

        # 更新积分
        u.credits = u.credits + total_credits

        # 签到记录
        record = CheckinRecord(
            user_id=user_id, date=today, streak=streak, credits=total_credits
        )
        session.add(record)

        # 积分流水
        note = f"签到 +{base}"
        if bonus:
            note += f" (连续{streak}天额外+{bonus})"
        session.add(CreditLedger(
            user_id=user_id,
            amount=total_credits,
            type="checkin",
            ref_id=today,
            balance_after=u.credits,
            note=note,
        ))

        await session.commit()

    return APIResponse(
        data={
            "credits": total_credits,
            "balance": u.credits,
            "streak": streak,
            "base": base,
            "bonus": bonus,
        },
        timestamp=int(time.time()),
    )


@router.get("/checkin/status")
async def checkin_status(user: dict = Depends(require_auth)):
    """签到状态：今天是否已签到、连续天数。"""
    user_id = user["user_id"]
    today = date.today().isoformat()

    async with async_session() as session:
        today_record = await session.execute(
            select(CheckinRecord).where(
                CheckinRecord.user_id == user_id, CheckinRecord.date == today
            )
        )
        today_r = today_record.scalar_one_or_none()

        # 最近30天签到日期（用于日历展示）
        month_ago = (date.today() - timedelta(days=29)).isoformat()
        month_records = await session.execute(
            select(CheckinRecord.date)
            .where(CheckinRecord.user_id == user_id, CheckinRecord.date >= month_ago)
            .order_by(CheckinRecord.date)
        )
        dates = [r[0] for r in month_records.all()]

    return APIResponse(
        data={
            "checked_in_today": today_r is not None,
            "streak": today_r.streak if today_r else 0,
            "today_credits": today_r.credits if today_r else 0,
            "checkin_dates": dates,
        },
        timestamp=int(time.time()),
    )


# ── 竞猜大盘涨跌 ──

_settings = get_settings()
GUESS_REWARD_CORRECT = 5
GUESS_REWARD_PARTICIPATE = 1
GUESS_DEADLINE_HOUR = 9  # 竞猜目标日当天9:00截止提交


async def _resolve_guess_target(now: datetime) -> tuple[str, str]:
    """返回下一个交易日作为竞猜目标。

    返回 (target_date_iso, flag): flag="open"|"none"
    """
    from app.utils.trading_calendar import get_next_trade_date

    tomorrow = (now.date() + timedelta(days=1)).strftime("%Y%m%d")
    try:
        next_td = await get_next_trade_date(tomorrow)
        d = datetime.strptime(next_td, "%Y%m%d")
        return d.date().isoformat(), "open"
    except RuntimeError:
        return "", "none"


@router.post("/guess")
async def submit_guess(direction: str = Query(..., pattern="^(up|down)$"), user: dict = Depends(require_auth)):
    """竞猜大盘涨跌——猜下一交易日涨跌，目标日当天9:00截止，每人每天一次。"""
    user_id = user["user_id"]
    now = datetime.now()
    guess_date, flag = await _resolve_guess_target(now)

    if flag == "none":
        raise HTTPException(status_code=400, detail="暂无交易日可竞猜")

    async with async_session() as session:
        existing = await session.execute(
            select(MarketGuess).where(
                MarketGuess.user_id == user_id, MarketGuess.guess_date == guess_date
            )
        )
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="该交易日已竞猜")

        guess = MarketGuess(
            user_id=user_id,
            guess_date=guess_date,
            direction=direction,
        )
        session.add(guess)
        await session.commit()

    return APIResponse(
        data={"guess_date": guess_date, "direction": direction, "message": "竞猜已提交，收盘后结算"},
        timestamp=int(time.time()),
    )


@router.get("/guess/status")
async def guess_status(user: dict = Depends(require_auth)):
    """当前竞猜状态：目标日、是否已猜、方向、结果。"""
    user_id = user["user_id"]
    now = datetime.now()
    guess_date, flag = await _resolve_guess_target(now)

    if flag == "none":
        return APIResponse(
            data={"has_guessed": False, "is_trade_day": False, "target_date": None},
            timestamp=int(time.time()),
        )

    async with async_session() as session:
        result = await session.execute(
            select(MarketGuess).where(
                MarketGuess.user_id == user_id, MarketGuess.guess_date == guess_date
            )
        )
        guess = result.scalar_one_or_none()

    if not guess:
        return APIResponse(
            data={
                "has_guessed": False,
                "is_trade_day": True,
                "target_date": guess_date,
            },
            timestamp=int(time.time()),
        )

    return APIResponse(
        data={
            "has_guessed": True,
            "target_date": guess_date,
            "direction": guess.direction,
            "score_change": guess.score_change,
            "settled": guess.score_change is not None,
        },
        timestamp=int(time.time()),
    )


# ── 竞猜结算（由 scheduler 在收盘后调用）──


async def settle_market_guesses():
    """结算竞猜——取最近一个交易日收盘数据，结算该日所有未结算竞猜。"""
    from app.utils.trading_calendar import get_latest_trade_date

    latest_td = await get_latest_trade_date()
    guess_date = datetime.strptime(latest_td, "%Y%m%d").date().isoformat()
    end = latest_td
    start = (datetime.strptime(latest_td, "%Y%m%d") - timedelta(days=5)).strftime("%Y%m%d")

    # 获取大盘涨跌方向（用上证指数）
    try:
        from app.services.tushare_client import get_pro
        pro = get_pro()
        df = pro.index_daily(ts_code="000001.SH", start_date=start, end_date=end)
        if df is None or df.empty:
            logger.warning("Guess settlement: no index data")
            return
        today_row = df[df["trade_date"] == end]
        if today_row.empty:
            today_row = df.head(1)
        pct = float(today_row.iloc[0]["pct_chg"])
        actual_direction = "up" if pct > 0 else "down" if pct < 0 else "flat"
    except Exception as e:
        logger.exception(f"Guess settlement: failed to get index data: {e}")
        return

    async with async_session() as session:
        # 获取今日所有未结算竞猜
        result = await session.execute(
            select(MarketGuess).where(
                MarketGuess.guess_date == guess_date, MarketGuess.score_change.is_(None)
            )
        )
        guesses = result.scalars().all()

        logger.info(f"Settling {len(guesses)} market guesses, actual={actual_direction}")

        for g in guesses:
            # If market is flat (rare), everyone gets participate points only
            if actual_direction == "flat":
                score = GUESS_REWARD_PARTICIPATE
            elif g.direction == actual_direction:
                score = GUESS_REWARD_CORRECT
            else:
                score = GUESS_REWARD_PARTICIPATE

            g.score_change = score

            # Update user credits
            u_result = await session.execute(select(User).where(User.id == g.user_id))
            u = u_result.scalar_one_or_none()
            if u:
                u.credits = (u.credits or 0) + score
                session.add(CreditLedger(
                    user_id=g.user_id,
                    amount=score,
                    type="guess",
                    ref_id=guess_date,
                    balance_after=u.credits,
                    note=f"竞猜{'猜对+5' if score == GUESS_REWARD_CORRECT else '参与+1'} ({guess_date})",
                ))

        await session.commit()

    logger.info(f"Guess settlement complete: {len(guesses)} guesses settled")
