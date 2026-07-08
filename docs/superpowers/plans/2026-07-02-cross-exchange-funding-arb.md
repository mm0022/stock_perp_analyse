# 跨所 Funding 套利 · 持仓视角改造 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `stock_perp_24hvlum_openclaw.py` 表3从「快照收益」改为「回本天数 + funding 稳定性」的持仓视角跨所套利判定。

**Architecture:** 新增 3 个历史 funding 拉取函数（Binance/OKX/Bybit），按 fundingTime 对齐成时间序列；用纯函数 `pair_metrics` 计算历史均值年化差、符号一致率、混合手续费、开仓价差、回本天数；改写 `main()` 表3 段落与 Slack/CSV 输出。纯计算逻辑单测，网络拉取靠实跑验证。

**Tech Stack:** Python 3.12, requests, pandas；测试用 pytest（`uv run --with pytest`）。

## Global Constraints

- **非 git 仓库**：本项目当前不是 git 仓库，不执行 commit。每个任务以「单测通过」或「实跑输出符合预期」作为完成标准。若后续 `git init`，可自行补提交。
- **保持单文件**：所有生产代码改动集中在 `stock_perp_24hvlum_openclaw.py`，不拆分。
- **参与配对交易所**：仅 Binance / OKX / Bybit（有价格 + funding + 历史接口）。
- **费率（bp，单笔单腿）**：Binance maker=0 taker=1.5；Bybit maker=0 taker=1.25；OKX maker=0.8 taker=2.7。
- **网络走代理**：所有请求复用现有 `fetch_json`（已带 `proxies`）。运行/测试网络部分需代理 `127.0.0.1:7890` 可用。
- **annualize 已实现**：`annualize(rate, interval_hours) = rate*(24/interval_hours)*365*100`，返回百分比，周期无效返回 None。已在文件中，勿重复定义。
- **阈值**：`SIGN_CONSISTENCY_MIN=0.8`、`BREAKEVEN_DAYS_MAX=15`、`HISTORY_DAYS=30`、`MIN_VOLUME=1_000_000`。

---

## File Structure

- Modify: `stock_perp_24hvlum_openclaw.py`
  - 配置区新增参数与 `FEE` 表
  - 新增区块「历史 funding 与持仓分析」：`mixed_fee_bp`、`infer_interval_hours`、`series_annualized`、`pair_metrics`、`get_{binance,okx,bybit}_funding_history`
  - 改写 `main()` 表3段 + `send_slack_report` + CSV 落盘
- Create: `test_funding_arb.py`（纯函数单测）

---

### Task 1: 配置参数 + 混合手续费函数

**Files:**
- Modify: `stock_perp_24hvlum_openclaw.py`（配置区 ~line 8-21；Funding 区块 `annualize` 之后）
- Test: `test_funding_arb.py`

**Interfaces:**
- Produces:
  - 模块级常量 `HISTORY_DAYS:int`、`FEE:dict[str,dict[str,float]]`、`FEE_MODE:str`、`SIGN_CONSISTENCY_MIN:float`、`BREAKEVEN_DAYS_MAX:int`、`MIN_VOLUME:int`
  - `mixed_fee_bp(ex_low: str, ex_high: str) -> float` — 返回开+平共 4 笔的混合手续费总额(bp)，= `2 * min(FEE[ex_low]['maker']+FEE[ex_high]['taker'], FEE[ex_low]['taker']+FEE[ex_high]['maker'])`

- [ ] **Step 1: 写失败测试** — 新建 `test_funding_arb.py`

