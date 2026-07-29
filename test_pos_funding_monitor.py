import pos_funding_monitor as pm


def _strat(product, ticker, acct, qty, stype='LONGSHORT'):
    return {'strategyType': stype, 'productType': product, 'ticker': ticker,
            'accountMap': acct, 'maxPositionQty': qty}


# ---- 持仓解析 ----

def test_parse_only_perp_leg():
    # SS-PU 现货腿必须忽略：两腿名义相等，一起算会翻倍
    data = [_strat('SM-PU', 'BTC/USDT', 'binance_g1_trade1', 100.0),
            _strat('SS-PU', 'BTC/USDT', 'binance_g1_trade1', 100.0)]
    assert pm.parse_biyi_positions(data)['Binance'] == {'BTC': 100.0}


def test_parse_three_exchanges():
    data = [_strat('SM-PU', 'BTC/USDT', 'binance_g1_trade1', 1.0),
            _strat('SM-PU', 'SUI/USDT', 'okexv5_client_x_trade1', 2.0),
            _strat('SM-PU', 'LINK/USDT', 'bybit_clientUTAGroup1_pairTrade1', 3.0)]
    pos = pm.parse_biyi_positions(data)
    assert pos['Binance'] == {'BTC': 1.0}
    assert pos['OKX'] == {'SUI': 2.0}
    assert pos['Bybit'] == {'LINK': 3.0}   # 现有主脚本会丢掉 Bybit，这里必须保留


def test_parse_sums_across_accounts():
    data = [_strat('SM-PU', 'DOGE/USDT', 'bybit_g1_trade1', 100.0),
            _strat('SM-PU', 'DOGE/USDT', 'bybit_g1_trade2', 50.0)]
    assert pm.parse_biyi_positions(data)['Bybit'] == {'DOGE': 150.0}


def test_parse_drops_zero_and_negative_qty():
    data = [_strat('SM-PU', 'CRCLB/USDT', 'binance_g1_trade1', 0.0),
            _strat('SM-PU', 'MU/USDT', 'binance_g1_trade1', -5.0)]
    assert pm.parse_biyi_positions(data)['Binance'] == {}


def test_parse_ignores_non_longshort_and_unknown_exchange():
    data = [_strat('SM-PU', 'BTC/USDT', 'binance_g1', 1.0, stype='ARBITRAGE'),
            _strat('SM-PU', 'BTC/USDT', 'gate_g1', 1.0),
            _strat('SM-PU', 'BADTICKER', 'binance_g1', 1.0)]
    pos = pm.parse_biyi_positions(data)
    assert all(pos[ex] == {} for ex in pm.EXCHANGES)


def test_parse_bad_qty_does_not_crash():
    data = [_strat('SM-PU', 'BTC/USDT', 'binance_g1', None),
            _strat('SM-PU', 'ETH/USDT', 'binance_g1', 'abc')]
    assert pm.parse_biyi_positions(data)['Binance'] == {}


# ---- base 名映射 ----

def test_variants_order_exact_first():
    # 'MU' 原样存在就不能被去 B / 去 X 干扰
    assert pm.base_variants('MU')[0] == 'MU'


def test_variants_strip_trailing_b():
    v = pm.base_variants('CRCLB')
    assert v[:2] == ['CRCLB', 'CRCL']


def test_variants_strip_leading_x():
    v = pm.base_variants('XTSLA')
    assert v[:2] == ['XTSLA', 'TSLA']


def test_variants_no_empty_from_single_char():
    assert pm.base_variants('B')[0] == 'B'
    assert all(x for x in pm.base_variants('B'))


def test_variants_include_multiplier_prefixes():
    v = pm.base_variants('PEPE')
    assert '1000PEPE' in v and '10000PEPE' in v and '1000000PEPE' in v


def test_variants_plain_forms_rank_before_multipliers():
    # 原样/剥离形式必须全部排在放大前缀形式之前
    v = pm.base_variants('CRCLB')
    assert v.index('CRCL') < v.index('1000CRCLB')


def test_match_bybit_multiplier_contract():
    # Bybit 上 PEPE 只有 1000PEPEUSDT，持仓 base 是 PEPE
    assert pm.match_base('PEPE', {'1000PEPE', 'BTC'}) == '1000PEPE'


def test_match_10000_and_1000000_contracts():
    assert pm.match_base('SATS', {'10000SATS'}) == '10000SATS'
    assert pm.match_base('MOG', {'1000000MOG'}) == '1000000MOG'


def test_match_prefers_plain_over_multiplier():
    # OKX 上 PEPE 原名存在时不能选放大合约
    assert pm.match_base('PEPE', {'PEPE', '1000PEPE'}) == 'PEPE'


def test_match_prefers_exact_over_stripped():
    # 同时存在 'BNB' 和 'BN' 时，必须选原样的 'BNB'
    assert pm.match_base('BNB', {'BNB', 'BN'}) == 'BNB'


def test_match_binance_spot_token_name():
    assert pm.match_base('CRCLB', {'CRCL', 'SNDK'}) == 'CRCL'


def test_match_okx_x_prefixed_spot_name():
    assert pm.match_base('XTSLA', {'TSLA'}) == 'TSLA'


