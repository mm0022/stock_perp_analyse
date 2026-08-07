r"""持仓币对当期 funding 监控（每小时一次）

判定：Biyi LONGSHORT 永续腿(SM-PU)有持仓的币，当期单期 funding < 阈值(默认 -5bp) 即告警。
方向假设：LONGSHORT = 现货多 + 永续空，故 funding 为负时我们付钱。Biyi 的 side 字段全为 None，
拿不到真实方向，此假设无法从接口验证。

失败可见性：Biyi 拉不到、某所 funding 全缺、持仓 base 匹配不上 perp symbol —— 都主动推 Slack
运维告警，绝不静默当成「无告警」。

用法（Windows / macOS / Linux 完全相同，不需要任何包装脚本）：
    python pos_funding_monitor.py                       # 跑一次
    python pos_funding_monitor.py --dry-run             # 跑一次但不发 Slack，只打屏
    python pos_funding_monitor.py --log pos_funding_monitor.log   # 同时写日志(UTF-8)

用 `--log` 而不是 shell 的 `>>`：日志由脚本以 UTF-8 直接写（stdout+stderr 都进，
等价 2>&1），与控制台编码无关。Windows 上也不必设 PYTHONIOENCODING——
console_io.init_output() 已在脚本内处理编码（否则输出里的 emoji 在 GBK 下会崩）。

环境变量：
    SLACK_WEBHOOK_URL / ALERT_SLACK_WEBHOOK_URL(优先)  告警去哪
    ALERT_FUNDING_BP    阈值，默认 -5
    PROXY_URL           交易所代理，默认 http://127.0.0.1:7890（Biyi 是内网，始终不走代理）

    Windows 首次设置（cmd 执行一次，之后要重开新窗口才生效）：
        setx SLACK_WEBHOOK_URL "https://hooks.slack.com/services/你的/webhook"
        setx PROXY_URL "http://127.0.0.1:7890"
        ^ 这是【回退候选】：脚本每轮先试直连，直连不通才用它。能直连就设空： setx PROXY_URL ""

定时每小时一次（这是**周期任务**，不是长跑进程）：
    Windows「任务计划程序」直接指向 python.exe，不经过任何脚本：
        程序:   C:\path\to\python.exe
        参数:   pos_funding_monitor.py --log pos_funding_monitor.log
        起始于: D:\stock_perp_analyse
        触发器: 每天，重复间隔 1 小时，持续 1 天
    或命令行创建（落在 05 分而非整点，避开交易所结算瞬间——费率此刻正在翻页）：
        schtasks /create /tn "PosFundingMonitor" /sc hourly /st 00:05 ^
          /tr "C:\path\to\python.exe D:\stock_perp_analyse\pos_funding_monitor.py --log D:\stock_perp_analyse\pos_funding_monitor.log"
        查看： schtasks /query /tn "PosFundingMonitor" /v /fo list
        试跑： schtasks /run  /tn "PosFundingMonitor"
        删除： schtasks /delete /tn "PosFundingMonitor" /f
    macOS/Linux crontab：
        5 * * * * cd /path/to/repo && python3 pos_funding_monitor.py --log pos_funding_monitor.log

前提检查：Biyi 是内网服务，运行这台机器必须能访问 https://biyi.tky.laozi.pro
否则监控拿不到持仓（脚本会推 Slack 运维告警，不会静默当成「无告警」）。
"""
import os
import sys
import time
from datetime import datetime

import requests

import stock_perp_24hvlum_openclaw as sp
from console_io import init_output

ALERT_FUNDING_BP = float(os.environ.get("ALERT_FUNDING_BP", "-5"))
PROBE_URL = "https://fapi.binance.com/fapi/v1/ping"
WEBHOOK = os.environ.get("ALERT_SLACK_WEBHOOK_URL") or os.environ.get("SLACK_WEBHOOK_URL", "")
EXCHANGES = ('Binance', 'OKX', 'Bybit')

# ====================== 网络出口 ======================

