"""持仓币对当期 funding 监控（每小时一次）

判定：Biyi LONGSHORT 永续腿(SM-PU)有持仓的币，当期单期 funding < 阈值(默认 -5bp) 即告警。
方向假设：LONGSHORT = 现货多 + 永续空，故 funding 为负时我们付钱。Biyi 的 side 字段全为 None，
拿不到真实方向，此假设无法从接口验证。

失败可见性：Biyi 拉不到、某所 funding 全缺、持仓 base 匹配不上 perp symbol —— 都主动推 Slack
运维告警，绝不静默当成「无告警」。

用法：
    python3 pos_funding_monitor.py            # 跑一次
    python3 pos_funding_monitor.py --dry-run  # 跑一次但不发 Slack，只打屏
环境变量：
    SLACK_WEBHOOK_URL / ALERT_SLACK_WEBHOOK_URL(优先)  告警去哪
    ALERT_FUNDING_BP    阈值，默认 -5
    PROXY_URL           交易所代理，默认 http://127.0.0.1:7890（Biyi 是内网，始终不走代理）
"""
import os
import sys
from datetime import datetime

import requests

import stock_perp_24hvlum_openclaw as sp

ALERT_FUNDING_BP = float(os.environ.get("ALERT_FUNDING_BP", "-5"))
WEBHOOK = os.environ.get("ALERT_SLACK_WEBHOOK_URL") or os.environ.get("SLACK_WEBHOOK_URL", "")
EXCHANGES = ('Binance', 'OKX', 'Bybit')

# ====================== 持仓 ======================

def _exchange_of(account_map):
    """accountMap 字符串 → 交易所名；认不出返回 None"""
    a = str(account_map or '').lower()
    if 'binance' in a:
        return 'Binance'
    if 'okex' in a or 'okx' in a:
        return 'OKX'
    if 'bybit' in a:
        return 'Bybit'
    return None


def parse_biyi_positions(strategies):
    """Biyi strategies 列表 → {exchange: {base: pos_usd}}

    只取 LONGSHORT 的永续腿(SM-PU)：现货腿(SS-PU)没有 funding，且两腿名义相等，一起算会翻倍。
    同一 ticker 多账户累加；qty<=0 的僵尸腿丢弃。
    """
    out = {ex: {} for ex in EXCHANGES}
    for s in strategies or []:
        if s.get('strategyType') != 'LONGSHORT' or s.get('productType') != 'SM-PU':
            continue
        ticker = s.get('ticker') or ''
        if '/' not in ticker:
            continue
        ex = _exchange_of(s.get('accountMap'))
        if ex is None:
            continue
        try:
            qty = float(s.get('maxPositionQty') or 0)
        except (ValueError, TypeError):
            continue
        if qty <= 0:
            continue
        base = ticker.split('/')[0]
        out[ex][base] = out[ex].get(base, 0.0) + qty
    return out


def get_positions():
    """从 Biyi 拉持仓。返回 (positions, error)；error 非 None 表示这次数据不可信"""
    try:
        # 内网服务，显式禁用代理
        r = requests.post(sp.BIYI_URL, json={"query": "$productType like SM-PU|SS-PU"},
                          timeout=15, proxies={'http': None, 'https': None})
    except Exception as e:
        return {ex: {} for ex in EXCHANGES}, f"Biyi 请求异常: {type(e).__name__}: {e}"
    if r.status_code != 200:
        return {ex: {} for ex in EXCHANGES}, f"Biyi HTTP {r.status_code}"
    try:
        data = r.json().get('data') or []
    except Exception as e:
        return {ex: {} for ex in EXCHANGES}, f"Biyi 响应解析失败: {e}"
    pos = parse_biyi_positions(data)
    if not any(pos[ex] for ex in EXCHANGES):
        return pos, f"Biyi 返回 {len(data)} 条但无任何有效永续持仓腿"
    return pos, None


# ====================== base 名映射 ======================

MULT_PREFIXES = ('1000', '10000', '1000000')  # 小面值币的放大合约前缀


def base_variants(pos_base):
    """持仓 base 的候选 perp base，按优先级：原样 → 去尾部 B → 去首部 X → 各自加放大前缀

    Biyi 的 ticker 用配置侧的现货代币名，与永续名可能错位：
      Binance: 现货 CRCLB → 永续 CRCL（去尾 B）；SNDK/SOXL/MU 原样
      OKX:     现货 XTSLA → 永续 TSLA（去首 X）
    小面值币在 Binance/Bybit 是放大面值合约，OKX 用原名：
      PEPE → Bybit/Binance 的 1000PEPE；SATS → 10000SATS；MOG → 1000000MOG
      放大只改面值不改费率比例，所以取到的 funding 可直接用于原持仓。
    原样永远优先，避免 BNB 这类天然以 B 结尾的名字被误剥。
    """
    out = _plain_variants(pos_base) + [p + b for b in _plain_variants(pos_base) for p in MULT_PREFIXES]
    return _dedupe(out)