def test_match_returns_none_when_absent():
    assert pm.match_base('NOSUCH', {'BTC'}) is None


# ---- 阈值判定 ----

def _row(ex, base, bp, iv=8.0, usd=50000.0):
    return {'exchange': ex, 'pos_base': base, 'perp_base': base,
            'bp': bp, 'interval_h': iv, 'pos_usd': usd}


def test_pick_alerts_threshold_and_order():
    rows = [_row('Binance', 'A', -3.0), _row('OKX', 'B', -9.0),
            _row('Bybit', 'C', -6.0), _row('Binance', 'D', 12.0)]
    assert [r['perp_base'] for r in pm.pick_alerts(rows, -5.0)] == ['B', 'C']


def test_pick_alerts_boundary_is_strict():
    # 恰好 -5 不告警，略低于才告警
    assert pm.pick_alerts([_row('Binance', 'A', -5.0)], -5.0) == []
    assert len(pm.pick_alerts([_row('Binance', 'A', -5.01)], -5.0)) == 1


def test_pick_alerts_positive_funding_never_alerts():
    assert pm.pick_alerts([_row('Binance', 'A', 300.0)], -5.0) == []


# ---- 现货 base 映射（方向与 perp 相反：现货是加后缀/前缀）----

def test_spot_variants_exact_first():
    # 普通币原样即命中，绝不能被 BTCB 抢走
    assert pm.spot_base_variants('BTC')[0] == 'BTC'


def test_spot_variants_add_b_for_binance_stock():
    # 'SNDK' 的 Binance 现货是 SNDKBUSDT，SNDKUSDT 不存在
    assert 'SNDKB' in pm.spot_base_variants('SNDK')


def test_spot_variants_add_x_for_okx_stock():
    # 'SOXL' 的 OKX 现货是 XSOXL-USDT
    assert 'XSOXL' in pm.spot_base_variants('SOXL')


def test_spot_variants_already_spot_name_hits_first():
    # 'CRCLB' 本身就是现货名
    assert pm.spot_base_variants('CRCLB')[0] == 'CRCLB'


# ---- 放大倍数 ----

def test_multiplier_one_when_same():
    assert pm.perp_multiplier('BTC', 'BTC') == 1.0


def test_multiplier_1000_for_bybit_pepe():
    assert pm.perp_multiplier('PEPE', '1000PEPE') == 1000.0


def test_multiplier_10000_and_1000000():
    assert pm.perp_multiplier('SATS', '10000SATS') == 10000.0
    assert pm.perp_multiplier('MOG', '1000000MOG') == 1000000.0


def test_multiplier_one_for_b_stripped_stock():
    # CRCLB → CRCL 是命名差异不是放大
    assert pm.perp_multiplier('CRCLB', 'CRCL') == 1.0


def test_multiplier_handles_stripped_then_scaled():
    # 剥离形式再放大也要认出来，否则价差会差 1000 倍
    assert pm.perp_multiplier('XFOO', '1000FOO') == 1000.0


# ---- 价差公式 ----

def test_spread_positive_when_spot_above_perp():
    # 现货卖一 100，合约买一 99 → (100-99)/100*1e4 = 100bp
    assert abs(pm.spread_bp(100.0, 99.0) - 100.0) < 1e-9


def test_spread_negative_when_perp_above_spot():
    # 永续报价高于现货卖价 → 负值（开仓方向有利）
    assert abs(pm.spread_bp(100.0, 101.0) - (-100.0)) < 1e-9


def test_spread_zero_when_equal():
    assert pm.spread_bp(100.0, 100.0) == 0.0


def test_spread_none_on_missing_leg():
    assert pm.spread_bp(None, 99.0) is None
    assert pm.spread_bp(100.0, None) is None
    assert pm.spread_bp(0.0, 99.0) is None


def test_spread_uses_multiplier_adjusted_perp_bid():
    # 1000PEPE 报价 0.002765 → 单币 0.000002765；现货卖一 0.000002766
    perp_bid = 0.002765 / pm.perp_multiplier('PEPE', '1000PEPE')
    s = pm.spread_bp(0.000002766, perp_bid)
    assert abs(s - 3.616) < 0.01   # 若忘记折回倍数，结果会是 -99900000 量级


# ---- 数值解析 ----

def test_price_parse_rejects_zero_and_garbage():
    assert pm._f('0') is None and pm._f('') is None and pm._f(None) is None
    assert pm._f('63.70000000') == 63.7


# ---- Slack blocks ----

def test_no_alerts_no_problems_means_no_message():
    assert pm.build_blocks([], [], 10, 'ts') == []


def test_problems_alone_still_send_message():
    blocks = pm.build_blocks([], ['Biyi HTTP 500'], 0, 'ts')
    assert blocks and 'Biyi HTTP 500' in str(blocks)


def test_alert_block_contains_symbol_bp_interval_position():
    blocks = pm.build_blocks([_row('Binance', 'CRCL', -8.32, iv=1.0, usd=34590.0)], [], 5, 'ts')
    text = str(blocks)
    assert 'CRCL' in text and '-8.32' in text and '1h' in text and '0.03M' in text
