#!/usr/bin/env python3
"""A股每日复盘自动生成脚本 - 数据来源：东方财富（通过 akshare）"""

import os
import sys
from datetime import date
from pathlib import Path

try:
    import chinese_calendar
    HAS_CHINESE_CALENDAR = True
except ImportError:
    HAS_CHINESE_CALENDAR = False

import akshare as ak
from openai import OpenAI


def is_trading_day(d: date) -> bool:
    if d.weekday() >= 5:
        return False
    if HAS_CHINESE_CALENDAR:
        try:
            return chinese_calendar.is_workday(d)
        except Exception:
            pass
    return True


def safe_fetch(func, *args, default=None, **kwargs):
    try:
        return func(*args, **kwargs)
    except Exception as e:
        print(f"[警告] {func.__name__} 失败: {e}")
        return default


def get_indices() -> dict:
    df = ak.stock_zh_index_spot_em()
    targets = {
        '000001': '上证指数',
        '399001': '深证成指',
        '399006': '创业板指',
        '000688': '科创50',
    }
    result = {}
    for code, name in targets.items():
        rows = df[df['代码'] == code]
        if not rows.empty:
            r = rows.iloc[0]
            result[name] = {
                'price': float(r['最新价']),
                'change_pct': float(r['涨跌幅']),
                'amount_wan': float(r.get('成交额', 0)),
            }
    return result


def get_market_activity() -> dict:
    df = safe_fetch(ak.stock_market_activity_legu, default=None)
    if df is None or df.empty:
        return {}
    try:
        data = {}
        for _, row in df.iterrows():
            data[str(row.iloc[0])] = row.iloc[1]
        return {
            'up': int(float(data.get('上涨', 0))),
            'down': int(float(data.get('下跌', 0))),
            'flat': int(float(data.get('平盘', 0))),
            'limit_up': int(float(data.get('涨停', 0))),
            'limit_down': int(float(data.get('跌停', 0))),
            'total_amount': data.get('两市成交额', data.get('总成交额', None)),
        }
    except Exception as e:
        print(f"[警告] 解析市场活跃度失败: {e}")
        return {}


def get_sector_flow() -> list:
    for func_name in ['stock_board_industry_fund_flow_rank', 'stock_sector_fund_flow_rank']:
        func = getattr(ak, func_name, None)
        if func is None:
            continue
        try:
            df = func(symbol="今日")
            if df is None or df.empty:
                continue
            flow_col = next((c for c in df.columns if '主力' in c and '净' in c), None)
            change_col = next((c for c in df.columns if '涨跌' in c), None)
            name_col = next((c for c in df.columns if '名称' in c or '板块' in c), df.columns[0])
            if flow_col:
                df = df.sort_values(flow_col, ascending=False)
            sectors = []
            for _, row in df.head(6).iterrows():
                sectors.append({
                    'name': str(row[name_col]),
                    'change_pct': float(row[change_col]) if change_col else 0,
                    'main_flow_wan': float(row[flow_col]) if flow_col else 0,
                })
            return sectors
        except Exception as e:
            print(f"[警告] {func_name} 失败: {e}")
    return []


def get_stock_flow() -> list:
    df = safe_fetch(ak.stock_individual_fund_flow_rank, symbol="今日", default=None)
    if df is None or df.empty:
        return []
    try:
        flow_col = next((c for c in df.columns if '主力' in c and '净' in c), None)
        change_col = next((c for c in df.columns if '涨跌幅' in c), None)
        name_col = next((c for c in df.columns if '名称' in c or '简称' in c), None)
        code_col = next((c for c in df.columns if '代码' in c), None)
        amount_col = next((c for c in df.columns if '成交额' in c), None)
        if flow_col:
            df = df.sort_values(flow_col, ascending=False)
        stocks = []
        for _, row in df.head(25).iterrows():
            stocks.append({
                'code': str(row[code_col]) if code_col else '',
                'name': str(row[name_col]) if name_col else '',
                'change_pct': float(row[change_col]) if change_col else 0,
                'main_flow_wan': float(row[flow_col]) if flow_col else 0,
                'amount_wan': float(row[amount_col]) if amount_col else 0,
            })
        return stocks
    except Exception as e:
        print(f"[警告] 解析个股资金流失败: {e}")
        return []


