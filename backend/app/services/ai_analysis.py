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


SYSTEM_PROMPT = """你是一个A股技术指标解读助手。你的职责是客观解读提供的量化指标数据，帮助用户理解当前的技术面状态。

严格约束：
- 只解读提供的指标，不编造数据之外的信息
- 不给出任何买卖建议、操作建议、仓位建议
- 不使用"建议买入""建议卖出""可参与""宜观望"等投资决策语言
- 不做方向性预测（涨/跌/突破/回调），只陈述当前指标状态

分析要求：
1. 指标解读：逐项说明 MACD/RSI/KDJ/布林带/均线的当前数值含义
2. 多空因素：基于指标列出偏多信号和偏空信号各2-4条（使用"偏多信号""偏空信号"替代"看多""看空"）
3. 风险提示：列出至少2条从指标中反映出的需要关注的风险

输出格式（markdown）：
## 指标解读
## 多空因素
### 偏多信号
### 偏空信号
## 风险提示

每节末尾必须包含声明："以上内容基于技术指标自动生成，不构成投资建议。投资决策请结合基本面和个人风险承受能力独立判断。"
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