```python
import stock_perp_24hvlum_openclaw as m

def test_mixed_fee_binance_okx():
    # min(0+2.7, 1.5+0.8)=2.3, *2 = 4.6
    assert abs(m.mixed_fee_bp('Binance', 'OKX') - 4.6) < 1e-9

def test_mixed_fee_binance_bybit():
    # min(0+1.25, 1.5+0)=1.25, *2 = 2.5
    assert abs(m.mixed_fee_bp('Binance', 'Bybit') - 2.5) < 1e-9

def test_mixed_fee_okx_bybit():
    # min(0.8+1.25, 2.7+0)=2.05, *2 = 4.1
    assert abs(m.mixed_fee_bp('OKX', 'Bybit') - 4.1) < 1e-9

def test_mixed_fee_symmetric():
    assert abs(m.mixed_fee_bp('OKX', 'Binance') - m.mixed_fee_bp('Binance', 'OKX')) < 1e-9
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run --with pytest pytest test_funding_arb.py -v`
Expected: FAIL —`AttributeError: module ... has no attribute 'mixed_fee_bp'`

- [ ] **Step 3: 加配置常量** — 在配置区（`LOG_FILE_NAME` 附近）加入：

```python
HISTORY_DAYS = 30
FEE = {  # 单笔单腿费率，单位 bp
    'Binance': {'maker': 0.0,  'taker': 1.5},
    'Bybit':   {'maker': 0.0,  'taker': 1.25},
    'OKX':     {'maker': 0.8,  'taker': 2.7},
}
FEE_MODE = 'mixed'
SIGN_CONSISTENCY_MIN = 0.8
BREAKEVEN_DAYS_MAX = 15
MIN_VOLUME = 1_000_000
```

- [ ] **Step 4: 实现 `mixed_fee_bp`** — 加在 `annualize` 之后：

```python
def mixed_fee_bp(ex_low, ex_high):
    """混合成交（一腿maker一腿taker）开+平共4笔的总手续费(bp)，自动选更省的分配"""
    fl, fh = FEE[ex_low], FEE[ex_high]
    per_build = min(fl['maker'] + fh['taker'], fl['taker'] + fh['maker'])
    return 2 * per_build
```

- [ ] **Step 5: 运行确认通过**

Run: `uv run --with pytest pytest test_funding_arb.py -v`
Expected: 4 passed

---

### Task 2: 历史 funding 拉取（三所）

**Files:**
- Modify: `stock_perp_24hvlum_openclaw.py`（Funding 区块内新增 3 函数）

**Interfaces:**
- Consumes: `fetch_json(url, params=None)`（已存在）
- Produces（三者返回结构一致）：
  - `get_binance_funding_history(target_stocks: list[str], days: int) -> dict[str, dict[int, float]]`
  - `get_okx_funding_history(target_stocks, days) -> dict[str, dict[int, float]]`
  - `get_bybit_funding_history(target_stocks, days) -> dict[str, dict[int, float]]`
  - 返回 `{token: {fundingTime_ms: rate_float}}`；无数据的 token 映射为 `{}`。

- [ ] **Step 1: 实现 Binance 历史** — 加在 `get_binance_funding` 之后：

```python
def get_binance_funding_history(target_stocks, days=HISTORY_DAYS):
    """Binance 历史 funding: {token: {ts_ms: rate}}"""
    print("⏳ 正在获取 Binance Funding 历史...")
    results = {s: {} for s in target_stocks}
    limit = min(1000, max(200, days * 6 + 10))  # 4h标的每天6期，留余量
    for tok in target_stocks:
        data = fetch_json("https://fapi.binance.com/fapi/v1/fundingRate",
                          {'symbol': f'{tok}USDT', 'limit': limit})
        if isinstance(data, list):
            for it in data:
                try:
                    results[tok][int(it['fundingTime'])] = float(it['fundingRate'])
                except (KeyError, ValueError, TypeError):
                    pass
    return results
```

- [ ] **Step 2: 实现 OKX 历史** — 用 `realizedRate`（实际结算值）：

```python
def get_okx_funding_history(target_stocks, days=HISTORY_DAYS):
    """OKX 历史 funding: {token: {ts_ms: rate}}"""
    print("⏳ 正在获取 OKX Funding 历史...")
    results = {s: {} for s in target_stocks}
    limit = min(100, max(1, days * 6))  # OKX 单页上限100
    for tok in target_stocks:
        data = fetch_json("https://www.okx.com/api/v5/public/funding-rate-history",
                          {'instId': f'{tok}-USDT-SWAP', 'limit': limit})
        if data and data.get('code') == '0':
            for it in data.get('data', []):
                try:
                    rate = it.get('realizedRate') or it.get('fundingRate')
                    results[tok][int(it['fundingTime'])] = float(rate)
                except (KeyError, ValueError, TypeError):
                    pass
    return results
```