def _plain_variants(pos_base):
    """不含放大前缀的 base 候选：原样 → 去尾部 B → 去首部 X"""
    out = [pos_base]
    if len(pos_base) > 1 and pos_base.endswith('B'):
        out.append(pos_base[:-1])
    if len(pos_base) > 1 and pos_base.startswith('X'):
        out.append(pos_base[1:])
    return _dedupe(out)


def _dedupe(items):
    seen, uniq = set(), []
    for b in items:
        if b and b not in seen:
            seen.add(b)
            uniq.append(b)
    return uniq


def spot_base_variants(pos_base):
    """现货 base 候选：原样 → 加尾部 B → 加首部 X

    与 perp 方向相反（perp 是剥、现货是加），因为 Biyi 的 ticker 命名不统一：
      Binance 股票现货是 XXXB —— pos_base 'CRCLB' 原样命中，但 'SNDK' 要补 B（SNDKUSDT 不存在）
      OKX 代币化股票现货是 X 前缀 —— pos_base 'SOXL' 要补 X（XSOXL-USDT）
    普通币（BTC/PEPE）原样即命中，所以补 B/补 X 只在原样落空时才生效，不会误匹配。
    """
    return _dedupe([pos_base, pos_base + 'B', 'X' + pos_base])


def perp_multiplier(pos_base, perp_base):
    """永续相对现货的面值放大倍数：1000PEPE vs PEPE → 1000.0；非放大关系 → 1.0

    Binance/Bybit 对小面值币的放大合约，报价本身是单币价的 N 倍，算价差前必须折回。
    """
    for b in _plain_variants(pos_base):
        if perp_base == b:
            return 1.0
    for p in MULT_PREFIXES:
        for b in _plain_variants(pos_base):
            if perp_base == p + b:
                return float(p)
    return 1.0


def match_base(pos_base, universe):
    """在该所的 perp base 集合里找对应；找不到返回 None"""
    for cand in base_variants(pos_base):
        if cand in universe:
            return cand
    return None


# ====================== 各所 perp universe + 当期 funding ======================

def binance_perp_funding():
    """→ ({base: symbol}, {base: {'bp':float,'interval_h':float}})；3 个批量接口，一次拿全所"""
    info = sp.fetch_json("https://fapi.binance.com/fapi/v1/exchangeInfo")
    universe = {}
    if info and 'symbols' in info:
        for s in info['symbols']:
            if (s.get('contractType') in ('PERPETUAL', 'TRADIFI_PERPETUAL')
                    and s.get('quoteAsset') == 'USDT' and s.get('status') == 'TRADING'):
                universe[s['symbol'].replace('USDT', '')] = s['symbol']

    interval_map = {}
    fi = sp.fetch_json("https://fapi.binance.com/fapi/v1/fundingInfo")
    if isinstance(fi, list):
        for it in fi:
            interval_map[it.get('symbol')] = it.get('fundingIntervalHours')

    funding = {}
    prem = sp.fetch_json("https://fapi.binance.com/fapi/v1/premiumIndex")
    if isinstance(prem, list):
        by_sym = {p.get('symbol'): p for p in prem}
        for base, sym in universe.items():
            p = by_sym.get(sym)
            if not p:
                continue
            try:
                rate = float(p.get('lastFundingRate'))
            except (TypeError, ValueError):
                continue
            funding[base] = {'bp': rate * 10000,
                             'interval_h': float(interval_map.get(sym) or 8)}
    return universe, funding


def bybit_perp_funding():
    """→ (universe set, {base: {'bp','interval_h'}})；2 个批量接口"""
    interval_map = {}
    info = sp.fetch_json("https://api.bybit.com/v5/market/instruments-info",
                         {'category': 'linear', 'limit': 1000})
    if info and info.get('retCode') == 0:
        for it in info.get('result', {}).get('list', []):
            fi = it.get('fundingInterval')
            if fi:
                interval_map[it['symbol']] = fi / 60.0

    universe, funding = set(), {}
    tick = sp.fetch_json("https://api.bybit.com/v5/market/tickers", {'category': 'linear'})
    if tick and tick.get('retCode') == 0:
        for it in tick.get('result', {}).get('list', []):
            sym = it.get('symbol', '')
            if not sym.endswith('USDT'):
                continue
            base = sym[:-4]
            universe.add(base)
            try:
                rate = float(it.get('fundingRate'))
            except (TypeError, ValueError):
                continue
            funding[base] = {'bp': rate * 10000,
                             'interval_h': float(interval_map.get(sym) or 8)}
    return universe, funding


