"""Claude Code 测试账号工具。

提供开发环境中 Claude Code 自动化测试所需的认证支持：
- create_test_user()：创建测试用户并生成永久 token
- remove_test_user()：清理测试用户数据

生产环境不启用。
"""

from __future__ import annotations

import asyncio
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

import bcrypt
from jose import jwt


async def create_test_user() -> dict:
    """创建测试用户，生成永久 JWT token（365 天），写入 .dev_token 文件。"""
    from app.core.database import async_session
    from app.core.settings import get_settings
    from app.models.orm.models import User
    from sqlalchemy import select

    settings = get_settings()
    username = "dev_test_user"
    phone = "00000000000"
    password = secrets.token_hex(12)
    password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    async with async_session() as session:
        result = await session.execute(select(User).where(User.phone == phone))
        existing = result.scalar_one_or_none()

        if existing:
            user_id = existing.id
        else:
            user = User(
                phone=phone,
                password_hash=password_hash,
                tier=99,
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)
            user_id = user.id

    # 生成 365 天有效的 token
    expire = datetime.now(timezone.utc) + timedelta(days=365)
    payload = {"sub": str(user_id), "tier": 99, "type": "access", "exp": expire}
    token = jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)

    # 写入项目根目录的 .dev_token
    project_root = Path(__file__).resolve().parent.parent.parent.parent
    token_path = project_root / ".dev_token"
    token_path.write_text(token + "\n")

    return {"user_id": user_id, "username": username, "phone": phone, "token": token, "token_file": str(token_path)}


async def remove_test_user() -> None:
    """删除测试用户及其访问日志，清理 .dev_token 文件。"""
    from app.core.database import async_session
    from app.models.orm.models import User
    from sqlalchemy import text

    async with async_session() as session:
        await session.execute(text("DELETE FROM access_logs WHERE user_id = (SELECT id FROM users WHERE phone = '00000000000')"))
        await session.execute(text("DELETE FROM user_favorites WHERE user_id = (SELECT id FROM users WHERE phone = '00000000000')"))
        await session.execute(text("DELETE FROM user_alert_configs WHERE user_id = (SELECT id FROM users WHERE phone = '00000000000')"))
        await session.execute(text("DELETE FROM alert_notifications WHERE user_id = (SELECT id FROM users WHERE phone = '00000000000')"))
        await session.execute(text("DELETE FROM users WHERE phone = '00000000000'"))
        await session.commit()

    project_root = Path(__file__).resolve().parent.parent.parent.parent
    token_path = project_root / ".dev_token"
    if token_path.exists():
        token_path.unlink()