- [ ] **Step 3: 实现 Bybit 历史** — 字段 `fundingRateTimestamp`：

```python
def get_bybit_funding_history(target_stocks, days=HISTORY_DAYS):
    """Bybit 历史 funding: {token: {ts_ms: rate}}"""
    print("⏳ 正在获取 Bybit Funding 历史...")
    results = {s: {} for s in target_stocks}
    limit = min(200, max(1, days * 6))  # Bybit 单页上限200
    for tok in target_stocks:
        data = fetch_json("https://api.bybit.com/v5/market/funding/history",
                          {'category': 'linear', 'symbol': f'{tok}USDT', 'limit': limit})
        if data and data.get('retCode') == 0:
            for it in data.get('result', {}).get('list', []):
                try:
                    results[tok][int(it['fundingRateTimestamp'])] = float(it['fundingRate'])
                except (KeyError, ValueError, TypeError):
                    pass
    return results
```

- [ ] **Step 4: 实跑验证（网络）** — 确认三所都能拉到 SPX 且时间戳可对齐：

Run:
```bash
python3 -c "
import stock_perp_24hvlum_openclaw as m
bn=m.get_binance_funding_history(['SPX'],30)['SPX']
ok=m.get_okx_funding_history(['SPX'],30)['SPX']
bb=m.get_bybit_funding_history(['SPX'],30)['SPX']
print('binance pts', len(bn), 'okx pts', len(ok), 'bybit pts', len(bb))
common=set(bn)&set(ok)
print('binance∩okx aligned pts', len(common))
assert len(bn)>10 and len(ok)>10 and len(common)>10, '数据不足或未对齐'
print('OK')
"
```
Expected: 各所 >10 个点，交集 >10，打印 `OK`。（若代理拦截间歇性空返回，重试；参考已知：三所时间戳一致）

---

### Task 3: 序列对齐 + 持仓指标 `pair_metrics`（纯函数）

**Files:**
- Modify: `stock_perp_24hvlum_openclaw.py`（Funding 区块内新增 3 函数）
- Test: `test_funding_arb.py`

**Interfaces:**
- Consumes: `annualize`（已存在）、`mixed_fee_bp`（Task 1）、常量 `HISTORY_DAYS/SIGN_CONSISTENCY_MIN/BREAKEVEN_DAYS_MAX`
- Produces:
  - `infer_interval_hours(series: dict[int,float]) -> float` — 由相邻时间戳最小正差推断结算周期(小时)，<2点或无有效差回退 `8.0`
  - `series_annualized(series: dict[int,float], keys=None) -> tuple[float|None, float]` — 返回 `(历史均值年化%, 周期h)`。周期始终基于完整 series 推断；均值只在 `keys` 指定的时间戳上算（`keys=None` 用全部），用于两所对齐到共同窗口。空序列返回 `(None, 8.0)`；keys 过滤后无有效点返回 `(None, 周期h)`
  - `pair_metrics(token, ex_a, ex_b, series_a, series_b, price_a, price_b) -> dict | None` — **净年化基于两所共同时间戳交集上的均值**；序列为空或无共同时间窗时返回 None。返回 dict 字段：`token, long_ex, short_ex, net_ann_pct, daily_bp, sign_consistency, diff_std_bp, fee_bp, spread_bp, onetime_cost_bp, breakeven_days, hold_30d_bp, verdict, reason`

- [ ] **Step 1: 写失败测试** — 追加到 `test_funding_arb.py`：

