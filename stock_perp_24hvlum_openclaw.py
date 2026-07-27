import requests
import pandas as pd
import time
from datetime import datetime
import os
import json
from concurrent.futures import ThreadPoolExecutor

# ====================== 配置 ======================
PROXY_URL = os.environ.get("PROXY_URL", "http://127.0.0.1:7890")  # 环境变量可覆盖；无代理设为 ""
LOG_FILE_NAME = "crypto_stock_volume_log.csv"
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")  # 从环境变量读，避免密钥进 git

# 策略参数
HISTORY_DAYS = 30
FEE = {  # 单笔单腿费率，单位 bp
    'Binance': {'maker': 0.0,  'taker': 1.5},
    'Bybit':   {'maker': 0.0,  'taker': 1.25},
    'OKX':     {'maker': 0.8,  'taker': 2.7},
}
FEE_MODE = 'mixed'
SIGN_CONSISTENCY_MIN = 0.8
BREAKEVEN_DAYS_MAX = 15
MAX_SPREAD_BP = 200        # 跨所价差超此值(bp)视为盘前/pre-IPO或价格背离，剔除（正常已上市股票通常<1%）
OKX_DISCOUNT_AMOUNT_USD = 50000   # OKX 折算率按此仓位规模(USDT)取覆盖到的最低档
MIN_VOLUME = 1_000_000

# 股票/大宗/指数 代币列表（仅股票、重金属、指数）
TARGET_TOKENS = [
    # 美股
    'TSLA', 'NVDA', 'AAPL', 'GOOG', 'META', 'MSFT', 'AMZN', 'MSTR', 'AMD', 'NFLX', 'DIS', 'KO', 'PEP', 'WMT', 'JNJ', 'PG',
    # 金属/大宗商品（所有支持交易的）
    'XAU', 'XAG', 'CL', 'NG', 'PAXG',  # 黄金、白银、原油、天然气、黄金代币
    # 指数（代币化）
    'SPX', 'NDX',
]

proxies = {}
if PROXY_URL:
    proxies = {"http": PROXY_URL, "https": PROXY_URL}

def format_num(val):
    if isinstance(val, (int, float)):
        return f"{val:,.0f}"
    return str(val)

EX_DISPLAY = {'Binance': 'BINANCE-U', 'OKX': 'OKX-U', 'Bybit': 'BYBIT-U'}

def fmt_oi(v):
    """OI 带 B/M/K 单位"""
    if not isinstance(v, (int, float)):
        return '-'
    if v >= 1e9:
        return f"{v/1e9:.2f}B"
    if v >= 1e6:
        return f"{v/1e6:.2f}M"
    if v >= 1e3:
        return f"{v/1e3:.2f}K"
    return f"{v:.0f}"

def fmt_mio(v):
    """成交量/金额统一按百万(mio)显示，便于横向比较"""
    if not isinstance(v, (int, float)):
        return '-'
    return f"{v/1e6:.1f}M"

def fmt_pct(v):
    """年化/波动 带 % 号（1位小数）"""
    return f"{v:.1f}%" if isinstance(v, (int, float)) else '-'

def get_binance_collateral_rate():
    """Binance 组合保证金折算率(公开 bapi，无需 API key)。返回 {asset: collateralRate}。
    股票代币的 asset 为现货代币名(如 TSLAB)，当前折算率 0.5"""
    try:
        resp = requests.get(
            "https://www.binance.com/bapi/margin/v1/public/margin/portfolio/collateral-rate",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=10, proxies=proxies)
        if resp.status_code != 200:
            print(f"⚠️ Binance 折算率获取失败: {resp.status_code}")
            return {}
        data = resp.json().get('data') or []
        out = {}
        for x in data:
            try:
                out[x['asset']] = float(x['collateralRate'])
            except (KeyError, ValueError, TypeError):
                pass
        return out
    except Exception as e:
        print(f"⚠️ Binance 折算率错误: {e}")
        return {}

BIYI_URL = "https://biyi.tky.laozi.pro/biyi/api/strategies/list"

def get_biyi_positions():
    """从 Biyi 读 LONGSHORT 持仓，返回 {exchange: {base: pos_usd}}。内网直连、无需认证。
    ticker 如 'SNDK/USDT' 或 'CRCLB/USDT'(现货代币名)，base 取 '/' 前段原样保留；
    匹配表6/表7 时用 base 或 base+'B' 兼容(perp base vs 现货代币名)。"""
    out = {'Binance': {}, 'OKX': {}}
    try:
        # 内网服务，显式禁用代理
        r = requests.post(BIYI_URL, json={"query": "$productType like SM-PU|SS-PU"},
                          timeout=15, proxies={'http': None, 'https': None})
        if r.status_code != 200:
            print(f"⚠️ Biyi 持仓获取失败: {r.status_code}")
            return out
        # 只算永续腿(SM-PU)，忽略现货腿(SS-PU)——同一对冲仓两腿名义相等，累加会翻倍
        strat = [s for s in (r.json().get('data') or [])
                 if s.get('strategyType') == 'LONGSHORT' and s.get('productType') == 'SM-PU']
        for s in strat:
            t = s.get('ticker', '')
            if '/' not in t:
                continue
            base = t.split('/')[0]
            acct = str(s.get('accountMap') or '').lower()
            try:
                qty = float(s.get('maxPositionQty') or 0)
            except (ValueError, TypeError):
                continue
            ex = 'Binance' if 'binance' in acct else ('OKX' if ('okex' in acct or 'okx' in acct) else None)
            if ex is None:
                continue
            out[ex][base] = out[ex].get(base, 0.0) + qty
    except Exception as e:
        print(f"⚠️ Biyi 持仓错误: {e}")
    return out

def pos_of(pos_map, base):
    """表6/表7 的 perp base 在持仓里的额度：兼容 base 与 base+'B'(现货代币名)"""
    v = pos_map.get(base)
    if v is None:
        v = pos_map.get(base + 'B')
    return v

def display_with_positions(rows, n=30):
    """显示 Top n，再把排在 n 之外但【有持仓】的行强制追加(保证持仓币一定出现)"""
    top = rows[:n]
    extra = [r for r in rows[n:] if r.get('pos_usd') is not None]
    return top + extra

def append_csv(df, path):
    """追加 df 到 CSV；若已有文件的列结构与 df 不同（schema 变更），先把旧文件备份为 .bak 再按新结构重写"""
    if os.path.isfile(path):
        try:
            old_cols = pd.read_csv(path, nrows=0).columns.tolist()
        except Exception:
            old_cols = None
        if old_cols is not None and old_cols != list(df.columns):
            os.replace(path, path + '.bak')
            print(f"⚠️ {path} 列结构变更，旧文件备份为 {path}.bak")
            df.to_csv(path, mode='w', header=True, index=False, encoding='utf-8-sig')
            return
    df.to_csv(path, mode='a', header=not os.path.isfile(path), index=False, encoding='utf-8-sig')

def fetch_json(url, params=None):
    try:
        resp = requests.get(url, params=params, timeout=10, proxies=proxies)
        if resp.status_code == 200:
            return resp.json()
        return None
    except Exception as e:
        print(f"❌ 网络错误: {e}")
        return None

MAX_WORKERS = 10  # 并发请求数（过高易触发交易所限频）

def parallel_each(fn, items, workers=MAX_WORKERS):
    """并发对每个 item 执行 fn（fn 自行写入共享结果，忽略返回值）。用于逐标的请求提速。"""
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(fn, items))

# ============ 成交量获取 (7所) ============

def get_binance_data(target_stocks):
    """Binance: 24h成交量"""
    print("⏳ 正在同步 Binance...")
    url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
    data = fetch_json(url)
    results = {s: "-" for s in target_stocks}
    if not data or 'symbols' not in data:
        return results
    valid_symbols = {}
    for symbol_info in data['symbols']:
        contract_type = symbol_info.get('contractType', '')
        if contract_type in ('PERPETUAL', 'TRADIFI_PERPETUAL') and symbol_info.get('quoteAsset') == 'USDT':
            base = symbol_info['symbol'].replace('USDT', '')
            valid_symbols[base] = symbol_info['symbol']
    # 批量：一次拉全量 24hr ticker，本地映射（避免逐标的请求）
    tickers = fetch_json("https://fapi.binance.com/fapi/v1/ticker/24hr")
    if isinstance(tickers, list):
        vol_by_sym = {t.get('symbol'): t.get('quoteVolume') for t in tickers}
        for stock in target_stocks:
            sym = valid_symbols.get(stock)
            if sym and vol_by_sym.get(sym) is not None:
                results[stock] = float(vol_by_sym[sym])
    return results