def get_limit_up_pool(date_str: str) -> tuple:
    df = safe_fetch(ak.stock_zt_pool_em, date=date_str, default=None)
    if df is None or df.empty:
        return 0, []
    name_col = next((c for c in df.columns if '名称' in c or '简称' in c), None)
    code_col = next((c for c in df.columns if '代码' in c), None)
    change_col = next((c for c in df.columns if '涨跌幅' in c), None)
    stocks = []
    for _, row in df.head(20).iterrows():
        stocks.append({
            'code': str(row[code_col]) if code_col else '',
            'name': str(row[name_col]) if name_col else '',
            'change_pct': float(row[change_col]) if change_col else 100,
        })
    return len(df), stocks


def get_limit_down_pool(date_str: str) -> tuple:
    df = safe_fetch(ak.stock_dt_pool_em, date=date_str, default=None)
    if df is None or df.empty:
        return 0, []
    name_col = next((c for c in df.columns if '名称' in c or '简称' in c), None)
    code_col = next((c for c in df.columns if '代码' in c), None)
    stocks = []
    for _, row in df.head(10).iterrows():
        stocks.append({
            'code': str(row[code_col]) if code_col else '',
            'name': str(row[name_col]) if name_col else '',
        })
    return len(df), stocks


def wan_to_str(wan: float) -> str:
    if abs(wan) >= 100_000_000:
        return f"{wan / 100_000_000:.2f}万亿"
    elif abs(wan) >= 10_000:
        return f"{wan / 10_000:.2f}亿"
    return f"{wan:.0f}万"


def build_data_text(data: dict) -> str:
    lines = []

    lines.append("=== 主要指数 ===")
    for name, d in data['indices'].items():
        direction = "上涨" if d['change_pct'] > 0 else ("下跌" if d['change_pct'] < 0 else "平盘")
        lines.append(f"{name}：{direction} {abs(d['change_pct']):.2f}%，收 {d['price']:.2f} 点")

    act = data['activity']
    lines.append("\n=== 市场统计 ===")
    if data.get('total_amount_wan'):
        lines.append(f"两市成交总额：{wan_to_str(data['total_amount_wan'])}")
    if act:
        lines.append(f"上涨家数：{act.get('up', 'N/A')}，下跌家数：{act.get('down', 'N/A')}，平盘：{act.get('flat', 'N/A')}")
        lines.append(f"涨停家数：{act.get('limit_up', 'N/A')}，跌停家数：{act.get('limit_down', 'N/A')}")

    if data.get('limit_up_count'):
        lines.append(f"涨停板数量（涨停股池）：{data['limit_up_count']} 只")
    if data.get('limit_up_stocks'):
        lines.append("涨停股（前20）：" + "、".join(
            f"{s['name']}({s['code']})" for s in data['limit_up_stocks'][:20]
        ))
    if data.get('limit_down_count'):
        lines.append(f"跌停板数量：{data['limit_down_count']} 只")

    if data['sectors']:
        lines.append("\n=== 行业板块资金流向（主力净流入前6）===")
        for s in data['sectors']:
            sign = "+" if s['change_pct'] >= 0 else ""
            lines.append(f"{s['name']}：{sign}{s['change_pct']:.2f}%，主力净流入 {wan_to_str(s['main_flow_wan'])}")

    if data['stocks']:
        lines.append("\n=== 个股主力净流入前25 ===")
        for s in data['stocks']:
            sign = "+" if s['change_pct'] >= 0 else ""
            lines.append(f"{s['name']}（{s['code']}）：{sign}{s['change_pct']:.2f}%，主力净流入 {wan_to_str(s['main_flow_wan'])}")

    return '\n'.join(lines)


def push_wechat(title: str, content: str) -> None:
    sendkey = os.environ.get('SERVERCHAN_KEY', '').strip()
    if not sendkey:
        print("[跳过] 未配置 SERVERCHAN_KEY，跳过微信推送")
        return
    import requests
    resp = requests.post(
        f"https://sctapi.ftqq.com/{sendkey}.send",
        data={'title': title, 'desp': content},
        timeout=15,
    )
    result = resp.json()
    if result.get('code') == 0:
        print("微信推送成功")
    else:
        print(f"[警告] 微信推送失败: {result}")