```python
def _series(rates, step_ms=14400000):  # 4h 间隔
    return {i * step_ms: r for i, r in enumerate(rates)}

def test_infer_interval_4h():
    assert abs(m.infer_interval_hours(_series([0.0001]*5)) - 4.0) < 1e-9

def test_infer_interval_single_point_fallback():
    assert m.infer_interval_hours({0: 0.0001}) == 8.0

def test_series_annualized_4h():
    ann, itv = m.series_annualized(_series([0.0001]*5))
    # 0.0001*(24/4)*365*100 = 21.9
    assert abs(ann - 21.9) < 1e-6 and abs(itv - 4.0) < 1e-9

def test_pair_metrics_recommend():
    # Binance 均值0.0001(ann21.9) 高 → 做空; OKX 均值0.00005(ann10.95) 低 → 做多
    sa = _series([0.0001]*5)   # Binance
    sb = _series([0.00005]*5)  # OKX
    # short_price=100(Binance), long_price=100.1(OKX): spread=(100-100.1)/100*1e4=-10bp
    r = m.pair_metrics('SPX', 'Binance', 'OKX', sa, sb, 100.0, 100.1)
    assert r['short_ex'] == 'Binance' and r['long_ex'] == 'OKX'
    assert abs(r['net_ann_pct'] - 10.95) < 1e-6
    assert abs(r['daily_bp'] - 10.95*100/365) < 1e-6
    assert r['sign_consistency'] == 1.0
    # fee=mixed(OKX,Binance)=4.6; spread=-10; onetime=4.6-(-10)=14.6
    assert abs(r['fee_bp'] - 4.6) < 1e-9
    assert abs(r['spread_bp'] - (-10.0)) < 1e-6
    assert abs(r['onetime_cost_bp'] - 14.6) < 1e-6
    # breakeven=14.6/daily
    assert abs(r['breakeven_days'] - 14.6/(10.95*100/365)) < 1e-6
    assert r['verdict'].startswith('✅')

def test_pair_metrics_unstable_not_recommended():
    # funding 差频繁反号 → 一致率低
    sa = _series([0.0001, -0.0002, 0.0001, -0.0002, 0.0003])
    sb = _series([0.00005]*5)
    r = m.pair_metrics('X', 'A', 'B', sa, sb, 100.0, 100.0)  # 用真实所名见下
```

（注：`test_pair_metrics_unstable_not_recommended` 需用真实交易所名以便 `mixed_fee_bp` 查表——改用 `'Binance','OKX'`，断言 `r['sign_consistency'] < 0.8` 且 `r['verdict'].startswith('⏸')`。）

- [ ] **Step 2: 修正 unstable 测试用真实所名并补断言**

```python
def test_pair_metrics_unstable_not_recommended():
    sa = _series([0.0001, -0.0002, 0.0001, -0.0002, 0.0003])  # 均值仍为正
    sb = _series([0.00005]*5)
    r = m.pair_metrics('SPX', 'Binance', 'OKX', sa, sb, 100.0, 100.0)
    assert r['sign_consistency'] < 0.8
    assert r['verdict'].startswith('⏸')
    assert r['reason'] == 'funding不稳'

def test_pair_metrics_uses_common_window():
    # 净年化只应基于两所共同时间戳(交集)，交集外的点不参与
    step = 14400000  # 4h
    # A: t0,t1,t2 —— t0 是一个交集外的异常高值，不应影响结果
    sa = {0: 0.01, step: 0.0001, 2*step: 0.0001}          # Binance
    # B: t1,t2,t3 —— 交集 = {t1, t2}
    sb = {step: 0.00005, 2*step: 0.00005, 3*step: 0.00005}  # OKX
    r = m.pair_metrics('SPX', 'Binance', 'OKX', sa, sb, 100.0, 100.0)
    # 交集只有 t1,t2：A 均值0.0001→ann21.9, B 均值0.00005→ann10.95, net=10.95
    assert abs(r['net_ann_pct'] - 10.95) < 1e-6

def test_pair_metrics_no_common_window_returns_none():
    sa = {0: 0.0001, 14400000: 0.0001}
    sb = {7200000: 0.00005, 21600000: 0.00005}  # 时间戳与 A 无交集
    assert m.pair_metrics('SPX', 'Binance', 'OKX', sa, sb, 100.0, 100.0) is None
```