def pick_proxies():
    """探测可用出口并写回 sp.proxies，返回 (proxies, 描述, error)

    本机出口在直连和代理之间来回切（代理软件时开时关），实测同一天内两种情况都出现过：
    直连通/代理拒连，以及直连全挂/代理通。硬编码任一种都会周期性让监控整轮失效，
    所以每轮启动时按 直连 → PROXY_URL 顺序探测，取第一个通的。
    """
    candidates = [({}, '直连')]
    proxy_url = os.environ.get("PROXY_URL", "")
    if proxy_url:
        candidates.append(({'http': proxy_url, 'https': proxy_url}, f'代理 {proxy_url}'))
    for prox, name in candidates:
        try:
            if requests.get(PROBE_URL, proxies=prox, timeout=8).status_code == 200:
                sp.proxies = prox
                return prox, name, None
        except Exception:
            continue
    tried = ' / '.join(n for _, n in candidates)
    return None, tried, f"网络出口全部不通（已试 {tried}），本轮无法取任何行情"


# ====================== premium 与 funding 预测 ======================
# 两所公式相同（已在 Binance/OKX × 股票/加密 共 5 个案例上数值验证）：
#     funding = clamp( premium + clamp(利率 − premium, ±5bp), ±cap )
# 即以「利率」为中心存在一个 ±5bp 的死区：premium 落在死区内，funding 恒等于利率。
# 股票永续两所利率都是 0 → 死区 [−5bp, +5bp]，funding 恰好为 0（实测 37~53% 的期数如此）。
# 加密永续利率 1bp → 死区 [−4bp, +6bp]，funding 恒为 1bp，永不归零。
DEAD_ZONE_BP = 5.0


def _bp(v):
    """接口返回的小数费率 → bp；缺失/非法返回 None（0 是合法值，不能当缺失）"""
    try:
        return float(v) * 10000
    except (TypeError, ValueError):
        return None


