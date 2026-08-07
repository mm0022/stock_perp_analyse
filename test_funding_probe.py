import funding_probe as fp
from pos_funding_monitor import funding_if_premium_held as F

# ---- classify_period：区分死区钳制 vs 费率锁定 ----
# 这是旧版误判的地方：旧逻辑只看「FR 不动 + premium 摆动大」就断言「本期费率已定」，
# 把死区钳制误判成费率锁定。SOXL 四个周期全被判错。


def test_classify_dead_zone_clamp():
    """FR 全期不动 + 运行均值在死区内 → 死区钳制（不是费率锁定）"""
    label, why = fp.classify_period(fr_span=0.0, avg_prem_bp=-1.6, ir_bp=0.0, prem_span=102.6)
    assert label == "死区钳制"
    assert "死区" in why


def test_classify_dead_zone_clamp_ignores_huge_instant_swing():
    """瞬时摆动再大，只要运行均值在死区内就是钳制——SOXL 的真实情形"""
    label, _ = fp.classify_period(fr_span=0.0, avg_prem_bp=-0.72, ir_bp=0.0, prem_span=101.0)
    assert label == "死区钳制"


def test_classify_rate_lock_candidate():
    """FR 不动但运行均值在死区外 → 这才是费率锁定的候选证据，须人工核查"""
    label, why = fp.classify_period(fr_span=0.0, avg_prem_bp=-20.0, ir_bp=0.0, prem_span=30.0)
    assert label == "⚠ 待查"
    assert "死区外" in why


def test_classify_dead_zone_boundary_is_inclusive():
    """恰好落在死区边界 ±5bp 上算钳制内"""
    assert fp.classify_period(0.0, 5.0, 0.0, 10.0)[0] == "死区钳制"
    assert fp.classify_period(0.0, -5.0, 0.0, 10.0)[0] == "死区钳制"
    assert fp.classify_period(0.0, 5.01, 0.0, 10.0)[0] == "⚠ 待查"


def test_classify_dead_zone_centered_on_interest_rate():
    """死区中心是利率，不是 0。利率 1bp(加密永续) → 死区 [-4, +6]"""
    assert fp.classify_period(0.0, 5.5, 1.0, 10.0)[0] == "死区钳制"   # 在 [-4,+6] 内
    assert fp.classify_period(0.0, -4.5, 1.0, 10.0)[0] == "⚠ 待查"    # 低于 -4


def test_classify_moved_in_period():
    """FR 期内有变化 → 与运行均值模型一致"""
    label, _ = fp.classify_period(fr_span=7.547, avg_prem_bp=5.249, ir_bp=0.0, prem_span=249.0)
    assert label == "期内变化"


# ---- running_model_frs：逐点运行均值代入公式 ----


def test_running_model_uses_cumulative_average_not_instant():
    """第 2 点用的是前 2 个样本的均值，不是第 2 个样本本身"""
    out = fp.running_model_frs([100.0, 0.0], 0.0, None, F)
    # 第1点: avg=100 -> 100+clamp(0-100,±5)=95
    assert abs(out[0] - 95.0) < 1e-9
    # 第2点: avg=50  -> 50+clamp(0-50,±5)=45   （若错用瞬时值 0 会得到 0）
    assert abs(out[1] - 45.0) < 1e-9


def test_running_model_stays_zero_inside_dead_zone():
    """均值一直在死区内 → 模型恒为利率（此处 0）"""
    out = fp.running_model_frs([3.0, -4.0, 1.0, -2.0], 0.0, None, F)
    assert all(abs(v) < 1e-9 for v in out)


def test_running_model_respects_cap():
    """超过 cap 时被截断"""
    out = fp.running_model_frs([500.0], 0.0, 37.5, F)
    assert abs(out[-1] - 37.5) < 1e-9


def test_running_model_length_matches_input():
    out = fp.running_model_frs([1.0, 2.0, 3.0], 0.0, None, F)
    assert len(out) == 3