def get_okx_data(target_stocks):
    """OKX: 24h成交量"""
    print("⏳ 正在同步 OKX...")
    vol_url = "https://www.okx.com/api/v5/market/tickers"
    vol_data = fetch_json(vol_url, {'instType': 'SWAP'})
    results = {s: "-" for s in target_stocks}
    if not vol_data or vol_data.get('code') != '0':
        return results
    target_set = set(target_stocks)
    for item in vol_data.get('data', []):
        inst_id = item.get('instId', '')
        if '-USDT-SWAP' in inst_id:
            base = inst_id.split('-')[0]
            if base in target_set:
                vol_qty = float(item.get('vol24h', '0'))
                vol_price = float(item.get('last', '0'))
                results[base] = vol_qty * vol_price
    return results

def get_bitget_data(target_stocks):
    """Bitget: 24h成交量"""
    print("⏳ 正在同步 Bitget...")
    inst_url = "https://api.bitget.com/api/v3/market/instruments"
    inst_data = fetch_json(inst_url, {'category': 'USDT-FUTURES'})
    results = {s: "-" for s in target_stocks}
    if not inst_data or inst_data.get('code') != '00000':
        return results
    valid_symbols = {item['baseCoin']: item['symbol'] for item in inst_data['data'] if item['baseCoin'] in target_stocks}
    if not valid_symbols:
        return results
    ticker_url = "https://api.bitget.com/api/v3/market/tickers"
    ticker_data = fetch_json(ticker_url, {'category': 'USDT-FUTURES'})
    if not ticker_data or ticker_data.get('code') != '00000':
        return results
    for item in ticker_data.get('data', []):
        base = item.get('symbol', '').replace('USDT', '')
        if base in results and results[base] == '-':
            results[base] = float(item.get('turnover24h', '0')) or 0
    return results

def get_bybit_data(target_stocks):
    """Bybit: 24h成交量（批量：一次全量 tickers）"""
    print("⏳ 正在同步 Bybit...")
    results = {s: "-" for s in target_stocks}
    data = fetch_json("https://api.bybit.com/v5/market/tickers", {'category': 'linear'})
    if not data or data.get('retCode') != 0:
        return results
    target = set(target_stocks)
    for item in data.get('result', {}).get('list', []):
        sym = item.get('symbol', '')
        if sym.endswith('USDT'):
            base = sym.replace('USDT', '')
            if base in target:
                vol = item.get('turnover24h', '0')
                results[base] = float(vol) if vol else 0
    return results

def get_mexc_data(target_stocks):
    """MEXC: 24h成交量 - 暂时跳过"""
    print("⏳ MEXC 暂时跳过...")
    return {s: "-" for s in target_stocks}

def get_gate_data(target_stocks):
    """Gate.io: 24h成交量"""
    print("⏳ 正在同步 Gate.io...")
    url = "https://api.gateio.ws/api/v4/futures/usdt/contracts"
    data = fetch_json(url)
    results = {s: "-" for s in target_stocks}
    if not data or not isinstance(data, list):
        return results
    
    valid_symbols = {}
    for item in data:
        name = item.get('name', '')
        if name.endswith('_USDT'):
            base = name.replace('_USDT', '')
            valid_symbols[base] = name
    
    for stock in target_stocks:
        if stock in valid_symbols:
            # 使用 trade_size (24h成交量)
            for item in data:
                if item.get('name') == valid_symbols[stock]:
                    results[stock] = float(item.get('trade_size', 0)) or 0
                    break
    
    return results

# ============ 合约价格获取 ============

def get_contract_prices(target_stocks):
    """获取各交易所合约价格"""
    print("⏳ 正在获取合约价格...")
    results = {s: {} for s in target_stocks}  # {token: {exchange: price}}
    
    # Binance 合约价格
    url = "https://fapi.binance.com/fapi/v1/ticker/24hr"
    data = fetch_json(url)
    if data and isinstance(data, list):
        for item in data:
            sym = item.get('symbol', '')
            if sym.endswith('USDT'):
                base = sym.replace('USDT', '')
                if base in target_stocks:
                    price = float(item.get('lastPrice', 0))  # 修复：用 lastPrice
                    if price > 0:
                        results[base]['Binance'] = price
    
    # OKX 合约价格
    url = "https://www.okx.com/api/v5/market/tickers"
    data = fetch_json(url, {'instType': 'SWAP'})
    if data and data.get('code') == '0':
        for item in data.get('data', []):
            inst_id = item.get('instId', '')
            if '-USDT-SWAP' in inst_id:
                base = inst_id.split('-')[0]
                if base in target_stocks:
                    price = float(item.get('last', 0))
                    if price > 0:
                        results[base]['OKX'] = price
    
    # Bybit 合约价格
    url = "https://api.bybit.com/v5/market/tickers"
    data = fetch_json(url, {'category': 'linear'})
    if data and data.get('retCode') == 0:
        for item in data.get('result', {}).get('list', []):
            sym = item.get('symbol', '')
            if sym.endswith('USDT'):
                base = sym.replace('USDT', '')
                if base in target_stocks:
                    price = float(item.get('lastPrice', 0))
                    if price > 0:
                        results[base]['Bybit'] = price
    
    return results

# ============ 现货价格获取 ============

def get_spot_prices(target_stocks):
    """获取现货价格 (Binance/OKX)"""
    print("⏳ 正在获取现货价格...")
    results = {}
    
    # Binance 现货
    url = "https://api.binance.com/api/v3/ticker/price"
    data = fetch_json(url)
    if data and isinstance(data, list):
        for item in data:
            sym = item.get('symbol', '')
            if sym.endswith('USDT') and not sym.startswith('USD'):
                base = sym.replace('USDT', '')
                if base in target_stocks:
                    results[base] = {'binance': float(item.get('price', 0))}
    
    # OKX 现货
    url = "https://www.okx.com/api/v5/market/tickers"
    data = fetch_json(url, {'instType': 'SPOT'})
    if data and data.get('code') == '0':
        for item in data.get('data', []):
            inst_id = item.get('instId', '')
            if '-USDT' in inst_id:
                base = inst_id.split('-')[0]
                if base in target_stocks:
                    if base not in results:
                        results[base] = {}
                    results[base]['okx'] = float(item.get('last', 0))
    
    return results

# ============ Funding Rate ============

def annualize(rate, interval_hours):
    """按真实结算周期把单期 funding rate 年化，返回百分比；周期无效则返回 None"""
    if not interval_hours or interval_hours <= 0:
        return None
    return rate * (24.0 / interval_hours) * 365 * 100

def mixed_fee_bp(ex_low, ex_high):
    """混合成交（一腿maker一腿taker）开+平共4笔的总手续费(bp)，自动选更省的分配"""
    fl, fh = FEE[ex_low], FEE[ex_high]
    per_build = min(fl['maker'] + fh['taker'], fl['taker'] + fh['maker'])
    return 2 * per_build

def get_binance_funding(target_stocks):
    """Binance Funding Rate"""
    print("⏳ 正在获取 Binance Funding...")
    url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
    data = fetch_json(url)
    results = {s: {'bp': '-', 'annualized': '-', 'next': '-'} for s in target_stocks}
    if not data or 'symbols' not in data:
        return results
    
    # 构建 symbol 映射
    valid_symbols = {}
    for symbol_info in data['symbols']:
        contract_type = symbol_info.get('contractType', '')
        if contract_type in ('PERPETUAL', 'TRADIFI_PERPETUAL') and symbol_info.get('quoteAsset') == 'USDT':
            base = symbol_info['symbol'].replace('USDT', '')
            valid_symbols[base] = symbol_info['symbol']

    # 拉取真实结算周期：fundingInfo 只列出有记录的 symbol，未列出的按 8h 默认
    interval_map = {}
    info = fetch_json("https://fapi.binance.com/fapi/v1/fundingInfo")
    if isinstance(info, list):
        for it in info:
            interval_map[it.get('symbol')] = it.get('fundingIntervalHours')

    # 批量：一次全量 premiumIndex，本地映射（避免逐标的请求）
    premium_map = {}
    premium = fetch_json("https://fapi.binance.com/fapi/v1/premiumIndex")
    if isinstance(premium, list):
        premium_map = {p.get('symbol'): p for p in premium}

    for stock in target_stocks:
        if stock not in valid_symbols:
            continue
        sym = valid_symbols[stock]
        premium_data = premium_map.get(sym)
        if premium_data:
            try:
                rate = float(premium_data.get('lastFundingRate', 0))
                next_time = int(premium_data.get('nextFundingTime', 0))
                interval = interval_map.get(sym, 8)  # 未列出默认 8h
                ann = annualize(rate, interval)
                results[stock]['bp'] = round(rate * 10000, 2)
                results[stock]['annualized'] = round(ann, 2) if ann is not None else '-'
                results[stock]['next'] = datetime.fromtimestamp(next_time/1000).strftime('%H:%M') if next_time > 0 else '-'
            except:
                pass
    return results

FUNDING_HISTORY_FILE = "funding_history.json"

