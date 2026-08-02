"""创建 Claude Code 开发测试账号。

运行此脚本以获取一个永久有效的认证 token，用于 Claude Code 自动化测试。
token 将写入项目根目录的 .dev_token 文件。

用法:
    cd backend && python scripts/setup_dev_auth.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# 将 backend 目录加入 Python path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.utils.dev_helper import create_test_user


async def main():
    result = await create_test_user()
    print(f"测试账号已创建:")
    print(f"  user_id: {result['user_id']}")
    print(f"  phone:   {result['phone']}")
    print(f"  tier:    99 (admin)")
    print(f"  token:   {result['token_file']}")
    print()
    print("Claude Code 使用方式:")
    print(f"  读取 .dev_token 文件内容，请求时带 Authorization: Bearer <token>")
    print()
    print(f"部署前清理: python scripts/cleanup_dev_auth.py")


if __name__ == "__main__":
    asyncio.run(main())