def test_running_model_matches_verified_real_values():
    """对齐 2026-08-06 实测：这三组 (期末运行均值, 利率, cap) → 模型 FR 已在真实数据上验证过"""
    assert abs(fp.running_model_frs([5.249], 0.0, 100.0, F)[-1] - 0.249) < 1e-6      # SNDK
    assert abs(fp.running_model_frs([-4.538], 1.0, 37.5, F)[-1] - 0.462) < 1e-6      # BTC
    assert abs(fp.running_model_frs([-1.598], 0.0, 100.0, F)[-1] - 0.0) < 1e-9       # SOXL


# ---- 常量一致性：公式的死区半宽必须与 pos_funding_monitor 同源 ----


def test_dead_zone_matches_production():
    """两处若分叉，这里的验证结论就不适用于生产代码了"""
    import pos_funding_monitor as pm
    assert fp.DEAD_ZONE_BP == pm.DEAD_ZONE_BP


# ---- _num ----


def test_num_parses():
    assert fp._num("3.5") == 3.5
    assert fp._num("0") == 0.0


def test_num_default_on_bad_input():
    assert fp._num("", 7.0) == 7.0
    assert fp._num(None, 7.0) == 7.0
    assert fp._num("bad", 7.0) == 7.0


def test_num_default_is_none_by_default():
    assert fp._num("") is None


def test_num_keeps_last_parsable_idiom():
    """流式扫描的用法：非法值不覆盖已记住的值"""
    cur = 1.0
    for v in ("2.0", "", "bad", None):
        cur = fp._num(v, cur)
    assert cur == 2.0


# ---- health_summary：失败可见性 ----


def test_health_all_ok():
    pct, weak = fp.health_summary({"A": 10, "B": 10}, {"A": 0, "B": 0})
    assert pct == 100.0
    assert weak == []


def test_health_flags_weak_instrument():
    """B 成功率 50% < 95% 阈值 → 必须被点名，这是防静默丢样本的核心"""
    pct, weak = fp.health_summary({"A": 10, "B": 5}, {"A": 0, "B": 5})
    assert abs(pct - 75.0) < 1e-9
    assert [b for b, _ in weak] == ["B"]


def test_health_weak_sorted_worst_first():
    ok = {"A": 9, "B": 5, "C": 10}
    bad = {"A": 1, "B": 5, "C": 0}
    _, weak = fp.health_summary(ok, bad)
    assert [b for b, _ in weak] == ["B", "A"]      # 50% 排在 90% 前面


def test_health_empty_no_crash():
    pct, weak = fp.health_summary({}, {})
    assert pct == 0.0 and weak == []


def test_health_ignores_instrument_with_no_attempts():
    pct, weak = fp.health_summary({"A": 0}, {"A": 0})
    assert pct == 0.0 and weak == []


# ---- _mae_verdict 分档 ----


def test_mae_verdict_tiers():
    assert fp._mae_verdict(0.1) == "吻合"
    assert "残差" in fp._mae_verdict(1.0)
    assert "偏离大" in fp._mae_verdict(5.0)


# ---- scan_periods：流式扫描 ----


def _write_csv(tmp_path, rows):
    """按 FIELDS 写一个最小 CSV，未给的字段留空"""
    import csv as _csv
    p = tmp_path / "probe.csv"
    with open(p, "w", newline="", encoding="utf-8-sig") as f:
        w = _csv.DictWriter(f, fieldnames=fp.FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fp.FIELDS})
    return str(p)