def load_funding_history():
    """加载本地持久化历史: {exchange: {token: {ts_int: rate}}}；不存在或损坏返回 {}"""
    if not os.path.isfile(FUNDING_HISTORY_FILE):
        return {}
    try:
        with open(FUNDING_HISTORY_FILE, encoding='utf-8') as f:
            raw = json.load(f)
        return {ex: {tok: {int(ts): r for ts, r in s.items()} for tok, s in toks.items()}
                for ex, toks in raw.items()}
    except Exception as e:
        print(f"⚠️ 读取历史文件失败，按空处理: {e}")
        return {}

def save_funding_history(hist):
    """保存 {exchange: {token: {ts: rate}}}（JSON 会把 int key 转字符串，读取时转回）"""
    try:
        with open(FUNDING_HISTORY_FILE, 'w', encoding='utf-8') as f:
            json.dump(hist, f)
    except Exception as e:
        print(f"❌ 保存历史文件失败: {e}")

def discover_tradfi_tokens():
    """从 Binance 自动发现所有股票/大宗/指数永续(contractType=TRADIFI_PERPETUAL 且在交易)，返回 base 列表；失败返回 []"""
    data = fetch_json("https://fapi.binance.com/fapi/v1/exchangeInfo")
    tokens = []
    if data and 'symbols' in data:
        for s in data['symbols']:
            if (s.get('contractType') == 'TRADIFI_PERPETUAL'
                    and s.get('quoteAsset') == 'USDT'
                    and s.get('status') == 'TRADING'):
                tokens.append(s['symbol'].replace('USDT', ''))
    return sorted(set(tokens))

def get_binance_funding_history(target_stocks, existing=None, days=HISTORY_DAYS):
    """Binance 增量历史 funding: 有本地则从最新 ts 之后拉，否则拉最近 days 天。返回 {token: {ts: rate}}"""
    print("⏳ 正在获取 Binance Funding 历史(增量)...")
    existing = existing or {}
    results = {s: dict(existing.get(s, {})) for s in target_stocks}
    default_since = int((time.time() - days * 86400) * 1000)

    def _one(tok):
        cur = results[tok]
        since = max(cur) + 1 if cur else default_since
        data = fetch_json("https://fapi.binance.com/fapi/v1/fundingRate",
                          {'symbol': f'{tok}USDT', 'startTime': since, 'limit': 1000})
        if isinstance(data, list):
            for it in data:
                try:
                    cur[int(it['fundingTime'])] = float(it['fundingRate'])
                except (KeyError, ValueError, TypeError):
                    pass
    parallel_each(_one, target_stocks)
    return results

def get_okx_funding_history(target_stocks, existing=None, days=HISTORY_DAYS):
    """OKX 增量历史 funding: 有本地用 before 拉更新的记录，否则用 after 往前翻页拉满 days。"""
    print("⏳ 正在获取 OKX Funding 历史(增量)...")
    existing = existing or {}
    results = {s: dict(existing.get(s, {})) for s in target_stocks}
    url = "https://www.okx.com/api/v5/public/funding-rate-history"
    cutoff = (time.time() - days * 86400) * 1000

    def _absorb(cur, rows):
        for it in rows:
            try:
                rate = it.get('realizedRate')
                if rate in (None, ''):
                    rate = it.get('fundingRate')
                cur[int(it['fundingTime'])] = float(rate)
            except (KeyError, ValueError, TypeError):
                pass

    def _one(tok):
        cur = results[tok]
        if cur:
            # 增量：before=最新ts 拉更新的记录(倒序)，多页直到不足100
            cursor = max(cur)
            for _ in range(6):
                data = fetch_json(url, {'instId': f'{tok}-USDT-SWAP', 'limit': 100, 'before': cursor})
                if not (data and data.get('code') == '0' and data.get('data')):
                    break
                rows = data['data']
                _absorb(cur, rows)
                cursor = max(int(r['fundingTime']) for r in rows)
                if len(rows) < 100:
                    break
        else:
            # 首次：after 往前翻页拉满 days
            after = None
            for _ in range(6):
                params = {'instId': f'{tok}-USDT-SWAP', 'limit': 100}
                if after is not None:
                    params['after'] = after
                data = fetch_json(url, params)
                if not (data and data.get('code') == '0' and data.get('data')):
                    break
                rows = data['data']
                _absorb(cur, rows)
                oldest = int(rows[-1]['fundingTime'])
                if oldest <= cutoff or len(rows) < 100:
                    break
                after = oldest  # OKX after 返回严格早于该 ts 的记录
    parallel_each(_one, target_stocks)
    return results

def get_bybit_funding_history(target_stocks, existing=None, days=HISTORY_DAYS):
    """Bybit 增量历史 funding: 有本地则 startTime+endTime 拉新增(必须成对)，否则拉最近200条。"""
    print("⏳ 正在获取 Bybit Funding 历史(增量)...")
    existing = existing or {}
    results = {s: dict(existing.get(s, {})) for s in target_stocks}
    now_ms = int(time.time() * 1000)

    def _one(tok):
        cur = results[tok]
        if cur:
            params = {'category': 'linear', 'symbol': f'{tok}USDT',
                      'startTime': max(cur) + 1, 'endTime': now_ms, 'limit': 200}
        else:
            params = {'category': 'linear', 'symbol': f'{tok}USDT', 'limit': 200}
        data = fetch_json("https://api.bybit.com/v5/market/funding/history", params)
        if data and data.get('retCode') == 0:
            for it in data.get('result', {}).get('list', []):
                try:
                    cur[int(it['fundingRateTimestamp'])] = float(it['fundingRate'])
                except (KeyError, ValueError, TypeError):
                    pass
    parallel_each(_one, target_stocks)
    return results

# ============ 单所 Funding 画像（表4） ============

def infer_interval_hours(series):
    """由相邻时间戳的众数(最常见间隔)推断结算周期(小时)，对个别异常小间隔鲁棒；无法推断回退 8h"""
    ts = sorted(series)
    if len(ts) < 2:
        return 8.0
    diffs = [ts[i+1] - ts[i] for i in range(len(ts)-1) if ts[i+1] > ts[i]]
    if not diffs:
        return 8.0
    common = max(set(diffs), key=diffs.count)  # 众数=真实结算周期
    return common / 3600000.0

def window_apr(series, cutoff_ms, interval_hours):
    """cutoff_ms 之后历史 funding 均值年化(%)，窗口内无数据返回 None"""
    vals = [r for ts, r in series.items() if ts >= cutoff_ms]
    if not vals:
        return None
    return annualize(sum(vals) / len(vals), interval_hours)

def window_std_apr(series, cutoff_ms, interval_hours):
    """cutoff_ms 之后 funding 的年化标准差(%)，与 apr 同口径(σ×每年期数×100)；<2点或周期无效返回 None"""
    vals = [r for ts, r in series.items() if ts >= cutoff_ms]
    if len(vals) < 2 or not interval_hours or interval_hours <= 0:
        return None
    mean = sum(vals) / len(vals)
    std = (sum((v - mean) ** 2 for v in vals) / len(vals)) ** 0.5
    return std * (24.0 / interval_hours) * 365 * 100

def get_open_interest_usd(target_stocks, contract_prices):
    """各所未平仓合约 USD 名义: {token: {exchange: oi_usd}}"""
    print("⏳ 正在获取 OI...")
    results = {s: {} for s in target_stocks}
    target = set(target_stocks)

    # Binance: 无批量 OI 接口，逐标的并发 openInterest(合约数量) × 合约价
    def _bn_oi(tok):
        d = fetch_json("https://fapi.binance.com/fapi/v1/openInterest", {'symbol': f'{tok}USDT'})
        if d and 'openInterest' in d:
            try:
                oi = float(d['openInterest'])
                price = contract_prices.get(tok, {}).get('Binance', 0)
                if oi > 0 and price > 0:
                    results[tok]['Binance'] = oi * price
            except (ValueError, TypeError):
                pass
    parallel_each(_bn_oi, target_stocks)

    # OKX: 一次全量 open-interest(instType=SWAP)，直接 oiUsd
    d = fetch_json("https://www.okx.com/api/v5/public/open-interest", {'instType': 'SWAP'})
    if d and d.get('code') == '0':
        for it in d.get('data', []):
            inst = it.get('instId', '')
            if inst.endswith('-USDT-SWAP'):
                base = inst.split('-')[0]
                if base in target:
                    try:
                        results[base]['OKX'] = float(it['oiUsd'])
                    except (KeyError, ValueError, TypeError):
                        pass

    # Bybit: 一次全量 tickers 的 openInterestValue(USD)
    d = fetch_json("https://api.bybit.com/v5/market/tickers", {'category': 'linear'})
    if d and d.get('retCode') == 0:
        for it in d.get('result', {}).get('list', []):
            sym = it.get('symbol', '')
            if sym.endswith('USDT'):
                base = sym.replace('USDT', '')
                if base in target:
                    try:
                        results[base]['Bybit'] = float(it['openInterestValue'])
                    except (KeyError, ValueError, TypeError):
                        pass
    return results