def _ms(v):
    """毫秒时间戳 → int；缺失/非法返回 None"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _rel_bp(a, b):
    """(a − b) / b × 1e4；任一缺失或 b<=0 返回 None"""
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return None
    return (a - b) / b * 10000 if b > 0 else None


def funding_if_premium_held(prem_bp, interest_bp, cap_bp=None):
    """把当前瞬时 premium 代入公式的结果(bp)——这是「若 premium 恒定不变」的假想值，
    **不是对下期 funding 的预测**。

    实测过它作为预测有多差（OKX 15 个标的，瞬时 premium vs 本期实际累积 funding）：
        SNDK 瞬时-22.56bp → 假想-17.56bp，实际累积仅 -4.10bp（差 13.46bp）
        CRCL/MU/PEPE/LINK 连正负号都相反
        平均绝对偏差 1.68bp，最大 13.46bp，15 个里 4 个符号判错
    因为交易所的 funding 是整个结算周期(8h)内 premium 的时间加权平均，瞬时值噪声极大。
    仅用于单测校准公式本身，不进入告警判断。
    """
    if prem_bp is None or interest_bp is None:
        return None
    adj = max(-DEAD_ZONE_BP, min(DEAD_ZONE_BP, interest_bp - prem_bp))
    f = prem_bp + adj
    if cap_bp:
        f = max(-cap_bp, min(cap_bp, f))
    return f


def period_elapsed_pct(next_funding_ms, interval_h, now_ms):
    """本期已过百分比 0..100。累积 funding 还剩多少变动空间取决于它：
    已过 90% 时当前累积值基本锁定；已过 10% 时还能大幅漂移。"""
    if not next_funding_ms or not interval_h or interval_h <= 0:
        return None
    span = interval_h * 3600_000
    remain = next_funding_ms - now_ms
    if remain < 0 or remain > span:
        return None
    return (1 - remain / span) * 100


def funding_slack(prem_bp):
    """瞬时 premium 离「负 funding 分界线(−5bp)」的距离 = prem + 5bp。

    这是一个**瞬时压力**读数，不是对下期 funding 的预测——见 funding_if_premium_held 的实测偏差。
    类比：slack 是时速表（此刻往哪个方向走多快），funding 列是里程表（本期已累积多少）。
      >0  此刻不在流血区，premium 还要再跌这么多才会开始拖低累积值
      <0  此刻处在流血区，若持续停留会把累积 funding 往负拖

    分界线为何是 −5bp（与利率无关，只要利率≥0）：
      prem ≥ 利率−5（死区内）→ funding = 利率 ≥ 0
      prem < 利率−5         → funding = prem+5，仅当 prem < −5 才为负
    所以 Bybit 不给利率项也能算。
    """
    if prem_bp is None:
        return None
    return prem_bp + DEAD_ZONE_BP


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

    interval_map, cap_map = {}, {}
    fi = sp.fetch_json("https://fapi.binance.com/fapi/v1/fundingInfo")
    if isinstance(fi, list):
        for it in fi:
            interval_map[it.get('symbol')] = it.get('fundingIntervalHours')
            try:
                cap_map[it['symbol']] = abs(float(it['adjustedFundingRateCap'])) * 10000
            except (KeyError, TypeError, ValueError):
                pass

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
                             'interval_h': float(interval_map.get(sym) or 8),
                             'cap_bp': cap_map.get(sym),
                             'interest_bp': _bp(p.get('interestRate')),
                             # Binance 不暴露原始 premium index；用 (mark−index) 近似。
                             # 注意 markPrice 本身已含 funding basis 的移动平均，所以这是
                             # 平滑后的值，比 OKX 的冲击价 premium 钝，会低估瞬时尖峰。
                             'prem_bp': _rel_bp(p.get('markPrice'), p.get('indexPrice')),
                             'prem_src': 'mark−idx',
                             'next_ms': _ms(p.get('nextFundingTime'))}
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
                             'interval_h': float(interval_map.get(sym) or 8),
                             'cap_bp': None,
                             # Bybit 不暴露利率项，没有它无法定位死区中心 → 不做 funding 预测，
                             # 只报 premium 本身。宁可显示 '-' 也不猜一个值。
                             'interest_bp': None,
                             'prem_bp': _rel_bp(it.get('markPrice'), it.get('indexPrice')),
                             'prem_src': 'mark−idx',
                             'next_ms': _ms(it.get('nextFundingTime'))}
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
        out[base] = {'bp': rate * 10000, 'interval_h': float(interval),
                     'cap_bp': _bp(fr.get('maxFundingRate')),
                     'interest_bp': _bp(fr.get('interestRate')),
                     # OKX 官方 premium：按 impactValue(如 SNDK $1万) 的冲击价算，
                     # 不是盘口中价，也不是 mark。这是唯一一所直接给出 funding 输入量的。
                     'prem_bp': _bp(fr.get('premium')),
                     'prem_src': '官方',
                     'next_ms': _ms(fr.get('fundingTime'))}

    sp.parallel_each(_one, list(bases), workers=3)
    return out


# ====================== 汇总 ======================

def collect_rows(positions):
    """→ (rows, problems)

    rows: [{'exchange','pos_base','perp_base','bp','interval_h','pos_usd'}]  已取到 funding 的持仓币
    problems: 数据可信度问题的人类可读描述列表
    """
    rows, problems = [], []
    now_ms = int(time.time() * 1000)

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
            prem, ir = f.get('prem_bp'), f.get('interest_bp')
            rows.append({'exchange': ex, 'pos_base': pos_base, 'perp_base': perp,
                         'bp': f['bp'], 'interval_h': f['interval_h'], 'pos_usd': pos_usd,
                         'prem_bp': prem, 'prem_src': f.get('prem_src'),
                         'interest_bp': ir, 'cap_bp': f.get('cap_bp'),
                         'slack_bp': funding_slack(prem),
                         'elapsed_pct': period_elapsed_pct(f.get('next_ms'), f['interval_h'], now_ms)})
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
                 (f"{'ex':<8} {'sym':<10} {'funding':>10} {'int':>5} {'已过':>6} {'pos':>8} "
                  f"{'prem':>7} {'slack':>7} "
                  f"{'spotAsk':>11} {'perpBid':>11} {'sprd':>8}")]
        for r in alerts:
            ep = r.get('elapsed_pct')
            lines.append(f"{r['exchange']:<8} {r['perp_base']:<10} "
                         f"{r['bp']:>8.2f}bp {_fmt_interval(r['interval_h']):>5} "
                         f"{(f'{ep:.0f}%' if ep is not None else '-'):>6} "
                         f"{sp.fmt_mio2(r['pos_usd']):>8} "
                         f"{_fmt_bp(r.get('prem_bp')):>7} {_fmt_bp(r.get('slack_bp')):>7} "
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
                         "`funding` 是本期累积到现在的值（已是周期内的时间加权平均，权威口径）｜"
                         "`已过`=本期走完百分比，越接近 100% 累积值越锁定\n"
                         "`prem`=**瞬时**永续/指数偏离｜"
                         f"`slack`=prem+{DEAD_ZONE_BP:g}bp，<0 表示此刻在流血区。"
                         "两者都是瞬时压力读数，**不能当下期 funding 的预测**"
                         "（实测瞬时值代入公式最大偏 13bp，15 个里 4 个符号都反）\n"
                         "OKX 的 prem 是官方冲击价口径；Binance/Bybit 用 (mark−index) 近似，偏平滑会低估尖峰\n"
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

    _, exit_name, net_err = pick_proxies()
    print(f"🌐 出口：{exit_name}" if not net_err else f"⚠️ {net_err}")

    positions, pos_err = get_positions()
    problems = [p for p in (net_err, pos_err) if p]
    n_pos = sum(len(positions[ex]) for ex in EXCHANGES)
    print("📦 持仓币：" + "，".join(f"{ex} {len(positions[ex])}" for ex in EXCHANGES) + f"（共 {n_pos}）")

    if pos_err or net_err:
        rows, collect_problems = [], []   # 出口或持仓不可信时不再发无意义的行情请求
    else:
        rows, collect_problems = collect_rows(positions)
    problems += collect_problems

    if rows:
        print(f"   {'ex':<8} {'sym':<10} {'累积funding':>11} {'int':>4} {'本期已过':>8} "
              f"{'pos':>8} {'瞬时prem':>9} {'slack':>7}  prem来源")
    for r in sorted(rows, key=lambda r: (r['slack_bp'] if r['slack_bp'] is not None else 999, r['bp'])):
        flag = "🚨" if r['bp'] < ALERT_FUNDING_BP else ("⚠️" if (r['slack_bp'] or 999) < 0 else "  ")
        ep = r.get('elapsed_pct')
        print(f"{flag} {r['exchange']:<8} {r['perp_base']:<10} {r['bp']:>9.2f}bp "
              f"{_fmt_interval(r['interval_h']):>4} {(f'{ep:.0f}%' if ep is not None else '-'):>8} "
              f"{sp.fmt_mio2(r['pos_usd']):>8} "
              f"{_fmt_bp(r.get('prem_bp')):>9} {_fmt_bp(r.get('slack_bp')):>7}  {r.get('prem_src') or '-'}")
    bleeding = sorted((r for r in rows if (r.get('slack_bp') is not None and r['slack_bp'] < 0)),
                      key=lambda r: r['slack_bp'])
    if bleeding:
        print(f"   ⚠️ {len(bleeding)} 个此刻 premium 在流血区(<−{DEAD_ZONE_BP:g}bp)，"
              f"若持续停留会把累积 funding 往负拖（瞬时压力，非预测）: "
              + ", ".join(f"{r['exchange']}/{r['perp_base']}({r['slack_bp']:.1f}bp)" for r in bleeding))

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


def _log_arg():
    """--log FILE → 路径；未指定返回 None"""
    if "--log" not in sys.argv:
        return None
    i = sys.argv.index("--log") + 1
    return sys.argv[i] if i < len(sys.argv) else None


if __name__ == "__main__":
    init_output(_log_arg())
    main(dry_run='--dry-run' in sys.argv)
