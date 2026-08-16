"""发送两日工作报告到飞书（用户本人 open_id）。

凭证从 ~/.cc-connect/config.toml 读取，不硬编码敏感信息。
用法: cd backend && PYTHONIOENCODING=utf-8 python send_report_to_feishu.py
"""
import json
import tomllib
import urllib.request
from pathlib import Path


def _load_creds() -> tuple[str, str, str]:
    cfg_path = Path.home() / ".cc-connect" / "config.toml"
    cfg = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    project = cfg["projects"][0]
    opts = project["platforms"][0]["options"]
    return opts["app_id"], opts["app_secret"], project["admin_from"]


APP_ID, APP_SECRET, OPEN_ID = _load_creds()


def get_token() -> str:
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    data = json.dumps({"app_id": APP_ID, "app_secret": APP_SECRET}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    resp = urllib.request.urlopen(req, timeout=15)
    return json.loads(resp.read())["tenant_access_token"]


def send_text(token: str, text: str) -> dict:
    url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id"
    data = json.dumps({
        "receive_id": OPEN_ID,
        "msg_type": "text",
        "content": json.dumps({"text": text}),
    }).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    })
    resp = urllib.request.urlopen(req, timeout=15)
    return json.loads(resp.read())


REPORT = """📊 Astock 两日工作报告（08-14 ~ 08-15）

━━━━━━ 第一天（08-14）：找到 A 股 alpha 到底在哪 ━━━━━━

核心突破：因子不是无效，是维度不对。
时间序列（单股买卖点）无 alpha，横截面（全市场排序）alpha 立刻出来。

✅ 两条 alpha 腿：
· 择时：放量见顶（量比>2且涨→看跌 +13.9pp，唯一可复制短线异象）
· 选股：反转 return_21d + 量价背离 a101_16

❌ 证伪：LLM辩论、技术指标(时序+横截面57个)、分钟因子、资金流净额、市场状态开关、滚动IC加权

━━━━━━ 第二天（08-15）：补全基本面 + 落地网站 ━━━━━━

✅ 财务因子挖掘（基本面第三条腿）：
· 价值 BP/SP/DP（IC +0.061/+0.047/+0.049）
· 现金流 cfps_yoy/ocf_yoy（t=16/15，核心 alpha）
· 成长 扣非净利同比（t=8）
· 关键：市场奖励"经营现金流"，不奖励"账面盈利"（ROE/ROA证伪）

✅ 反转 42d 升级：
· A股全程反转无动量
· rev_42d 最优（净+21% 夏普1.15，2021抱团年从-8.68%转正+19.28%）
· 量价腿 rev_42d+a101_16 = 净+22.34% 夏普1.53 正年份11/11

✅ Stockwin 因子库（策略实验室→因子库）：
· 9有效因子替换16已证伪技术指标
· 配置驱动（改JSON即更新）
· 4功能：展示+诊断+匹配+选股

✅ 数据全量接入（8000积分+2000元）：
· 筹码分布/龙虎榜/概念板块/券商金股/解禁/增减持/业绩快报（10表）
· F5-F8 财务字段补齐

━━━━━━ 最终成果 ━━━━━━

三条信息源闭环：
价格→反转42d、量→量价背离、基本面→价值/现金流/成长
全合成 11/11 正年份，量价腿夏普 1.53

━━━━━━ 明天待办 ━━━━━━

1. 后台 fina_indicator 拉取完成（F7/F8诊断生效）
2. 网站内容大改（方向待定）

（完整文档：docs/work-report-2026-08-15.md）"""


if __name__ == "__main__":
    token = get_token()
    r = send_text(token, REPORT)
    print("发送结果:", r.get("code"), r.get("msg"))