def get_spot_availability(target_stocks):
    """各所是否有该标的现货交易对: {exchange: set(base)}，用于表4标注可否同所期现对冲"""
    print("⏳ 正在获取现货可用性...")
    target = set(target_stocks)
    avail = {'Binance': set(), 'OKX': set(), 'Bybit': set()}
    d = fetch_json("https://api.binance.com/api/v3/ticker/price")
    if isinstance(d, list):
        for it in d:
            sym = it.get('symbol', '')
            if sym.endswith('USDT'):
                base = sym.replace('USDT', '')
                if base in target:
                    avail['Binance'].add(base)
    d = fetch_json("https://www.okx.com/api/v5/market/tickers", {'instType': 'SPOT'})
    if d and d.get('code') == '0':
        for it in d.get('data', []):
            inst = it.get('instId', '')
            if inst.endswith('-USDT'):
                base = inst.split('-')[0]
                if base in target:
                    avail['OKX'].add(base)
    d = fetch_json("https://api.bybit.com/v5/market/tickers", {'category': 'spot'})
    if d and d.get('retCode') == 0:
        for it in d.get('result', {}).get('list', []):
            sym = it.get('symbol', '')
            if sym.endswith('USDT'):
                base = sym.replace('USDT', '')
                if base in target:
                    avail['Bybit'].add(base)
    return avail

def build_funding_profile_rows(tokens, hist, fr_map, oi_usd, spot_avail, current_time, now_ms):
    """组装表4行：每所每标的一行。fr_map={ex:{tok:{'bp':..}}}, hist={ex:{tok:{ts:rate}}}, spot_avail={ex:set(base)}"""
    rows = []
    day_ms = 86400000
    for ex in ('Binance', 'OKX', 'Bybit'):
        ex_rows = []
        for tok in tokens:
            series = hist.get(ex, {}).get(tok, {})
            if not series:
                continue
            interval = infer_interval_hours(series)
            bp = fr_map.get(ex, {}).get(tok, {}).get('bp', '-')
            apr3 = window_apr(series, now_ms - 3 * day_ms, interval)
            apr7 = window_apr(series, now_ms - 7 * day_ms, interval)
            apr30 = window_apr(series, now_ms - 30 * day_ms, interval)
            std7 = window_std_apr(series, now_ms - 7 * day_ms, interval)
            oi = oi_usd.get(tok, {}).get(ex)
            settle = datetime.fromtimestamp(max(series) / 1000).strftime('%m-%d %H:%M')
            ex_rows.append({
                'exchange': ex, 'symbol': tok, 'timestamp': current_time,
                'settle_time': settle,
                'int': round(interval, 1),
                'funding_bp': bp if bp != '-' else None,
                '3d_apr%': round(apr3, 2) if apr3 is not None else None,
                '7d_apr%': round(apr7, 2) if apr7 is not None else None,
                '30d_apr%': round(apr30, 2) if apr30 is not None else None,
                'std_7d_y%': round(std7, 2) if std7 is not None else None,
                'OI_usd': round(oi, 0) if oi is not None else None,
                'spot': 'Y' if tok in spot_avail.get(ex, set()) else 'N',
            })
        # 每个交易所组内独立排序：按 7d 年化降序，无数据排最后；取 Top 30
        ex_rows.sort(key=lambda r: (r['7d_apr%'] is None, -(r['7d_apr%'] or 0)))
        rows.extend(ex_rows[:30])
    return rows

def cross_pair_metrics(tok, ex_a, ex_b, series_a, series_b, price_a, price_b, oi_a, oi_b, curr_a, curr_b, vol_a, vol_b, now_ms):
    """跨所净 funding 套利指标（高7d年化所做空、低所做多）；7d为主、30d看持续。数据不足返回 None"""
    day = 86400000
    if not series_a or not series_b:
        return None
    ia = infer_interval_hours(series_a)
    ib = infer_interval_hours(series_b)
    a7 = window_apr(series_a, now_ms - 7 * day, ia)
    b7 = window_apr(series_b, now_ms - 7 * day, ib)
    if a7 is None or b7 is None:
        return None
    a30 = window_apr(series_a, now_ms - 30 * day, ia)
    b30 = window_apr(series_b, now_ms - 30 * day, ib)
    a3 = window_apr(series_a, now_ms - 3 * day, ia)
    b3 = window_apr(series_b, now_ms - 3 * day, ib)
    # 高 7d 年化所做空(收 funding)，低所做多(付 funding)
    if a7 >= b7:
        short_ex, short_s, short_p, s7, s30, s3, short_oi, curr_short, short_vol = ex_a, series_a, price_a, a7, a30, a3, oi_a, curr_a, vol_a
        long_ex, long_s, long_p, l7, l30, l3, long_oi, curr_long, long_vol = ex_b, series_b, price_b, b7, b30, b3, oi_b, curr_b, vol_b
    else:
        short_ex, short_s, short_p, s7, s30, s3, short_oi, curr_short, short_vol = ex_b, series_b, price_b, b7, b30, b3, oi_b, curr_b, vol_b
        long_ex, long_s, long_p, l7, l30, l3, long_oi, curr_long, long_vol = ex_a, series_a, price_a, a7, a30, a3, oi_a, curr_a, vol_a
    f7 = s7 - l7                                                    # 7d funding 净年化
    f3 = (s3 - l3) if (s3 is not None and l3 is not None) else None  # 3d funding 净年化
    net30 = (s30 - l30) if (s30 is not None and l30 is not None) else None
    # 符号一致率：7d 窗口内对齐时间戳交集，逐期 (空腿 − 多腿) > 0 占比
    cutoff = now_ms - 7 * day
    common = sorted(set(t for t in short_s if t >= cutoff) & set(t for t in long_s if t >= cutoff))
    consistency = (sum(1 for t in common if short_s[t] - long_s[t] > 0) / len(common)) if common else 0.0
    fee_bp = mixed_fee_bp(long_ex, short_ex)
    spread_bp = (short_p - long_p) / short_p * 10000 if (short_p and long_p and short_p > 0) else 0.0
    if abs(spread_bp) > MAX_SPREAD_BP:  # 盘前/pre-IPO 永续两所价差极大、无法真实对冲，剔除
        return None
    # 当前价差按持仓期平仓年化(%)：spread_bp/1e4 × (365/天数) × 100
    sp3 = spread_bp * 365 / 300   # 3 天持仓
    sp7 = spread_bp * 365 / 700   # 7 天持仓
    net_3d = (f3 + sp3) if f3 is not None else None   # 3d 总收益年化 = funding + spread
    net_7d = f7 + sp7                                 # 7d 总收益年化
    # 回本天数：一次性(手续费 − 开仓有利价差) / 每天 funding 收益(用 7d funding)
    onetime = fee_bp - spread_bp
    daily_bp = f7 * 100 / 365
    if daily_bp <= 0:
        breakeven = float('inf')
    elif onetime <= 0:
        breakeven = 0.0
    else:
        breakeven = onetime / daily_bp
    ois = [x for x in (short_oi, long_oi) if x is not None]
    min_oi = min(ois) if ois else None
    # 判定看 funding(可持续)，spread 是一次性不作持续依据
    if f7 <= 0:
        verdict, reason = '⏸', 'funding≤0'
    elif consistency < SIGN_CONSISTENCY_MIN:
        verdict, reason = '⏸', 'funding不稳'
    elif breakeven > BREAKEVEN_DAYS_MAX:
        verdict, reason = '⏸', '回本慢'
    else:
        verdict, reason = '✅', ''
    # 当前是否可进：当期 funding 空腿(做空所) − 多腿(做多所) > 0
    if isinstance(curr_short, (int, float)) and isinstance(curr_long, (int, float)):
        curr_net = curr_short - curr_long
        enter = '✅' if curr_net > 0 else '✗'
    else:
        curr_net, enter = None, '?'
    return {
        'symbol': tok, 'long_ex': long_ex, 'short_ex': short_ex,
        'curr_net_bp': round(curr_net, 2) if curr_net is not None else None,
        'enter': enter,
        '3d_funding': round(f3, 2) if f3 is not None else None,
        '3d_spread': round(sp3, 2),
        'net_3d': round(net_3d, 2) if net_3d is not None else None,
        '7d_funding': round(f7, 2),
        '7d_spread': round(sp7, 2),
        'net_7d': round(net_7d, 2),
        'net_30d': round(net30, 2) if net30 is not None else None,
        'consistency': round(consistency, 2),
        'fee_bp': round(fee_bp, 2), 'spread_bp': round(spread_bp, 2),
        'breakeven_d': round(breakeven, 1) if breakeven != float('inf') else None,
        'min_oi': round(min_oi, 0) if min_oi is not None else None,
        'long_vol': round(long_vol, 0) if isinstance(long_vol, (int, float)) else None,
        'short_vol': round(short_vol, 0) if isinstance(short_vol, (int, float)) else None,
        'verdict': verdict, 'reason': reason,
    }

