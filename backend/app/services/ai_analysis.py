"""AI 分析服务——DeepSeek 客户端 + prompt 模板。

通过 OpenAI SDK 兼容接口调用 DeepSeek-V4-Flash。

两种模式：
1. analyze_stock        单 Agent 指标解读（原版，客观陈述，不给方向）
2. analyze_stock_debate 多空辩论 + 结构化评级（原型：Bull/Bear 两研究员辩论 + 裁决 Agent）

辩论模式借鉴 TradingAgents / ai-hedge-fund 的"多空辩论 + 可追溯裁决"思想，
但不引入 LangGraph 等重依赖——用普通顺序调用实现，数据源仍走 Tushare。
"""

from __future__ import annotations

import json
import logging
import re
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


async def _call_llm(
    system: str,
    user: str,
    max_tokens: int = 1200,
    temperature: float = 0.3,
) -> str:
    """统一 LLM 调用封装。返回纯文本（失败向上抛，由调用方处理退分）。"""
    client = _get_client()
    response = await client.chat.completions.create(
        model=_settings.deepseek_model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return response.choices[0].message.content or ""


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
    """调用 DeepSeek 生成 AI 分析文本（单 Agent 模式，原版行为）。

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

    try:
        text = await _call_llm(SYSTEM_PROMPT, user_prompt)
        logger.info(f"AI analysis generated for {stock_code}")

        # 缓存到当日
        await cache_set(cache_key, text, ttl=86400)
        return text

    except Exception as e:
        logger.exception(f"DeepSeek API error for {stock_code}: {e}")
        raise


# ════════════════════════ 多空辩论模式（原型） ════════════════════════

DISCLAIMER = "以上内容基于技术指标自动生成，不构成投资建议。投资决策请结合基本面和个人风险承受能力独立判断。"

# 已验证的统计先验（项目目前唯一通过前向验证的 alpha），作为参考背景注入。
# 明确标注"未经严格验证"——因为它只基于 2 年日线、无分时数据。
STAT_REFERENCE = (
    "量比>2 且放量大涨 → 5日短线看跌 +10.7pp（大盘股 +14.4pp），"
    "这是项目目前唯一通过前向验证的 alpha；五眼共振本身无预测力。"
    "注：该结论仅基于约2年日线数据、无分时数据，稳定性存疑。"
)


def _render_reference(extra_evidence: dict | None) -> str:
    """把规则引擎结论渲染成【参考背景】段落（明确标注未验证，仅供独立判断）。"""
    if not extra_evidence:
        return ""

    lines = ["\n\n【参考背景（来自项目规则引擎，未经严格验证，仅供独立判断，勿当作事实）】"]
    if extra_evidence.get("five_eye_summary"):
        lines.append(f"- 五眼共识：{extra_evidence['five_eye_summary']}")
    ra = extra_evidence.get("retreat_alert")
    if isinstance(ra, dict):
        ra_msg = ra.get("message") or ra.get("level") or ""
        if ra_msg:
            lines.append(f"- 量比退潮预警：{ra_msg}")
    lines.append(f"- 统计先验：{STAT_REFERENCE}")
    return "\n".join(lines)


BULL_PROMPT = """你是一位A股多头研究员。你的任务是基于给定的技术指标数据，尽可能客观地找出【偏多】的论据。

严格约束：
- 主要基于提供的指标数据立论，不编造数据之外的信息；若输入含【参考背景】，仅作参考（未经严格验证），立论仍以指标为准
- 每条论据必须引用具体指标数值（如"RSI=35 处于超卖区""MACD金叉""温和放量突破"）
- 不使用"建议买入""看涨""必涨"等投资决策或预测语言，只用"偏多信号"陈述
- 只输出论据，不要任何开头语、结尾总结或免责声明

输出格式（纯文本，严格按此，每条一行）：
1. <论据，含具体指标数值>
2. <论据，含具体指标数值>
...（3-5 条，按说服力从强到弱排序）"""

BEAR_PROMPT = """你是一位A股空头研究员。你的任务是基于给定的技术指标数据，尽可能客观地找出【偏空】的论据。

严格约束：
- 主要基于提供的指标数据立论，不编造数据之外的信息；若输入含【参考背景】，仅作参考（未经严格验证），立论仍以指标为准
- 每条论据必须引用具体指标数值（如"RSI=78 处于超买区""MACD死叉""缩量滞涨"）
- 不使用"建议卖出""看跌""必跌"等投资决策或预测语言，只用"偏空信号"陈述
- 只输出论据，不要任何开头语、结尾总结或免责声明

输出格式（纯文本，严格按此，每条一行）：
1. <论据，含具体指标数值>
2. <论据，含具体指标数值>
...（3-5 条，按说服力从强到弱排序）"""

JUDGE_PROMPT = """你是一位A股技术面裁决员。下面给出多头研究员与空头研究员对同一只股票的论据，请综合判断当前技术面方向倾向。

严格约束：
- 只基于双方论据中出现的指标判断，不引入论据之外的新数据
- direction 是"多空力量对比后的研究性方向倾向"，不是操作建议
- 只输出 JSON，不要 markdown 代码块（```）、不要任何 JSON 之外的文字

输出必须是纯 JSON，格式如下：
{
  "direction": "bullish 或 bearish 或 neutral",
  "confidence": 1,
  "thesis": "一句话核心结论",
  "bull_evidence": ["多头较强论据，最多3条"],
  "bear_evidence": ["空头较强论据，最多3条"],
  "risk_flags": ["需要关注的风险点，至少1条"]
}

字段说明：
- direction：多空力量对比后的方向倾向（bullish=偏多 / bearish=偏空 / neutral=中性）
- confidence：1-5 整数，5=非常确定
- thesis：一句话概括当前技术面核心状态
- bull_evidence / bear_evidence：从双方论据中筛选出的较强论据（保留具体指标数值）
- risk_flags：即使偏多也必须列出的风险点"""


def _extract_json(text: str) -> dict:
    """从 LLM 输出中稳健提取 JSON（容错 markdown 代码块包裹）。"""
    # 去掉 ```json ... ``` 包裹
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start : end + 1]
    return json.loads(text)


async def analyze_stock_debate(
    stock_code: str,
    stock_name: str,
    indicators_json: dict,
    extra_evidence: dict | None = None,
    use_cache: bool = True,
) -> dict:
    """多空辩论 + 结构化评级（原型）。

    流程：
    1. Bull 研究员：从指标中找偏多论据（1 次 LLM 调用）
    2. Bear 研究员：从指标中找偏空论据（1 次 LLM 调用）
    3. 裁决 Agent：综合多空，输出结构化评级（1 次 LLM 调用）

    extra_evidence: 可选，规则引擎（五眼共识/量比退潮预警）结论，作为【参考背景】
    注入证据，明确标注"未经验证、仅供独立判断"。

    返回 {"rating": {...}, "report": str}。rating 为结构化 JSON（可入库做 alpha 检验），
    report 为可直接展示的 markdown。

    同股同日缓存命中时直接返回缓存。
    """
    today = date.today().isoformat()
    cache_key = f"ai_debate:{stock_code}:{today}"
    if use_cache:
        cached = await cache_get(cache_key)
        if cached:
            logger.info(f"AI debate cache hit: {cache_key}")
            return cached

    indicators_str = json.dumps(indicators_json, ensure_ascii=False, indent=2)
    reference_block = _render_reference(extra_evidence)
    evidence_prompt = f"""股票代码：{stock_code}
股票名称：{stock_name}
分析日期：{today}

技术指标数据：
```json
{indicators_str}
```{reference_block}"""

    try:
        # 1. 多空研究员并行（顺序调用，避免 LLM 并发限流）
        bull_text = await _call_llm(BULL_PROMPT, evidence_prompt, max_tokens=600)
        bear_text = await _call_llm(BEAR_PROMPT, evidence_prompt, max_tokens=600)

        # 2. 裁决 Agent
        judge_user = f"""股票代码：{stock_code}
股票名称：{stock_name}

【多头研究员论据】
{bull_text}

【空头研究员论据】
{bear_text}

请按系统指令输出纯 JSON 评级。"""
        judge_text = await _call_llm(JUDGE_PROMPT, judge_user, max_tokens=800, temperature=0.2)

        # 3. 解析结构化评级（解析失败则降级为中性）
        try:
            rating = _extract_json(judge_text)
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning(f"Judge JSON parse failed for {stock_code}, fallback neutral: {e}")
            rating = {
                "direction": "neutral",
                "confidence": 1,
                "thesis": "裁决 Agent 输出无法解析，已降级为中性。",
                "bull_evidence": [],
                "bear_evidence": [],
                "risk_flags": ["AI 输出异常，请稍后重试"],
            }

        # 补齐缺省字段，防 KeyError
        rating.setdefault("direction", "neutral")
        rating.setdefault("confidence", 1)
        rating.setdefault("thesis", "")
        rating.setdefault("bull_evidence", [])
        rating.setdefault("bear_evidence", [])
        rating.setdefault("risk_flags", [])

        # 4. 组装 markdown 报告
        report = _build_debate_report(stock_code, stock_name, rating, bull_text, bear_text)

        result = {"rating": rating, "report": report}
        if use_cache:
            await cache_set(cache_key, result, ttl=86400)
        logger.info(f"AI debate generated for {stock_code}: {rating['direction']} (conf={rating['confidence']})")
        return result

    except Exception as e:
        logger.exception(f"DeepSeek debate API error for {stock_code}: {e}")
        raise


def _direction_label(direction: str) -> str:
    return {"bullish": "偏多", "bearish": "偏空", "neutral": "中性"}.get(direction, "中性")


def _build_debate_report(
    stock_code: str,
    stock_name: str,
    rating: dict,
    bull_text: str,
    bear_text: str,
) -> str:
    """把多空论据 + 结构化评级组装成 markdown 报告。"""
    direction = rating.get("direction", "neutral")
    confidence = rating.get("confidence", 1)
    thesis = rating.get("thesis", "")
    bull_ev = rating.get("bull_evidence", []) or []
    bear_ev = rating.get("bear_evidence", []) or []
    risk_flags = rating.get("risk_flags", []) or []

    lines: list[str] = []
    lines.append(f"## {stock_name}({stock_code}) 多空辩论")

    lines.append("\n### 多头研究员观点")
    for line in (bull_text or "").strip().splitlines():
        line = line.strip()
        if line:
            lines.append(f"- {line}")

    lines.append("\n### 空头研究员观点")
    for line in (bear_text or "").strip().splitlines():
        line = line.strip()
        if line:
            lines.append(f"- {line}")

    lines.append("\n## 裁决")
    lines.append(f"**方向倾向**：{_direction_label(direction)}（置信度 {confidence}/5）")
    if thesis:
        lines.append(f"\n**核心结论**：{thesis}")

    if bull_ev:
        lines.append("\n**偏多证据**：")
        for ev in bull_ev:
            lines.append(f"- {ev}")
    if bear_ev:
        lines.append("\n**偏空证据**：")
        for ev in bear_ev:
            lines.append(f"- {ev}")
    if risk_flags:
        lines.append("\n**风险提示**：")
        for r in risk_flags:
            lines.append(f"- {r}")

    lines.append(f"\n> {DISCLAIMER}")
    return "\n".join(lines)
