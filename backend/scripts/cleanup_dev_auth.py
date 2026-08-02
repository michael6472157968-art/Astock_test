"""清理 Claude Code 开发测试账号。

删除测试用户及其所有关联数据，删除 .dev_token 文件。

用法:
    cd backend && python scripts/cleanup_dev_auth.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.utils.dev_helper import remove_test_user


async def main():
    await remove_test_user()
    print("测试账号已清理——用户、访问日志、自选股、预警、通知均已删除。")
    print(".dev_token 文件已删除。")


if __name__ == "__main__":
    asyncio.run(main())