def build_cross_arb_rows(tokens, hist, contract_prices, oi_usd, fr_map, vol_map, now_ms):
    """每标的在三所里两两配对，取 7d 净差最高的一对；按 net_7d 降序。fr_map 当期 funding；vol_map 各所 24h 成交量"""
    exs = ('Binance', 'OKX', 'Bybit')
    rows = []
    for tok in tokens:
        best = None
        for i in range(len(exs)):
            for j in range(i + 1, len(exs)):
                ea, eb = exs[i], exs[j]
                sa = hist.get(ea, {}).get(tok, {})
                sb = hist.get(eb, {}).get(tok, {})
                if not sa or not sb:
                    continue
                pa = contract_prices.get(tok, {}).get(ea, 0)
                pb = contract_prices.get(tok, {}).get(eb, 0)
                oa = oi_usd.get(tok, {}).get(ea)
                ob = oi_usd.get(tok, {}).get(eb)
                ca = fr_map.get(ea, {}).get(tok, {}).get('bp', '-')
                cb = fr_map.get(eb, {}).get(tok, {}).get('bp', '-')
                ca = ca if isinstance(ca, (int, float)) else None
                cb = cb if isinstance(cb, (int, float)) else None
                va = vol_map.get(ea, {}).get(tok)
                vb = vol_map.get(eb, {}).get(tok)
                va = va if isinstance(va, (int, float)) else None
                vb = vb if isinstance(vb, (int, float)) else None
                r = cross_pair_metrics(tok, ea, eb, sa, sb, pa, pb, oa, ob, ca, cb, va, vb, now_ms)
                if r and (best is None or r['net_7d'] > best['net_7d']):
                    best = r
        if best:
            rows.append(best)
    rows.sort(key=lambda r: -(r['net_7d'] or 0))
    return rows

def build_binance_basis_rows(tokens, hist, contract_prices, vol_bn, fr_map, pos_map, now_ms):
    """Binance 有现货(XXXBUSDT)的股票永续：当期funding + 3d/7d/30d funding 年化 + 期现基差 + 现货/合约24h成交量 + 持仓；按 7dF 降序"""
    spot = fetch_json("https://api.binance.com/api/v3/ticker/24hr")
    spot_map = {}  # base -> (现货价, 现货24h成交额USDT)；现货 symbol = base + 'B' + 'USDT'
    if isinstance(spot, list):
        target = set(tokens)
        for it in spot:
            sym = it.get('symbol', '')
            if sym.endswith('BUSDT'):
                base = sym[:-5]  # 去掉 BUSDT 后缀
                if base in target:
                    try:
                        spot_map[base] = (float(it['lastPrice']), float(it['quoteVolume']))
                    except (KeyError, ValueError, TypeError):
                        pass
    collat = get_binance_collateral_rate()  # {asset: 折算率}；未配 API key 则为空
    day = 86400000
    rows = []
    for tok in tokens:
        if tok not in spot_map:   # 只列有现货的
            continue
        series = hist.get('Binance', {}).get(tok, {})
        if not series:
            continue
        iv = infer_interval_hours(series)
        f3 = window_apr(series, now_ms - 3 * day, iv)
        f7 = window_apr(series, now_ms - 7 * day, iv)
        f30 = window_apr(series, now_ms - 30 * day, iv)
        perp = contract_prices.get(tok, {}).get('Binance', 0)
        spot_p, spot_vol = spot_map[tok]
        pv = vol_bn.get(tok)
        perp_vol = pv if isinstance(pv, (int, float)) else None
        cb = fr_map.get('Binance', {}).get(tok, {}).get('bp', '-')
        funding_bp = cb if isinstance(cb, (int, float)) else None
        spread = (perp - spot_p) / perp * 10000 if perp and perp > 0 else None
        disc = collat.get(tok + 'B', collat.get(tok))  # 现货代币名 XXXB 优先，回退 base
        pos = pos_of(pos_map, tok)  # 持仓额(USD)，无则 None
        rows.append({
            'symbol': tok,
            'funding_bp': funding_bp,
            'discount': disc,
            '3dF': round(f3, 2) if f3 is not None else None,
            '7dF': round(f7, 2) if f7 is not None else None,
            '30dF': round(f30, 2) if f30 is not None else None,
            'perp': round(perp, 4) if perp else None,
            'spot': round(spot_p, 4),
            'spread_bp': round(spread, 2) if spread is not None else None,
            'perp_vol': round(perp_vol, 0) if perp_vol is not None else None,
            'spot_vol': round(spot_vol, 0),
            'pos_usd': round(pos, 0) if pos is not None else None,
        })
    rows.sort(key=lambda r: (r['7dF'] is None, -(r['7dF'] or 0)))
    return rows

def get_okx_discount(bases, spot_map, amount_usd=OKX_DISCOUNT_AMOUNT_USD):
    """OKX 保证金折算率：档位 minAmt/maxAmt 单位为【币数量】。
    qty = amount_usd / 现货价，按 qty 落档，取覆盖到的最低档 discountRate。无数据/无价返回 0。
    例：SOXL 现货 153，5万U → qty≈326 → 落 tier4(272~442) → 0.5"""
    result = {}

    url = "https://www.okx.com/api/v5/public/discount-rate-interest-free-quota"

    def one(base):
        # OKX 该接口限频严格(50011)，重试退避
        d = None
        for attempt in range(5):
            d = fetch_json(url, {'ccy': 'X' + base})
            if d and d.get('code') == '0':
                break
            time.sleep(0.4 * (attempt + 1))
        rate = 0.0
        sp = spot_map.get(base, (0, 0))[0]
        if d and d.get('code') == '0' and d.get('data') and sp and sp > 0:
            details = d['data'][0].get('details') or []
            if details:
                qty = amount_usd / sp
                chosen = details[0]  # 默认第一档
                for t in details:  # qty(币数) 跨过该档 minAmt 则覆盖到此档(折算率更低)
                    try:
                        if qty > float(t['minAmt']):
                            chosen = t
                        else:
                            break
                    except (KeyError, ValueError, TypeError):
                        break
                try:
                    rate = float(chosen['discountRate'])
                except (KeyError, ValueError, TypeError):
                    pass
        result[base] = rate
    parallel_each(one, bases, workers=3)  # 低并发避免限频
    return result

def build_okx_basis_rows(pos_map, now_ms):
    """OKX 代币化股票(X前缀现货 + 永续)：当期funding + 3d/7d/30d年化 + 期现基差 + 成交量 + 折算率 + 持仓；按 7dF 降序"""
    # 现货(X前缀 -USDT)：volCcy24h 已是 USDT 成交额
    sp = fetch_json("https://www.okx.com/api/v5/market/tickers", {'instType': 'SPOT'})
    spot_map = {}
    if sp and sp.get('code') == '0':
        for x in sp.get('data', []):
            i = x.get('instId', '')
            if i.startswith('X') and i.endswith('-USDT'):
                try:
                    spot_map[i[1:-5]] = (float(x['last']), float(x['volCcy24h']))
                except (KeyError, ValueError, TypeError):
                    pass
    # 永续：volCcy24h(币数) × last = USDT 成交额
    sw = fetch_json("https://www.okx.com/api/v5/market/tickers", {'instType': 'SWAP'})
    swap_map = {}
    if sw and sw.get('code') == '0':
        for x in sw.get('data', []):
            i = x.get('instId', '')
            if i.endswith('-USDT-SWAP'):
                try:
                    last = float(x['last'])
                    swap_map[i.split('-')[0]] = (last, float(x['volCcy24h']) * last)
                except (KeyError, ValueError, TypeError):
                    pass
    bases = [b for b in spot_map if b in swap_map]
    hist_okx = get_okx_funding_history(bases)
    fr_okx = get_okx_funding(bases)
    disc = get_okx_discount(bases, spot_map)
    day = 86400000
    rows = []
    for base in bases:
        series = hist_okx.get(base, {})
        if not series:
            continue
        iv = infer_interval_hours(series)
        f3 = window_apr(series, now_ms - 3 * day, iv)
        f7 = window_apr(series, now_ms - 7 * day, iv)
        f30 = window_apr(series, now_ms - 30 * day, iv)
        perp_p, perp_vol = swap_map[base]
        spot_p, spot_vol = spot_map[base]
        spread = (perp_p - spot_p) / perp_p * 10000 if perp_p > 0 else None
        cb = fr_okx.get(base, {}).get('bp', '-')
        fund = cb if isinstance(cb, (int, float)) else None
        rows.append({
            'symbol': base,
            'funding_bp': fund,
            '3dF': round(f3, 2) if f3 is not None else None,
            '7dF': round(f7, 2) if f7 is not None else None,
            '30dF': round(f30, 2) if f30 is not None else None,
            'perp': round(perp_p, 4),
            'spot': round(spot_p, 4),
            'spread_bp': round(spread, 2) if spread is not None else None,
            'perp_vol': round(perp_vol, 0),
            'spot_vol': round(spot_vol, 0),
            'discount': disc.get(base, 0.0),
            'pos_usd': (lambda p: round(p, 0) if p is not None else None)(pos_of(pos_map, base)),
        })
    rows.sort(key=lambda r: (r['7dF'] is None, -(r['7dF'] or 0)))
    return rows