def generate_review(data: dict, target_date: date) -> str:
    client = OpenAI(
        api_key=os.environ['DOUBAO_API_KEY'],
        base_url='https://ark.cn-beijing.volces.com/api/v3',
    )
    month_day = f"{target_date.month}.{target_date.day}"
    data_text = build_data_text(data)

    system_prompt = "你是一位专业的A股市场分析师，擅长撰写每日市场复盘报告，语言专业、客观，数据准确，分析深入。"

    user_prompt = f"""请根据以下 {target_date.strftime('%Y年%m月%d日')} A股收盘数据，严格按照指定格式生成复盘报告。

【今日市场数据】
{data_text}

【输出格式】（直接输出 Markdown，不要加代码块包裹）

# {month_day} 复盘

上证指数：[上涨/下跌][X.XX]%（收[XXXX.XX]点）
深圳指数：[上涨/下跌][X.XX]%（收[XXXXX.XX]点）
创业板指数：[上涨/下跌][X.XX]%（收[XXXX.XX]点）

两市成交总额: [X.XX万亿]（相比于前一日[放量/缩量][XXXX]亿 [上涨/下跌][X.XX]%）[量能性质]
上涨家数：下跌家数——[XXXX]:[XXXX]
涨停家数：跌停家数——[XXX]:[XX]
市场情绪：1）[情绪判断1]
           2）[情绪判断2]

总结：[150-200字综合总结，描述指数分化、量能特征、资金主线、市场叙事]

---

## 主线题材和板块：

### [板块名称]（主力资金流入[XX.XX]亿）
板块上涨：[X.XX]%

**情绪龙头：**

[股票名称]（[代码]）：+[X.XX]%，成交额约[XX.XX]亿，换手率约[X.XX]%，[公司核心业务一句话]+[当日上涨逻辑和市场表现，100字]

[第二只情绪龙头，同格式]

**中军龙头：**

[股票名称]（[代码]）：+[X.XX]%，成交额约[XX.XX]亿，换手率约[X.XX]%，总市值约[XXXX.XX]亿，[公司简介]+[当日表现，60字]

[第二只中军龙头，同格式]

**上涨原因：** [120-180字板块上涨核心催化剂和产业逻辑分析]

---

[重复以上格式，列出2-4个主线板块，按主力资金流入由大到小排列]

> 数据说明：部分个股换手率、总市值数据为基于多来源研究的合理估算，仅供参考。

【注意事项】
1. 指数涨跌幅、收盘点位、涨跌家数、涨跌停数必须使用上方提供的真实数据
2. 主线板块选主力净流入最高的2-4个行业
3. 情绪龙头和中军龙头从个股资金流数据中选取，优先选涨幅大、成交额高的龙头
4. 换手率、总市值等可合理估算，但涨跌幅必须与数据一致
5. 不要编造不存在的股票代码或名称
6. 对于前日成交额（用于放量/缩量判断），若无数据可合理估算
"""

    response = client.chat.completions.create(
        model='doubao-seed-2-0-pro-260215',
        max_tokens=5000,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )
    return response.choices[0].message.content.strip()


def main():
    date_env = os.environ.get('REVIEW_DATE', '').strip()
    target_date = date.fromisoformat(date_env) if date_env else date.today()

    print(f"目标复盘日期: {target_date}")

    if not is_trading_day(target_date):
        print(f"{target_date} 为非交易日，退出")
        sys.exit(0)

    output_path = Path('reviews') / f'{target_date}.md'
    if output_path.exists():
        print(f"{output_path} 已存在，退出")
        sys.exit(0)

    print("正在抓取市场数据...")
    date_str = target_date.strftime('%Y%m%d')

    indices = get_indices()
    activity = get_market_activity()
    sectors = get_sector_flow()
    stocks = get_stock_flow()
    limit_up_count, limit_up_stocks = get_limit_up_pool(date_str)
    limit_down_count, limit_down_stocks = get_limit_down_pool(date_str)

    total_amount_wan = None
    raw_total = activity.get('total_amount')
    if raw_total is not None:
        try:
            total_amount_wan = float(raw_total)
        except Exception:
            pass

    print(f"指数: {len(indices)} 个, 板块: {len(sectors)} 个, 个股: {len(stocks)} 只, 涨停: {limit_up_count} 只, 跌停: {limit_down_count} 只")

    data = {
        'indices': indices,
        'activity': activity,
        'sectors': sectors,
        'stocks': stocks,
        'limit_up_count': limit_up_count,
        'limit_up_stocks': limit_up_stocks,
        'limit_down_count': limit_down_count,
        'limit_down_stocks': limit_down_stocks,
        'total_amount_wan': total_amount_wan,
    }

    print("调用 Claude API 生成复盘内容...")
    content = generate_review(data, target_date)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding='utf-8')
    print(f"复盘已生成: {output_path}")

    month_day = f"{target_date.month}.{target_date.day}"
    push_wechat(f"{month_day} A股复盘", content)


if __name__ == '__main__':
    main()
