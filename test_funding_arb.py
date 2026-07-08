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

# ---- 表4 单所画像纯函数 ----

def _series(rates, step_ms=14400000, start=0):  # 4h 间隔
    return {start + i * step_ms: r for i, r in enumerate(rates)}

def test_infer_interval_4h():
    assert abs(m.infer_interval_hours(_series([0.0001]*5)) - 4.0) < 1e-9

def test_infer_interval_single_point_fallback():
    assert m.infer_interval_hours({0: 0.0001}) == 8.0

def test_infer_interval_robust_to_outlier():
    # 混入一个 1 分钟的异常小间隔，众数仍应为 4h（不被 min 拉到接近0）
    s = {0: 0.1, 60000: 0.1, 14400000: 0.1, 28800000: 0.1, 43200000: 0.1}
    assert abs(m.infer_interval_hours(s) - 4.0) < 1e-9

def test_window_apr_basic():
    # 全在窗口内，均值0.0001，4h年化 = 0.0001*(24/4)*365*100 = 21.9
    s = _series([0.0001]*5)
    assert abs(m.window_apr(s, 0, 4.0) - 21.9) < 1e-6

def test_window_apr_cutoff_filters():
    # 只取 cutoff 之后的点：前两点较高值被排除
    s = {0: 0.01, 14400000: 0.0001, 28800000: 0.0001}
    # cutoff=14400000 → 仅取后两点，均值0.0001 → 21.9
    assert abs(m.window_apr(s, 14400000, 4.0) - 21.9) < 1e-6

def test_window_apr_empty_returns_none():
    assert m.window_apr({0: 0.0001}, 999999999999, 4.0) is None

def test_window_std_apr_zero_variance():
    # 恒定 funding → std=0
    s = _series([0.0001]*5)
    assert abs(m.window_std_apr(s, 0, 4.0) - 0.0) < 1e-9

def test_window_std_apr_value():
    # rates [0.0001, 0.0003] 总体std = 0.0001; 年化 = 0.0001*(24/4)*365*100 = 21.9
    s = {0: 0.0001, 14400000: 0.0003}
    assert abs(m.window_std_apr(s, 0, 4.0) - 21.9) < 1e-6

def test_window_std_apr_single_point_none():
    assert m.window_std_apr({0: 0.0001}, 0, 4.0) is None

def test_profile_rows_sorted_per_exchange_by_7d_desc():
    hi = 14400000  # 4h
    hist = {
        'Binance': {'A': {0: 0.0001, hi: 0.0001}, 'B': {0: 0.0003, hi: 0.0003}},
        'OKX': {}, 'Bybit': {},
    }
    fr = {'Binance': {'A': {'bp': 1.0}, 'B': {'bp': 3.0}}, 'OKX': {}, 'Bybit': {}}
    oi = {'A': {}, 'B': {}}
    spot = {'Binance': {'A'}, 'OKX': set(), 'Bybit': set()}  # A 有现货，B 无
    rows = m.build_funding_profile_rows(['A', 'B'], hist, fr, oi, spot, 'ts', 2 * hi)
    bn = [r for r in rows if r['exchange'] == 'Binance']
    # B 的 7d 年化更高 → 组内排在 A 前
    assert [r['symbol'] for r in bn] == ['B', 'A']
    assert bn[0]['7d_apr%'] > bn[1]['7d_apr%']
    # spot 列：A=Y, B=N
    assert {r['symbol']: r['spot'] for r in bn} == {'A': 'Y', 'B': 'N'}

# ---- 表5 跨所净 funding 套利 ----

def test_cross_pair_metrics_basic():
    hi = 14400000  # 4h
    now = 100 * hi
    def series(rate, n=50):
        return {now - i * hi: rate for i in range(n)}
    sa = series(0.0002)  # Binance 高
    sb = series(0.0001)  # OKX 低
    r = m.cross_pair_metrics('X', 'Binance', 'OKX', sa, sb, 100.0, 100.0, 1e6, 1e6, 2.0, 1.0, now)
    assert r['short_ex'] == 'Binance' and r['long_ex'] == 'OKX'  # 高7d做空
    assert r['enter'] == '✅'   # 当期 空(2.0) − 多(1.0) = 1.0 > 0
    assert abs(r['7d_funding'] - 21.9) < 0.5   # 43.8 - 21.9
    assert abs(r['3d_funding'] - 21.9) < 0.5   # 恒定 rate → 3d funding 同 7d
    assert r['3d_spread'] == 0.0 and r['7d_spread'] == 0.0   # 价格相同 → 价差 0
    assert abs(r['net_3d'] - 21.9) < 0.5 and abs(r['net_7d'] - 21.9) < 0.5  # spread=0 → net=funding
    assert r['consistency'] == 1.0
    assert r['verdict'] == '✅'              # funding>0, 一致1.0, 回本<15

def test_cross_pair_metrics_unstable():
    hi = 14400000
    now = 100 * hi
    sa, sb = {}, {}
    for i in range(50):
        ts = now - i * hi
        sa[ts] = 0.0003 if i % 2 == 0 else -0.0002  # 交替，均值低
        sb[ts] = 0.0001
    r = m.cross_pair_metrics('X', 'Binance', 'OKX', sa, sb, 100.0, 100.0, 1e6, 1e6, None, None, now)
    assert r['consistency'] < 0.8
    assert r['reason'] == 'funding不稳' and r['verdict'] == '⏸'
    assert r['enter'] == '?'   # 当期 funding 缺失 → 无法判断