def get_okx_funding(target_stocks):
    """OKX Funding Rate"""
    print("⏳ 正在获取 OKX Funding...")
    results = {s: {'bp': '-', 'annualized': '-'} for s in target_stocks}
    target_set = set(target_stocks)
    
    # OKX 无批量 funding 接口，逐标的并发查询
    def _one(tok):
        url = f"https://www.okx.com/api/v5/public/funding-rate?instId={tok}-USDT-SWAP"
        data = fetch_json(url)
        if data and data.get('code') == '0' and data.get('data'):
            try:
                fr = data['data'][0]
                funding_rate = float(fr.get('fundingRate', 0))
                # 真实结算周期 = 下次结算 - 本次结算
                ft = int(fr.get('fundingTime', 0))
                nt = int(fr.get('nextFundingTime', 0))
                interval = (nt - ft) / 3600000 if nt > ft > 0 else 8
                ann = annualize(funding_rate, interval)
                results[tok]['bp'] = round(funding_rate * 10000, 2)
                results[tok]['annualized'] = round(ann, 2) if ann is not None else '-'
            except:
                pass
    parallel_each(_one, target_stocks)
    return results


def get_bybit_funding(target_stocks):
    """Bybit Funding Rate"""
    print("⏳ 正在获取 Bybit Funding...")
    results = {s: {'bp': '-', 'annualized': '-'} for s in target_stocks}

    # 批量拉结算周期（fundingInterval 单位：分钟）
    interval_map = {}
    info = fetch_json("https://api.bybit.com/v5/market/instruments-info", {'category': 'linear', 'limit': 1000})
    if info and info.get('retCode') == 0:
        for it in info.get('result', {}).get('list', []):
            fi = it.get('fundingInterval')
            if fi:
                interval_map[it['symbol']] = fi / 60.0

    # 批量：一次全量 tickers（含 fundingRate），本地过滤
    data = fetch_json("https://api.bybit.com/v5/market/tickers", {'category': 'linear'})
    target = set(target_stocks)
    if data and data.get('retCode') == 0:
        for it in data.get('result', {}).get('list', []):
            sym = it.get('symbol', '')
            if not sym.endswith('USDT'):
                continue
            base = sym.replace('USDT', '')
            if base not in target:
                continue
            try:
                funding_rate = float(it.get('fundingRate', 0))
                interval = interval_map.get(sym, 8)  # 未取到默认 8h
                ann = annualize(funding_rate, interval)
                results[base]['bp'] = round(funding_rate * 10000, 2)
                results[base]['annualized'] = round(ann, 2) if ann is not None else '-'
            except (ValueError, TypeError):
                pass
    return results