def okx_perp_universe():
    """OKX USDT 本位永续的 base 集合（1 个批量接口）"""
    universe = set()
    sw = sp.fetch_json("https://www.okx.com/api/v5/market/tickers", {'instType': 'SWAP'})
    if sw and sw.get('code') == '0':
        for x in sw.get('data', []):
            i = x.get('instId', '')
            if i.endswith('-USDT-SWAP'):
                universe.add(i.split('-')[0])
    return universe


def okx_funding(bases):
    """OKX 无批量 funding 接口，只对需要的 base 并发逐个查（持仓币数量有限，不触发限频）"""
    out = {}

    def _one(base):
        d = sp.fetch_json(f"https://www.okx.com/api/v5/public/funding-rate?instId={base}-USDT-SWAP")
        if not (d and d.get('code') == '0' and d.get('data')):
            return
        fr = d['data'][0]
        try:
            rate = float(fr.get('fundingRate'))
            ft, nt = int(fr.get('fundingTime') or 0), int(fr.get('nextFundingTime') or 0)
        except (TypeError, ValueError):
            return
        interval = (nt - ft) / 3600000 if nt > ft > 0 else 8
        out[base] = {'bp': rate * 10000, 'interval_h': float(interval)}

    sp.parallel_each(_one, list(bases), workers=3)
    return out


# ====================== 汇总 ======================

def collect_rows(positions):
    """→ (rows, problems)

    rows: [{'exchange','pos_base','perp_base','bp','interval_h','pos_usd'}]  已取到 funding 的持仓币
    problems: 数据可信度问题的人类可读描述列表
    """
    rows, problems = [], []

    bn_universe, bn_funding = binance_perp_funding()
    bb_universe, bb_funding = bybit_perp_funding()
    ok_universe = okx_perp_universe()

    universes = {'Binance': set(bn_universe), 'Bybit': bb_universe, 'OKX': ok_universe}
    fundings = {'Binance': bn_funding, 'Bybit': bb_funding, 'OKX': {}}

    # OKX 的 funding 按需拉，先做映射
    matched = {}   # {ex: {perp_base: (pos_base, pos_usd)}}
    for ex in EXCHANGES:
        matched[ex] = {}
        if positions[ex] and not universes[ex]:
            problems.append(f"{ex}: 有 {len(positions[ex])} 个持仓币，但 perp 列表一个都没拉到（代理/接口异常）")
            continue
        unmatched = []
        for pos_base, pos_usd in positions[ex].items():
            perp = match_base(pos_base, universes[ex])
            if perp is None:
                unmatched.append(pos_base)
            else:
                matched[ex][perp] = (pos_base, pos_usd)
        if unmatched:
            problems.append(f"{ex}: 持仓币在 perp 列表里匹配不到 → {', '.join(sorted(unmatched))}")

    if matched['OKX']:
        fundings['OKX'] = okx_funding(matched['OKX'].keys())

    for ex in EXCHANGES:
        if not matched[ex]:
            continue
        missing = []
        for perp, (pos_base, pos_usd) in matched[ex].items():
            f = fundings[ex].get(perp)
            if f is None:
                missing.append(perp)
                continue
            rows.append({'exchange': ex, 'pos_base': pos_base, 'perp_base': perp,
                         'bp': f['bp'], 'interval_h': f['interval_h'], 'pos_usd': pos_usd})
        if missing and len(missing) == len(matched[ex]):
            problems.append(f"{ex}: {len(missing)} 个持仓币的 funding 全部取不到（代理/限频/接口变更）")
        elif missing:
            problems.append(f"{ex}: funding 取不到 → {', '.join(sorted(missing))}")

    return rows, problems


def pick_alerts(rows, threshold=ALERT_FUNDING_BP):
    """当期 funding 低于阈值的行，最负的排最前"""
    return sorted((r for r in rows if r['bp'] < threshold), key=lambda r: r['bp'])


# ====================== 告警币的盘口与价差 ======================