- [ ] **Step 3: 运行确认失败**

Run: `uv run --with pytest pytest test_funding_arb.py -v`
Expected: 新测试 FAIL（`infer_interval_hours` 等未定义）

- [ ] **Step 4: 实现三函数** — 加在 `mixed_fee_bp` 之后：

```python
def infer_interval_hours(series):
    """由相邻时间戳最小正差推断结算周期(小时)，无法推断回退 8h"""
    ts = sorted(series)
    if len(ts) < 2:
        return 8.0
    diffs = [ts[i+1] - ts[i] for i in range(len(ts)-1) if ts[i+1] > ts[i]]
    if not diffs:
        return 8.0
    return min(diffs) / 3600000.0

def series_annualized(series, keys=None):
    """历史均值年化(%)与周期(h)。
    周期始终基于完整 series 推断；均值只在 keys 指定的时间戳上计算
    （keys=None 时用全部）——用于把两所对齐到共同时间窗口再比较。"""
    if not series:
        return None, 8.0
    interval = infer_interval_hours(series)  # 始终基于完整序列
    ks = list(series) if keys is None else [t for t in keys if t in series]
    if not ks:
        return None, interval
    mean_rate = sum(series[t] for t in ks) / len(ks)
    return annualize(mean_rate, interval), interval

def pair_metrics(token, ex_a, ex_b, series_a, series_b, price_a, price_b):
    """跨所 funding 套利持仓指标；无共同时间窗或序列为空时返回 None。
    净年化基于两所【共同时间戳交集】上的均值，避免三所窗口不等长扭曲比较。"""
    if not series_a or not series_b:
        return None
    common = sorted(set(series_a) & set(series_b))
    if not common:
        return None
    ann_a, _ = series_annualized(series_a, common)
    ann_b, _ = series_annualized(series_b, common)
    if ann_a is None or ann_b is None:
        return None
    # 高年化所做空(收funding)，低年化所做多(付funding)
    if ann_a >= ann_b:
        short_ex, short_series, short_price, short_ann = ex_a, series_a, price_a, ann_a
        long_ex, long_series, long_price, long_ann = ex_b, series_b, price_b, ann_b
    else:
        short_ex, short_series, short_price, short_ann = ex_b, series_b, price_b, ann_b
        long_ex, long_series, long_price, long_ann = ex_a, series_a, price_a, ann_a

    net_ann = short_ann - long_ann
    daily_bp = net_ann * 100 / 365

    # 稳定性：在共同时间窗口(common)上逐期 (空腿rate - 多腿rate)
    diffs = [short_series[t] - long_series[t] for t in common]
    sign_consistency = sum(1 for d in diffs if d > 0) / len(diffs)
    mean_d = sum(diffs) / len(diffs)
    var = sum((d - mean_d) ** 2 for d in diffs) / len(diffs)
    diff_std_bp = (var ** 0.5) * 10000

    fee_bp = mixed_fee_bp(long_ex, short_ex)
    if short_price and long_price and short_price > 0:
        spread_bp = (short_price - long_price) / short_price * 10000
    else:
        spread_bp = 0.0
    onetime_cost = fee_bp - spread_bp

    if daily_bp <= 0:
        breakeven = float('inf')
    elif onetime_cost <= 0:
        breakeven = 0.0
    else:
        breakeven = onetime_cost / daily_bp
    hold_30d = daily_bp * HISTORY_DAYS - onetime_cost

    if net_ann <= 0:
        verdict, reason = '⏸ 观察', '净funding≤0'
    elif sign_consistency < SIGN_CONSISTENCY_MIN:
        verdict, reason = '⏸ 观察', 'funding不稳'
    elif breakeven > BREAKEVEN_DAYS_MAX:
        verdict, reason = '⏸ 观察', '回本太慢'
    else:
        verdict, reason = '✅ 推荐', ''

    return {
        'token': token, 'long_ex': long_ex, 'short_ex': short_ex,
        'net_ann_pct': net_ann, 'daily_bp': daily_bp,
        'sign_consistency': sign_consistency, 'diff_std_bp': diff_std_bp,
        'fee_bp': fee_bp, 'spread_bp': spread_bp, 'onetime_cost_bp': onetime_cost,
        'breakeven_days': breakeven, 'hold_30d_bp': hold_30d,
        'verdict': verdict, 'reason': reason,
    }
```