def main():
    current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"\n🕒 采集时间：{current_time}\n")
    
    tokens = discover_tradfi_tokens()
    if tokens:
        print(f"🔍 自动发现 {len(tokens)} 个股票/大宗/指数永续 (Binance TRADIFI)")
    else:
        tokens = TARGET_TOKENS
        print("⚠️ 自动发现失败，回退手动名单 TARGET_TOKENS")
    
    # 获取7所成交量
    vol_bn = get_binance_data(tokens)
    vol_ok = get_okx_data(tokens)
    vol_bg = get_bitget_data(tokens)
    vol_bb = get_bybit_data(tokens)
    vol_mx = get_mexc_data(tokens)
    vol_gt = get_gate_data(tokens)
    
    # 获取现货价格
    
    # 获取 Funding Rate
    fr_bn = get_binance_funding(tokens)
    fr_ok = get_okx_funding(tokens)
    fr_bb = get_bybit_funding(tokens)
    
    # 获取合约价格
    contract_prices = get_contract_prices(tokens)
    
    print("\n" + "="*80)
    print("📊 表1: 24h成交量 (USDT)")
    print("="*80)
    header = f"{'Symbol':<8} {'Binance':>12} {'OKX':>12} {'Bitget':>12} {'Bybit':>12} {'MEXC':>12} {'Gate':>12}"
    print(header)
    print("-"*80)
    
    rows_vol = []
    for tok in tokens:
        v1 = vol_bn.get(tok, '-')
        v2 = vol_ok.get(tok, '-')
        v3 = vol_bg.get(tok, '-')
        v4 = vol_bb.get(tok, '-')
        v5 = vol_mx.get(tok, '-')
        v6 = vol_gt.get(tok, '-')
        
        # 跳过全是 - 的
        if v1 == v2 == v3 == v4 == v5 == v6 == '-':
            continue
            
        row = {
            'Symbol': tok,
            'Binance': format_num(v1) if v1 != '-' else '-',
            'OKX': format_num(v2) if v2 != '-' else '-',
            'Bitget': format_num(v3) if v3 != '-' else '-',
            'Bybit': format_num(v4) if v4 != '-' else '-',
            'MEXC': format_num(v5) if v5 != '-' else '-',
            'Gate': format_num(v6) if v6 != '-' else '-'
        }
        rows_vol.append(row)
        print(f"{tok:<8} {format_num(v1) if v1 != '-' else '-':>12} {format_num(v2) if v2 != '-' else '-':>12} {format_num(v3) if v3 != '-' else '-':>12} {format_num(v4) if v4 != '-' else '-':>12} {format_num(v5) if v5 != '-' else '-':>12} {format_num(v6) if v6 != '-' else '-':>12}")
    
    # ============ 表4: 单所 Funding 画像 ============
    now_ms = time.time() * 1000
    hist_prev = load_funding_history()
    hist = {
        'Binance': get_binance_funding_history(tokens, hist_prev.get('Binance')),
        'OKX': get_okx_funding_history(tokens, hist_prev.get('OKX')),
        'Bybit': get_bybit_funding_history(tokens, hist_prev.get('Bybit')),
    }
    save_funding_history(hist)
    oi_usd = get_open_interest_usd(tokens, contract_prices)
    spot_avail = get_spot_availability(tokens)
    fr_map = {'Binance': fr_bn, 'OKX': fr_ok, 'Bybit': fr_bb}
    rows_profile = build_funding_profile_rows(tokens, hist, fr_map, oi_usd, spot_avail, current_time, now_ms)

    date_str = current_time[:10]
    for ex in ('Binance', 'OKX', 'Bybit'):
        ex_rows = [r for r in rows_profile if r['exchange'] == ex]
        if not ex_rows:
            continue
        print(f"\nFunding Top{len(ex_rows)} ({EX_DISPLAY[ex]}) — {date_str}")
        print(f"{'exchange':<11}{'symbol':<14}{'timestamp':<13}{'int':>4}{'funding(bp)':>12}"
              f"{'3d_apr%':>9}{'7d_apr%':>9}{'30d_apr%':>10}{'std_7d_y%':>11}{'OI':>12}{'spot':>5}")
        print("-" * 110)
        for r in ex_rows:
            fb = f"{r['funding_bp']:g}" if isinstance(r['funding_bp'], (int, float)) else '-'
            iv = f"{r['int']:g}h"
            print(f"{EX_DISPLAY[ex]:<11}{r['symbol']+'/USDT':<14}{r['settle_time']:<13}{iv:>4}"
                  f"{fb:>12}{fmt_pct(r['3d_apr%']):>9}{fmt_pct(r['7d_apr%']):>9}"
                  f"{fmt_pct(r['30d_apr%']):>10}{fmt_pct(r['std_7d_y%']):>11}{fmt_oi(r['OI_usd']):>12}{r['spot']:>5}")

    # ============ 表5: 跨所净 Funding 套利 (持仓视角) ============
    vol_map = {'Binance': vol_bn, 'OKX': vol_ok, 'Bybit': vol_bb}
    cross_arb = build_cross_arb_rows(tokens, hist, contract_prices, oi_usd, fr_map, vol_map, now_ms)
    print(f"\n跨所净 Funding 套利 Top{min(30, len(cross_arb))} — {date_str}  (高FR所做空/低FR所做多)")
    print(f"{'symbol':<24}{'做多所':<18}{'做空所':<18}"
          f"{'3d_fund':>16}{'3d_sprd':>16}{'net_3d':>16}{'7d_fund':>16}{'7d_sprd':>16}{'net_7d':>16}"
          f"{'net_30d':>16}{'一致率':>14}{'当前净bp':>13}{'可进':>8}{'费bp':>11}{'回本d':>11}{'minOI':>16}"
          f"{'多所24hVol':>16}{'空所24hVol':>16}{'判定':>20}")
    print("-" * 320)
    for r in cross_arb[:30]:
        be = '-' if r['breakeven_d'] is None else f"{r['breakeven_d']:g}"
        tag = r['verdict'] + (f"({r['reason']})" if r['reason'] else '')
        cn = '-' if r['curr_net_bp'] is None else f"{r['curr_net_bp']:g}"
        print(f"{r['symbol']+'/USDT':<24}{EX_DISPLAY[r['long_ex']]:<18}{EX_DISPLAY[r['short_ex']]:<18}"
              f"{fmt_pct(r['3d_funding']):>16}{fmt_pct(r['3d_spread']):>16}{fmt_pct(r['net_3d']):>16}"
              f"{fmt_pct(r['7d_funding']):>16}{fmt_pct(r['7d_spread']):>16}{fmt_pct(r['net_7d']):>16}"
              f"{fmt_pct(r['net_30d']):>16}{r['consistency']*100:>13.0f}%{cn:>13}{r['enter']:>8}"
              f"{r['fee_bp']:>11.1f}{be:>11}{fmt_oi(r['min_oi']):>16}"
              f"{fmt_mio(r['long_vol']):>16}{fmt_mio(r['short_vol']):>16}{tag:>20}")

    # 从 Biyi 拉持仓(内网)，表6/表7 用于标注+保证显示
    biyi_pos = get_biyi_positions()

    # ============ 表6: Binance 股票永续 Funding + 期现基差 ============
    binance_basis = build_binance_basis_rows(tokens, hist, contract_prices, vol_bn, fr_map, biyi_pos['Binance'], now_ms)
    b6 = display_with_positions(binance_basis)
    n_pos6 = sum(1 for r in binance_basis if r.get('pos_usd') is not None)
    print(f"\nBinance 股票永续期现基差 (有现货{len(binance_basis)}个, 显示Top30+持仓{n_pos6}) — {date_str}  (按7dF降序)")
    print(f"{'symbol':<18}{'fund(bp)':>10}{'3dF':>13}{'7dF':>13}{'30dF':>13}{'perp':>12}{'spot':>12}{'spread_bp':>13}{'合约24hVol':>16}{'现货24hVol':>16}{'折算率':>10}{'持仓U':>12}")
    print("-" * 158)
    for r in b6:
        sp = '-' if r['spread_bp'] is None else f"{r['spread_bp']:.1f}"
        pp = '-' if r['perp'] is None else f"{r['perp']:g}"
        st = f"{r['spot']:g}"
        fb = '-' if r['funding_bp'] is None else f"{r['funding_bp']:g}"
        dr = '-' if r.get('discount') is None else f"{r['discount']:.2f}"
        ps = fmt_mio(r['pos_usd']) if r.get('pos_usd') is not None else '-'
        print(f"{r['symbol']+'/USDT':<18}{fb:>10}{fmt_pct(r['3dF']):>13}{fmt_pct(r['7dF']):>13}"
              f"{fmt_pct(r['30dF']):>13}{pp:>12}{st:>12}{sp:>13}"
              f"{fmt_mio(r['perp_vol']):>16}{fmt_mio(r['spot_vol']):>16}{dr:>10}{ps:>12}")

    # ============ 表7: OKX 代币化股票期现基差 ============
    okx_basis = build_okx_basis_rows(biyi_pos['OKX'], now_ms)
    o7 = display_with_positions(okx_basis)
    n_pos7 = sum(1 for r in okx_basis if r.get('pos_usd') is not None)
    print(f"\nOKX 代币化股票期现基差 (有现货{len(okx_basis)}个, 显示Top30+持仓{n_pos7}) — {date_str}  (按7dF降序)")
    print(f"{'symbol':<16}{'fund(bp)':>10}{'3dF':>13}{'7dF':>13}{'30dF':>13}{'perp':>12}{'spot':>12}"
          f"{'spread_bp':>13}{'合约24hVol':>16}{'现货24hVol':>16}{'折算率':>10}{'持仓U':>12}")
    print("-" * 158)
    for r in o7:
        sp = '-' if r['spread_bp'] is None else f"{r['spread_bp']:.1f}"
        pp = '-' if r['perp'] is None else f"{r['perp']:g}"
        st = f"{r['spot']:g}"
        fb = '-' if r['funding_bp'] is None else f"{r['funding_bp']:g}"
        ps = fmt_mio(r['pos_usd']) if r.get('pos_usd') is not None else '-'
        print(f"{r['symbol']+'/USDT':<16}{fb:>10}{fmt_pct(r['3dF']):>13}{fmt_pct(r['7dF']):>13}"
              f"{fmt_pct(r['30dF']):>13}{pp:>12}{st:>12}{sp:>13}"
              f"{fmt_mio(r['perp_vol']):>16}{fmt_mio(r['spot_vol']):>16}{r['discount']:>10.2f}{ps:>12}")

    # 保存 CSV
    df = pd.DataFrame(rows_vol)
    df.insert(0, 'Timestamp', current_time)
    df.to_csv(LOG_FILE_NAME, mode='a', header=not os.path.isfile(LOG_FILE_NAME), index=False, encoding='utf-8-sig')
    print(f"\n💾 已保存至：{LOG_FILE_NAME}")

    if rows_profile:
        dfp = pd.DataFrame(rows_profile)
        pf = "funding_profile_log.csv"
        append_csv(dfp, pf)
        print(f"💾 表4已保存至：{pf}")

    if cross_arb:
        dca = pd.DataFrame(cross_arb)
        dca.insert(0, 'Timestamp', current_time)
        append_csv(dca, "cross_funding_arb_log.csv")
        print(f"💾 表5已保存至：cross_funding_arb_log.csv")

    if binance_basis:
        dbb = pd.DataFrame(binance_basis)
        dbb.insert(0, 'Timestamp', current_time)
        append_csv(dbb, "binance_basis_log.csv")
        print(f"💾 表6已保存至：binance_basis_log.csv")

    if okx_basis:
        dob = pd.DataFrame(okx_basis)
        dob.insert(0, 'Timestamp', current_time)
        append_csv(dob, "okx_basis_log.csv")
        print(f"💾 表7已保存至：okx_basis_log.csv")

    # 发送 Slack：表5跨所套利 + 表6 Binance期现基差 + 表7 OKX期现基差（表4画像/表1成交量 只算不发）
    if SLACK_WEBHOOK_URL:
        combined = (build_cross_blocks(cross_arb, current_time)
                    + build_binance_basis_blocks(binance_basis, current_time)
                    + build_okx_basis_blocks(okx_basis, current_time))
        if combined:
            try:
                resp = requests.post(SLACK_WEBHOOK_URL, json={"blocks": combined}, timeout=10)
                print("✅ 已发送 跨所套利+期现基差 到 Slack" if resp.status_code == 200 else f"❌ Slack 失败: {resp.status_code}")
            except Exception as e:
                print(f"❌ Slack 错误: {e}")

def build_profile_blocks(rows_profile, current_time):
    """构建表4 单所 Funding 画像的 Slack blocks 列表（供与其他表合并成一条消息）"""
    if not rows_profile:
        return []
    date_str = current_time[:10]
    blocks = [{"type": "header", "text": {"type": "plain_text",
                                          "text": f"📊 Funding 画像 Top30/所 — {date_str}"}}]
    legend = ("*字段说明*（每所按 7d 年化降序取 Top30）：\n"
              "• `int`=结算周期 | `fund`=当期费率(bp)\n"
              "• `3d/7d/30d`=近3/7/30天 funding 均值年化%\n"
              "• `std`=近7天 funding 年化波动%（越低越稳）\n"
              "• `OI`=未平仓名义(USD) | `spot`=该所有无现货(Y/N)")
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": legend}})
    for ex in ('Binance', 'OKX', 'Bybit'):
        ex_rows = [r for r in rows_profile if r['exchange'] == ex]
        if not ex_rows:
            continue
        lines = [f"*{EX_DISPLAY[ex]}* (Top{len(ex_rows)})", "```"]
        lines.append(f"{'symbol':<12}{'int':>4}{'fund':>7}{'3d%':>7}{'7d%':>7}{'30d%':>7}{'std':>7}{'OI':>9}{'spot':>5}")
        for r in ex_rows:
            fb = f"{r['funding_bp']:g}" if isinstance(r['funding_bp'], (int, float)) else '-'
            iv = f"{r['int']:g}h"
            lines.append(f"{r['symbol']+'/USDT':<12}{iv:>4}{fb:>7}"
                         f"{fmt_pct(r['3d_apr%']):>7}{fmt_pct(r['7d_apr%']):>7}"
                         f"{fmt_pct(r['30d_apr%']):>7}{fmt_pct(r['std_7d_y%']):>7}{fmt_oi(r['OI_usd']):>9}{r['spot']:>5}")
        lines.append("```")
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}})
    return blocks