def _f(v):
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def binance_books():
    """→ ({spot_base: (bid, ask)}, {perp_base: (bid, ask)})，各一次全量盘口"""
    spot = {}
    d = sp.fetch_json("https://api.binance.com/api/v3/ticker/bookTicker")
    if isinstance(d, list):
        for x in d:
            s = x.get('symbol', '')
            if s.endswith('USDT'):
                spot[s[:-4]] = (_f(x.get('bidPrice')), _f(x.get('askPrice')))
    perp = {}
    d = sp.fetch_json("https://fapi.binance.com/fapi/v1/ticker/bookTicker")
    if isinstance(d, list):
        for x in d:
            s = x.get('symbol', '')
            if s.endswith('USDT'):
                perp[s[:-4]] = (_f(x.get('bidPrice')), _f(x.get('askPrice')))
    return spot, perp


def bybit_books():
    """→ ({spot_base: (bid, ask)}, {perp_base: (bid, ask)})"""
    out = []
    for cat in ('spot', 'linear'):
        m = {}
        d = sp.fetch_json("https://api.bybit.com/v5/market/tickers", {'category': cat})
        if d and d.get('retCode') == 0:
            for x in d.get('result', {}).get('list', []):
                s = x.get('symbol', '')
                if s.endswith('USDT'):
                    m[s[:-4]] = (_f(x.get('bid1Price')), _f(x.get('ask1Price')))
        out.append(m)
    return out[0], out[1]


def okx_books():
    """→ ({spot_base: (bid, ask)}, {perp_base: (bid, ask)})"""
    spot = {}
    d = sp.fetch_json("https://www.okx.com/api/v5/market/tickers", {'instType': 'SPOT'})
    if d and d.get('code') == '0':
        for x in d.get('data', []):
            i = x.get('instId', '')
            if i.endswith('-USDT'):
                spot[i[:-5]] = (_f(x.get('bidPx')), _f(x.get('askPx')))
    perp = {}
    d = sp.fetch_json("https://www.okx.com/api/v5/market/tickers", {'instType': 'SWAP'})
    if d and d.get('code') == '0':
        for x in d.get('data', []):
            i = x.get('instId', '')
            if i.endswith('-USDT-SWAP'):
                perp[i.split('-')[0]] = (_f(x.get('bidPx')), _f(x.get('askPx')))
    return spot, perp


BOOK_FETCHERS = {'Binance': binance_books, 'Bybit': bybit_books, 'OKX': okx_books}


def spread_bp(spot_ask, perp_bid):
    """(现货卖一 − 合约买一) / 现货卖一 × 1e4；缺任一腿返回 None

    这是开仓/加仓方向的价差：买现货吃卖一、空永续吃买一。为负说明永续报价高于现货卖价。
    """
    if not spot_ask or not perp_bid:
        return None
    return (spot_ask - perp_bid) / spot_ask * 10000


def attach_quotes(alerts):
    """给告警行补 spot_ask / perp_bid / spread_bp。只对告警涉及的交易所拉盘口。

    perp_bid 已按放大倍数折回单币价（1000PEPE 的报价 ÷1000），以便与现货卖一直接可比。
    """
    for ex in {r['exchange'] for r in alerts}:
        spot_book, perp_book = BOOK_FETCHERS[ex]()
        for r in (x for x in alerts if x['exchange'] == ex):
            mult = perp_multiplier(r['pos_base'], r['perp_base'])
            pb = perp_book.get(r['perp_base'], (None, None))[0]
            r['perp_bid'] = pb / mult if pb else None
            r['mult'] = mult
            r['spot_ask'] = None
            for cand in spot_base_variants(r['pos_base']):
                if cand in spot_book:
                    r['spot_ask'] = spot_book[cand][1]
                    r['spot_sym'] = cand
                    break
            r['spread_bp'] = spread_bp(r['spot_ask'], r['perp_bid'])
    return alerts


# ====================== Slack ======================

def _fmt_interval(h):
    return f"{h:g}h"


def _fmt_px(v):
    """价格：跨度从 0.0000027(PEPE) 到 1056(SNDK)，用 6 位有效数字"""
    return f"{v:.6g}" if isinstance(v, (int, float)) else '-'


def _fmt_bp(v):
    return f"{v:.1f}" if isinstance(v, (int, float)) else '-'