- [ ] **Step 5: 运行确认通过**

Run: `uv run --with pytest pytest test_funding_arb.py -v`
Expected: 全部 passed

---

### Task 4: 改写 main 表3 + Slack + CSV

**Files:**
- Modify: `stock_perp_24hvlum_openclaw.py`（`main()` 表3段 ~line 424-495；`send_slack_report` ~line 508-562）

**Interfaces:**
- Consumes: Task 2/3 全部函数、现有 `get_contract_prices`、各所成交量、`MIN_VOLUME`

- [ ] **Step 1: 在 main 拉取历史并组装配对** — 替换现有表3段落（`print("📊 表3...")` 到该段 `print(...)` 循环结束）：

```python
    # 拉历史 funding
    hist = {
        'Binance': get_binance_funding_history(tokens),
        'OKX': get_okx_funding_history(tokens),
        'Bybit': get_bybit_funding_history(tokens),
    }

    print("\n" + "="*120)
    print("📊 表3: 跨所 Funding 套利 (持仓视角, 成交量>100万)")
    print("="*120)
    header = (f"{'Symbol':<7}{'做多所':<9}{'做空所':<9}{'净年化%':>9}{'每天bp':>8}"
              f"{'一致率':>7}{'手续费bp':>9}{'价差bp':>8}{'回本天':>8}{'持30dbp':>9} {'判定'}")
    print(header)
    print("-"*120)

    rows_cross = []
    for tok in tokens:
        max_vol = max(
            [v.get(tok, 0) for v in (vol_bn, vol_ok, vol_bb, vol_bg)
             if isinstance(v.get(tok), (int, float))] or [0]
        )
        if max_vol < MIN_VOLUME:
            continue
        exs = ['Binance', 'OKX', 'Bybit']
        best = None
        for i in range(len(exs)):
            for j in range(i+1, len(exs)):
                ea, eb = exs[i], exs[j]
                sa, sb = hist[ea].get(tok, {}), hist[eb].get(tok, {})
                if not sa or not sb:
                    continue
                pa = contract_prices.get(tok, {}).get(ea, 0)
                pb = contract_prices.get(tok, {}).get(eb, 0)
                mret = pair_metrics(tok, ea, eb, sa, sb, pa, pb)
                if mret and (best is None or mret['net_ann_pct'] > best['net_ann_pct']):
                    best = mret
        if not best:
            continue
        rows_cross.append(best)
        be = '∞' if best['breakeven_days'] == float('inf') else f"{best['breakeven_days']:.1f}"
        tag = best['verdict'] + (f"({best['reason']})" if best['reason'] else '')
        print(f"{best['token']:<7}{best['long_ex']:<9}{best['short_ex']:<9}"
              f"{best['net_ann_pct']:>9.2f}{best['daily_bp']:>8.2f}"
              f"{best['sign_consistency']*100:>6.0f}%{best['fee_bp']:>9.2f}"
              f"{best['spread_bp']:>8.2f}{be:>8}{best['hold_30d_bp']:>9.2f} {tag}")
```

- [ ] **Step 2: 改写 `send_slack_report`** — 适配新 dict 字段：