def build_cross_blocks(cross_arb, current_time):
    """构建表5 跨所净 funding 套利的 Slack blocks 列表（供与其他表合并成一条消息）"""
    if not cross_arb:
        return []
    date_str = current_time[:10]
    rows = cross_arb[:30]
    # 全 ASCII 表头/取值，避免中文与 emoji 在等宽字体下宽度≠len 导致错位
    header = (f"{'symbol':<13}{'long':<11}{'short':<11}{'3dF':>10}{'3dS':>10}{'n3d':>10}"
              f"{'7dF':>10}{'7dS':>10}{'n7d':>10}{'cons':>8}{'longV':>9}{'shortV':>9}{'ent':>6}{'v':>5}")

    def fmtrow(r):
        # 红绿灯 emoji：同款 emoji 宽度一致(都2格)，放最后两列既醒目又不破坏对齐
        ent = {'✅': '🟢', '✗': '🔴', '?': '⚪'}.get(r['enter'], '⚪')
        v = {'✅': '🟢', '⏸': '⚪'}.get(r['verdict'], '⚪')
        return (f"{r['symbol']+'/USDT':<13}{EX_DISPLAY[r['long_ex']]:<11}{EX_DISPLAY[r['short_ex']]:<11}"
                f"{fmt_pct(r['3d_funding']):>10}{fmt_pct(r['3d_spread']):>10}{fmt_pct(r['net_3d']):>10}"
                f"{fmt_pct(r['7d_funding']):>10}{fmt_pct(r['7d_spread']):>10}{fmt_pct(r['net_7d']):>10}"
                f"{r['consistency']*100:>7.0f}%{fmt_mio(r['long_vol']):>9}{fmt_mio(r['short_vol']):>9}{ent:>5}{v:>4}")
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": f"📊 跨所净Funding套利 Top{len(rows)} — {date_str}"}}]
    legend = ("*字段*：long=低FR所做多 / short=高FR所做空（delta中性吃 funding 差）\n"
              "• `3dF/7dF`=近3/7天 funding 净年化% | `3dS/7dS`=当前价差按3/7天平仓年化%\n"
              "• `n3d/n7d`=funding+spread 合计年化% | `cons`=近7天净差为正占比(越高越稳)\n"
              "• `longV/shortV`=做多/做空所 24h 成交额(百万U) | `ent`=当期可进(🟢有利/🔴否/⚪未知) | `v`=判定(🟢推荐/⚪观察)")
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": legend}})
    # 列加宽 + emoji 后单块超 3000 字符，每 15 行一个 code block
    for i in range(0, len(rows), 15):
        chunk = rows[i:i + 15]
        text = "```\n" + header + "\n" + "\n".join(fmtrow(r) for r in chunk) + "\n```"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})
    return blocks

def build_binance_basis_blocks(basis_rows, current_time):
    """构建表6 Binance 股票永续 Funding+基差 的 Slack blocks 列表"""
    if not basis_rows:
        return []
    date_str = current_time[:10]
    top = display_with_positions(basis_rows)
    header = f"{'symbol':<13}{'fund':>7}{'3dF':>9}{'7dF':>9}{'30dF':>9}{'spread':>9}{'perpVol':>10}{'spotVol':>10}{'折算':>7}{'持仓':>8}"

    def fr(r):
        sp = '-' if r['spread_bp'] is None else f"{r['spread_bp']:.1f}"
        fb = '-' if r['funding_bp'] is None else f"{r['funding_bp']:g}"
        dr = '-' if r.get('discount') is None else f"{r['discount']:.2f}"
        ps = fmt_mio(r['pos_usd']) if r.get('pos_usd') is not None else '-'
        return (f"{r['symbol']+'/USDT':<13}{fb:>7}{fmt_pct(r['3dF']):>9}{fmt_pct(r['7dF']):>9}"
                f"{fmt_pct(r['30dF']):>9}{sp:>9}{fmt_mio(r['perp_vol']):>10}{fmt_mio(r['spot_vol']):>10}{dr:>7}{ps:>8}")
    n_pos = sum(1 for r in basis_rows if r.get('pos_usd') is not None)
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": f"📊 Binance股票永续期现基差 (有现货{len(basis_rows)}个,含持仓{n_pos}) — {date_str}"}}]
    legend = ("*字段*：有现货(XXXB)的股票，显示 Top30+持仓；`fund`=当期funding(bp) | `3dF/7dF/30dF`=近3/7/30天funding年化%（按7dF降序）\n"
              "• `spread`=(perp−spot)/perp×1e4 bp，永续相对现货溢价，可期现对冲\n"
              "• `perpVol/spotVol`=合约/现货24h成交额(百万U) | `折算`=组合保证金折算率 | `持仓`=Biyi持仓额(百万U)")
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": legend}})
    for i in range(0, len(top), 15):
        chunk = top[i:i + 15]
        text = "```\n" + header + "\n" + "\n".join(fr(r) for r in chunk) + "\n```"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})
    return blocks

def build_okx_basis_blocks(basis_rows, current_time):
    """构建表7 OKX 代币化股票期现基差 的 Slack blocks 列表"""
    if not basis_rows:
        return []
    date_str = current_time[:10]
    top = display_with_positions(basis_rows)
    header = f"{'symbol':<12}{'fund':>7}{'3dF':>9}{'7dF':>9}{'30dF':>9}{'spread':>9}{'perpVol':>10}{'spotVol':>10}{'折算':>7}{'持仓':>8}"

    def fr(r):
        sp = '-' if r['spread_bp'] is None else f"{r['spread_bp']:.1f}"
        fb = '-' if r['funding_bp'] is None else f"{r['funding_bp']:g}"
        ps = fmt_mio(r['pos_usd']) if r.get('pos_usd') is not None else '-'
        return (f"{r['symbol']+'/USDT':<12}{fb:>7}{fmt_pct(r['3dF']):>9}{fmt_pct(r['7dF']):>9}"
                f"{fmt_pct(r['30dF']):>9}{sp:>9}{fmt_mio(r['perp_vol']):>10}{fmt_mio(r['spot_vol']):>10}"
                f"{r['discount']:>7.2f}{ps:>8}")
    n_pos = sum(1 for r in basis_rows if r.get('pos_usd') is not None)
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": f"📊 OKX代币化股票期现基差 (有现货{len(basis_rows)}个,含持仓{n_pos}) — {date_str}"}}]
    legend = ("*字段*：OKX 代币化股票(现货X前缀)，显示 Top30+持仓；`fund`=当期funding(bp) | `3dF/7dF/30dF`=近3/7/30天funding年化%（按7dF降序）\n"
              "• `spread`=(perp−spot)/perp×1e4 bp，永续相对现货溢价，可期现对冲\n"
              "• `perpVol/spotVol`=合约/现货24h成交额(百万U) | `折算`=OKX保证金折算率(按5万U落档) | `持仓`=Biyi持仓额(百万U)")
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": legend}})
    for i in range(0, len(top), 15):
        chunk = top[i:i + 15]
        text = "```\n" + header + "\n" + "\n".join(fr(r) for r in chunk) + "\n```"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})
    return blocks

def send_slack_volume_report(rows_vol, current_time):
    """发送成交量报告到 Slack"""
    if not rows_vol:
        return
    
    lines = ["```"]
    header = f"{'Symbol':<8} {'Binance':>12} {'OKX':>12} {'Bitget':>12} {'Bybit':>12} {'Gate':>12}"
    lines.append(header)
    lines.append("-" * 70)
    
    for row in rows_vol:
        symbol = row['Symbol'][:8].ljust(8)
        bn = row['Binance'][:12].rjust(12)
        ok = row['OKX'][:12].rjust(12)
        bg = row['Bitget'][:12].rjust(12)
        bb = row['Bybit'][:12].rjust(12)
        gt = row['Gate'][:12].rjust(12)
        line = f"{symbol} {bn} {ok} {bg} {bb} {gt}"
        lines.append(line)
    
    lines.append("```")
    text_content = "\n".join(lines)
    
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*📊 24h成交量 (USDT) - {current_time}*"
            }
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": text_content
            }
        }
    ]
    
    payload = {"blocks": blocks}
    
    try:
        resp = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=10)
        if resp.status_code == 200:
            print("✅ 已发送成交量到 Slack")
        else:
            print(f"❌ Slack 成交量发送失败: {resp.status_code}")
    except Exception as e:
        print(f"❌ Slack 成交量错误: {e}")



RUN_INTERVAL_SEC = 8 * 3600  # 循环模式间隔：8 小时

if __name__ == "__main__":
    import sys
    loop = '--loop' in sys.argv  # 加 --loop 则每 8 小时自动跑一次，否则只跑一次
    while True:
        try:
            main()
        except KeyboardInterrupt:
            print("\n🛑 用户中断")
            break
        except Exception as e:
            print(f"\n❌ 错误：{e}")
        if not loop:
            break
        print(f"\n😴 休眠 8 小时后再次采集…（Ctrl+C 退出）")
        try:
            time.sleep(RUN_INTERVAL_SEC)
        except KeyboardInterrupt:
            print("\n🛑 用户中断")
            break
