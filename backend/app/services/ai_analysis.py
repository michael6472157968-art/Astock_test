"""AI 分析服务——DeepSeek 客户端 + prompt 模板。

通过 OpenAI SDK 兼容接口调用 DeepSeek-V4-Flash。
"""

from __future__ import annotations

import json
import logging
from datetime import date

from openai import AsyncOpenAI

from app.core.cache import cache_get, cache_set
from app.core.settings import get_settings

logger = logging.getLogger("ai_analysis")
_settings = get_settings()


def _get_client() -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=_settings.deepseek_api_key,
        base_url=_settings.deepseek_base_url,
    )


SYSTEM_PROMPT = """你是一个专业的A股短线技术分析师。严格基于提供的技术指标数据进行分析，不要编造数据之外的信息。

分析要求：
1. 技术指标解读：逐项解读MACD/RSI/KDJ/布林带/均线的当前状态和含义
2. 多空因素：列出看多因素和看空因素各2-4条
3. T+3~T+7操作建议：基于量化评分和风险等级给出具体的短线操作建议
4. 风险提示：列出至少2条需要注意的风险

输出格式（markdown）：
## 技术指标解读
## 多空因素
### 看多因素
### 看空因素
## 短线建议（T+3~T+7）
## 风险提示

注意：分析中必须明确声明"以上分析基于技术指标自动生成，不构成投资建议"。
"""


async def analyze_stock(
    stock_code: str,
    stock_name: str,
    indicators_json: dict,
) -> str:
    """调用 DeepSeek 生成 AI 分析文本。

    先检查同股同日缓存，命中则直接返回。
    """
    today = date.today().isoformat()
    cache_key = f"ai:{stock_code}:{today}"
    cached = await cache_get(cache_key)
    if cached:
        logger.info(f"AI cache hit: {cache_key}")
        return cached

    # 序列化指标数据为紧凑 JSON
    indicators_str = json.dumps(indicators_json, ensure_ascii=False, indent=2)

    user_prompt = f"""请分析以下A股股票的技术面：

股票代码：{stock_code}
股票名称：{stock_name}
分析日期：{today}

技术指标数据：
```json
{indicators_str}
```

请按照系统指令中的格式输出分析报告。"""

    client = _get_client()
    try:
        response = await client.chat.completions.create(
            model=_settings.deepseek_model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
            max_tokens=1200,
        )
        text = response.choices[0].message.content or ""
        logger.info(f"AI analysis generated for {stock_code}, tokens: {response.usage}")

        # 缓存到当日
        await cache_set(cache_key, text, ttl=86400)
        return text

    except Exception as e:
        logger.exception(f"DeepSeek API error for {stock_code}: {e}")
        raise