```python
def send_slack_report(rows_cross, current_time):
    if not rows_cross:
        return
    lines = ["```"]
    lines.append(f"{'Sym':<6}{'多':<8}{'空':<8}{'净年化%':>8}{'一致率':>7}{'回本天':>7}{'判定'}")
    lines.append("-" * 60)
    for r in rows_cross:
        be = '∞' if r['breakeven_days'] == float('inf') else f"{r['breakeven_days']:.1f}"
        tag = r['verdict'] + (f"({r['reason']})" if r['reason'] else '')
        lines.append(f"{r['token']:<6}{r['long_ex']:<8}{r['short_ex']:<8}"
                     f"{r['net_ann_pct']:>8.2f}{r['sign_consistency']*100:>6.0f}%{be:>7} {tag}")
    lines.append("```")
    text_content = "\n".join(lines)
    blocks = [
        {"type": "header", "text": {"type": "plain_text",
                                    "text": f"📊 跨所Funding套利(持仓) - {current_time}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": text_content}},
    ]
    try:
        resp = requests.post(SLACK_WEBHOOK_URL, json={"blocks": blocks}, timeout=10)
        print("✅ 已发送 Slack 通知" if resp.status_code == 200 else f"❌ Slack 发送失败: {resp.status_code}")
    except Exception as e:
        print(f"❌ Slack 错误: {e}")
```

- [ ] **Step 3: 更新表3 CSV 落盘** — 在现有 `df = pd.DataFrame(rows_vol)` 落盘之后追加表3落盘：

```python
    if rows_cross:
        df_cross = pd.DataFrame(rows_cross)
        df_cross.insert(0, 'Timestamp', current_time)
        cross_file = "cross_funding_arb_log.csv"
        df_cross.to_csv(cross_file, mode='a', header=not os.path.isfile(cross_file),
                        index=False, encoding='utf-8-sig')
        print(f"💾 表3已保存至：{cross_file}")
```

- [ ] **Step 4: 语法检查**

Run: `python3 -c "import ast; ast.parse(open('stock_perp_24hvlum_openclaw.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 5: 实跑全流程验证**

Run: `python3 stock_perp_24hvlum_openclaw.py`
Expected: 表3 打印新列（净年化%/回本天/判定等）；有推荐项显示 `✅ 推荐`，不稳/慢的显示 `⏸ 观察(原因)`；CSV `cross_funding_arb_log.csv` 生成。人工抽查一行（如 SPX Binance vs OKX）净年化差方向与手算一致。

---

## Self-Review

**Spec coverage：**
- §4 数据层 → Task 2（三所历史拉取，返回 `{token:{ts:rate}}`）✓
- §5 决策模型①持续收益 → `series_annualized` + `pair_metrics.net_ann_pct/daily_bp`（Task 3）✓
- §5 ②稳定性 → `pair_metrics.sign_consistency/diff_std_bp`（Task 3）✓
- §5 ③一次性成本（混合费率、价差、收敛假设）→ `mixed_fee_bp`（Task 1）+ `pair_metrics.spread_bp/onetime_cost_bp`（Task 3）✓
- §5 ④判定（回本天数、阈值、原因）→ `pair_metrics.breakeven_days/verdict/reason`（Task 3）✓
- §6 参数配置 → Task 1 常量 ✓
- §7 输出（新表3/Slack/CSV）→ Task 4 ✓
- §8 保持单文件、配对限三所 → Global Constraints + Task 4 ✓
- §9 验证 → Task 2 Step4 实跑、Task 3 单测、Task 4 Step5 实跑 ✓

**超范围（spec §8 已声明，不在本计划）：** GOOG/NG symbol 映射、表2 期现套利。

**Placeholder scan：** 无 TBD/TODO；所有代码步骤含完整代码。

**Type consistency：** `mixed_fee_bp(ex_low, ex_high)` 全程一致；`pair_metrics` 返回字段在 Task 4 打印/Slack/CSV 中使用的键（token/long_ex/short_ex/net_ann_pct/daily_bp/sign_consistency/fee_bp/spread_bp/breakeven_days/hold_30d_bp/verdict/reason）与 Task 3 定义一致；历史函数返回 `{token:{ts:rate}}` 与 Task 4 `hist[ex].get(tok,{})` 用法一致。