def build_blocks(alerts, problems, rows_total, now_str, threshold=ALERT_FUNDING_BP):
    """告警 + 运维问题合成一条消息的 blocks；都没有则返回 []"""
    blocks = []
    if alerts:
        # 表头用 ASCII：中文在等宽字体里占 2 格，Python 的 :<8 按字符数算会错位
        lines = ["```",
                 (f"{'ex':<8} {'sym':<10} {'funding':>10} {'int':>5} {'pos':>8} "
                  f"{'spotAsk':>11} {'perpBid':>11} {'sprd':>8}")]
        for r in alerts:
            lines.append(f"{r['exchange']:<8} {r['perp_base']:<10} "
                         f"{r['bp']:>8.2f}bp {_fmt_interval(r['interval_h']):>5} "
                         f"{sp.fmt_mio2(r['pos_usd']):>8} "
                         f"{_fmt_px(r.get('spot_ask')):>11} {_fmt_px(r.get('perp_bid')):>11} "
                         f"{_fmt_bp(r.get('spread_bp')):>8}")
        lines.append("```")
        blocks += [
            {"type": "header", "text": {"type": "plain_text",
                                        "text": f"🚨 持仓 funding 告警 ({len(alerts)}) — {now_str}"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}},
            {"type": "context", "elements": [{"type": "mrkdwn",
                "text": (f"当期单期费率 < {threshold:g}bp 即告警｜共扫描 {rows_total} 个持仓币｜"
                         "方向按 LONGSHORT=空永续推定，负费率=我们付钱｜"
                         "同 -5bp 在 1h 周期的出血速度是 8h 的 8 倍\n"
                         "`sprd`=(现货卖一−合约买一)/现货卖一×1e4 bp，开仓方向；"
                         "放大合约(1000PEPE等)的 `perpBid` 已按倍数折回单币价；`-`=盘口未取到")}]},
        ]
    if problems:
        blocks += [
            {"type": "header", "text": {"type": "plain_text",
                                        "text": f"⚠️ 持仓 funding 监控数据异常 — {now_str}"}},
            {"type": "section", "text": {"type": "mrkdwn",
                                         "text": "\n".join(f"• {p}" for p in problems)}},
            {"type": "context", "elements": [{"type": "mrkdwn",
                "text": "上述币这一轮未被有效监控，不代表它们 funding 正常"}]},
        ]
    return blocks


def send_slack(blocks):
    if not WEBHOOK:
        print("⚠️ 未配置 SLACK_WEBHOOK_URL，跳过发送")
        return
    try:
        resp = requests.post(WEBHOOK, json={"blocks": blocks}, timeout=10)
        print("✅ 已发送 Slack" if resp.status_code == 200 else f"❌ Slack 失败: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        print(f"❌ Slack 错误: {e}")


def main(dry_run=False):
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"\n🕒 持仓 funding 监控 {now_str}（阈值 {ALERT_FUNDING_BP:g}bp）")

    positions, pos_err = get_positions()
    problems = [pos_err] if pos_err else []
    n_pos = sum(len(positions[ex]) for ex in EXCHANGES)
    print("📦 持仓币：" + "，".join(f"{ex} {len(positions[ex])}" for ex in EXCHANGES) + f"（共 {n_pos}）")

    rows, collect_problems = ([], []) if pos_err else collect_rows(positions)
    problems += collect_problems

    for r in sorted(rows, key=lambda r: r['bp']):
        flag = "🚨" if r['bp'] < ALERT_FUNDING_BP else "  "
        print(f"{flag} {r['exchange']:<8} {r['perp_base']:<10} {r['bp']:>8.2f}bp "
              f"{_fmt_interval(r['interval_h']):>5} {sp.fmt_mio2(r['pos_usd']):>9}")

    alerts = pick_alerts(rows)
    if alerts:
        attach_quotes(alerts)
        for r in alerts:
            note = f"  (放大{r['mult']:g}x已折回)" if r.get('mult', 1) != 1 else ""
            print(f"   ↳ {r['exchange']} {r['perp_base']}: "
                  f"spotAsk={_fmt_px(r.get('spot_ask'))}({r.get('spot_sym', '?')}) "
                  f"perpBid={_fmt_px(r.get('perp_bid'))} "
                  f"sprd={_fmt_bp(r.get('spread_bp'))}bp{note}")
    print(f"📊 已监控 {len(rows)} 个持仓币，触发告警 {len(alerts)} 个，数据异常 {len(problems)} 条")
    for p in problems:
        print(f"⚠️ {p}")

    blocks = build_blocks(alerts, problems, len(rows), now_str)
    if not blocks:
        print("✅ 无告警、无异常，不发 Slack")
        return
    if dry_run:
        print("🧪 dry-run，不发 Slack")
        return
    send_slack(blocks)


if __name__ == "__main__":
    main(dry_run='--dry-run' in sys.argv)