def test_scan_groups_by_inst_and_period(tmp_path):
    path = _write_csv(tmp_path, [
        {"ts_bj": "2026-08-07 01:00:00", "inst": "A", "fundingTime_bj": "P1",
         "fundingRate_bp": "1.0", "premium_bp": "2.0"},
        {"ts_bj": "2026-08-07 01:00:01", "inst": "B", "fundingTime_bj": "P1",
         "fundingRate_bp": "3.0", "premium_bp": "4.0"},
        {"ts_bj": "2026-08-07 02:00:00", "inst": "A", "fundingTime_bj": "P2",
         "fundingRate_bp": "5.0", "premium_bp": "6.0"},
    ])
    groups, total, first_ts, last_ts = fp.scan_periods(path)
    assert total == 3
    assert set(groups) == {("A", "P1"), ("B", "P1"), ("A", "P2")}
    assert first_ts == "2026-08-07 01:00:00"
    assert last_ts == "2026-08-07 02:00:00"
    assert list(groups[("A", "P1")]["fr"]) == [1.0]


def test_scan_since_filters_by_string_order(tmp_path):
    path = _write_csv(tmp_path, [
        {"ts_bj": "2026-08-06 23:59:59", "inst": "A", "fundingTime_bj": "P1",
         "fundingRate_bp": "1.0", "premium_bp": "1.0"},
        {"ts_bj": "2026-08-07 00:00:00", "inst": "A", "fundingTime_bj": "P1",
         "fundingRate_bp": "2.0", "premium_bp": "2.0"},
    ])
    groups, total, first_ts, _ = fp.scan_periods(path, since="2026-08-07")
    assert total == 1
    assert first_ts == "2026-08-07 00:00:00"
    assert list(groups[("A", "P1")]["fr"]) == [2.0]


def test_scan_pairs_only_when_both_present(tmp_path):
    """FR 或 premium 缺一个就不入列——否则逐点比模型会错位"""
    path = _write_csv(tmp_path, [
        {"ts_bj": "t1", "inst": "A", "fundingTime_bj": "P", "fundingRate_bp": "1.0", "premium_bp": ""},
        {"ts_bj": "t2", "inst": "A", "fundingTime_bj": "P", "fundingRate_bp": "", "premium_bp": "2.0"},
        {"ts_bj": "t3", "inst": "A", "fundingTime_bj": "P", "fundingRate_bp": "3.0", "premium_bp": "4.0"},
    ])
    groups, total, _, _ = fp.scan_periods(path)
    g = groups[("A", "P")]
    assert total == 3 and g["n"] == 3          # 三行都计入样本数
    assert list(g["fr"]) == [3.0]              # 但只有第三行成对
    assert list(g["prem"]) == [4.0]


def test_scan_tracks_first_last_fr_for_rollover(tmp_path):
    path = _write_csv(tmp_path, [
        {"ts_bj": "t1", "inst": "A", "fundingTime_bj": "P", "fundingRate_bp": "1.0", "premium_bp": "1.0"},
        {"ts_bj": "t2", "inst": "A", "fundingTime_bj": "P", "fundingRate_bp": "9.0", "premium_bp": "1.0"},
    ])
    g = fp.scan_periods(path)[0][("A", "P")]
    assert g["first_fr"] == 1.0 and g["last_fr"] == 9.0
    assert g["first_ts"] == "t1" and g["last_ts"] == "t2"


def test_scan_remembers_last_parsable_ir_and_cap(tmp_path):
    path = _write_csv(tmp_path, [
        {"ts_bj": "t1", "inst": "A", "fundingTime_bj": "P", "fundingRate_bp": "1.0",
         "premium_bp": "1.0", "interestRate_bp": "1.0", "maxFR_bp": "37.5"},
        {"ts_bj": "t2", "inst": "A", "fundingTime_bj": "P", "fundingRate_bp": "1.0",
         "premium_bp": "1.0", "interestRate_bp": "", "maxFR_bp": ""},
    ])
    g = fp.scan_periods(path)[0][("A", "P")]
    assert g["ir"] == 1.0 and g["cap"] == 37.5   # 第二行的空值不该把它们清掉


def test_scan_empty_csv(tmp_path):
    path = _write_csv(tmp_path, [])
    groups, total, first_ts, last_ts = fp.scan_periods(path)
    assert groups == {} and total == 0 and first_ts is None and last_ts is None
