"""OKX funding 机制实测记录器

结论（2026-08-06，12000+ 条跨夜采样，SNDK/SOXL/BTC 共 4 个完整周期）：
**当期 fundingRate 是本期 premium 的运行累积均值**，代入死区公式即得

    FR(t) = clamp( avg + clamp(利率 − avg, ±5bp), ±cap )    avg = 期初到 t 的 premium 均值

算子顺序是「先平均后死区」，非线性使其与「先死区后平均」不等价：
SOXL 前者 MAE 0.000bp / 后者 0.943bp；BTC 前者 0.134bp / 后者 0.174bp。
窗口扫描 MAE 随窗口长度单调降至 360–480min 触底，短窗口(5~60min)明显更差 → 全期累积
而非滑动窗口。（均值是否按时间加权，等间隔采样区分不出来，未定。）

⚠️ 早期版本这里写着「不支持运行均值模型」，是错的，已推翻。错因：当时只跑 84 个样本
（约 20 分钟）且恰好落在周期**末尾**，却按「30 个采样」估算新样本影响力——那是我们
自己的采样数，不是交易所的累积样本数。周期末尾交易所侧分母已近 8 小时，新样本偏离
8bp 只推动约 8/480 ≈ 0.017bp，看着就是「不动」。跑满整期后 BTC 的 FR 期内实际移动
0.82~0.97bp，SNDK 从 0 走到 +6.04bp。同理 settState 恒为 'settled' 也不是费率已定的证据。

FR 全期不动的**常见原因是死区钳制而非费率锁定**：运行均值 premium 只要落在
[利率−5bp, 利率+5bp] 内，funding 就恒等于利率，哪怕瞬时 premium 摆动上百 bp。
SOXL 四个周期 FR 恒为 0 即如此（运行均值始终 −4.5~−0.4bp，瞬时摆动却达 102bp）。

已知边界：21 个周期里 20 个吻合（后半期 MAE 多在 0.0~0.2bp），唯一显著偏离的是
SNDK 2026-08-06 08:00 期（期末差 −5.79bp，后半期 MAE 1.67bp）——恰是 SanDisk 财报当期，
premium 尖峰达 +199bp、现货 −11.6%。极端行情下本脚本的等间隔采样与交易所的取样/加权
出现分歧，方向未查明。日常行情不受影响。

无法从接口回溯「历史上某时刻显示的费率」，所以只能自己记。

用法：
    python3 funding_probe.py                       # 前台跑，Ctrl+C 停
    python3 funding_probe.py --interval 15         # 轮次周期(秒)，默认 15
    python3 funding_probe.py --insts SNDK,BTC      # 标的，默认 SNDK,SOXL,BTC
    python3 funding_probe.py --workers 3           # 并发标的数，默认 3（OKX 限频严格）
    python3 funding_probe.py --no-mark             # 不取 mark/index，每标的 3 个请求降为 1
落盘：funding_probe.csv（追加，断点续跑不覆盖）
分析：python3 funding_probe.py --report           # 读 CSV 出结论，不联网
      python3 funding_probe.py --report --since 2026-08-07   # 只分析该时间之后

多标的注意：
  - `--interval` 是**轮次周期**，睡眠按本轮耗时扣减，所以加标的不会让节奏漂移
  - 标的多时先用 `--no-mark` 保采样密度；mark/index 只是辅助列，模型输入量是 premium
  - 部分标的取不到会**逐轮点名**，每 20 轮打一次成功率摘要；成功率 <95% 的单独告警。
    采样密度直接决定运行均值结论的可信度，静默丢样本等于结论作废
"""
import csv
import os
import sys
import time
from array import array
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

import requests

CSV_PATH = "funding_probe.csv"
DEFAULT_INSTS = ("SNDK", "SOXL", "BTC")

MAX_WORKERS = 3         # 并发标的数。OKX 公共接口限频严格(50011)，与主脚本 get_okx_discount 取同一保守值
RETRY_MAX = 3           # 单次请求重试次数
RETRY_BASE_S = 0.4      # 退避基数，第 n 次重试等 RETRY_BASE_S * 2**n 秒
HEALTH_EVERY = 20       # 每多少轮打印一次健康度摘要
SUCCESS_WARN_PCT = 95.0 # 成功率低于此值的标的会被单独点名
FIELDS = ["ts_utc", "ts_bj", "inst", "fundingRate_bp", "premium_bp", "interestRate_bp",
          "settState", "method", "formulaType", "impactValue", "minFR_bp", "maxFR_bp",
          "fundingTime_bj", "prevFundingTime_bj", "nextFundingTime_bj", "nextFundingRate_bp",
          "markPx", "idxPx", "mark_vs_idx_bp"]


def pick_proxies():
    """出口探测：直连 → PROXY_URL。本机出口会来回切，硬编码任一种都会周期性失效。"""
    cands = [({}, "直连")]
    pu = os.environ.get("PROXY_URL", "")
    if pu:
        cands.append(({"http": pu, "https": pu}, f"代理 {pu}"))
    for prox, name in cands:
        try:
            if requests.get("https://www.okx.com/api/v5/public/time",
                            proxies=prox, timeout=8).status_code == 200:
                return prox, name
        except Exception:
            continue
    return None, "全部不通"


PROXIES = {}


def get(url, params=None, retries=RETRY_MAX):
    """→ (json, err)。err 非 None 表示失败，且**说明原因**——原版直接吞掉异常返回 None，
    多标的长跑时会变成静默丢样本，而采样密度直接决定运行均值结论的可信度。

    只对限频(50011 / HTTP 429)和网络异常重试；业务错误码重试无益，立即返回。
    """
    err = None
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, params=params, timeout=10, proxies=PROXIES)
            if r.status_code == 200:
                j = r.json()
                code = j.get("code")
                if code in (None, "0"):
                    return j, None
                if code != "50011":            # 非限频业务错误，重试无意义
                    return None, f"code={code}"
                err = "限频50011"
            elif r.status_code == 429:
                err = "HTTP429"
            else:
                return None, f"HTTP{r.status_code}"
        except Exception as e:
            err = type(e).__name__
        if attempt < retries:
            time.sleep(RETRY_BASE_S * (2 ** attempt))
    return None, err


def bj(ms):
    """毫秒 → 北京时间字符串；缺失返回空"""
    try:
        return (datetime.fromtimestamp(int(ms) / 1000, timezone.utc)
                + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return ""


def bp(v):
    try:
        return round(float(v) * 10000, 4)
    except (TypeError, ValueError):
        return None


def sample_one(base, want_mark=True):
    """→ (行 dict, err)。拿不到 funding-rate 才算失败(行为 None)——premium 在它里面，是模型输入量。

    mark/index 缺失**不算失败**：它们只是辅助列（用于对比「官方 premium」与「mark−index 近似」），
    缺了照样落盘。want_mark=False 时干脆不请求，把每标的请求数从 3 降到 1，
    在多标的场景下可把可达采样密度提高约 3 倍。
    """
    inst = f"{base}-USDT-SWAP"
    d, err = get("https://www.okx.com/api/v5/public/funding-rate", {"instId": inst})
    if not (d and d.get("data")):
        return None, err or "data为空"
    x = d["data"][0]
    mark = idx = rel = None
    if want_mark:
        mk, _ = get("https://www.okx.com/api/v5/public/mark-price",
                    {"instType": "SWAP", "instId": inst})
        ix, _ = get("https://www.okx.com/api/v5/market/index-tickers", {"instId": f"{base}-USDT"})
        try:
            mark = float(mk["data"][0]["markPx"])
            idx = float(ix["data"][0]["idxPx"])
            rel = round((mark - idx) / idx * 10000, 4) if idx > 0 else None
        except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError):
            pass
    now = datetime.now(timezone.utc)
    return {
        "ts_utc": now.strftime("%Y-%m-%d %H:%M:%S"),
        "ts_bj": (now + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S"),
        "inst": base,
        "fundingRate_bp": bp(x.get("fundingRate")),
        "premium_bp": bp(x.get("premium")),
        "interestRate_bp": bp(x.get("interestRate")),
        "settState": x.get("settState"),
        "method": x.get("method"),
        "formulaType": x.get("formulaType"),
        "impactValue": x.get("impactValue"),
        "minFR_bp": bp(x.get("minFundingRate")),
        "maxFR_bp": bp(x.get("maxFundingRate")),
        "fundingTime_bj": bj(x.get("fundingTime")),
        "prevFundingTime_bj": bj(x.get("prevFundingTime")),
        "nextFundingTime_bj": bj(x.get("nextFundingTime")),
        # 空字符串表示 OKX 还没给出下期预测值——本身就是个信号，原样记下
        "nextFundingRate_bp": bp(x.get("nextFundingRate")),
        "markPx": mark, "idxPx": idx, "mark_vs_idx_bp": rel,
    }, None


def sample_batch(insts, workers=MAX_WORKERS, want_mark=True):
    """并发采样一轮 → (rows, {inst: 失败原因})。

    串行版本约 1.5s/标的，73 个标的会把轮次间隔拖到 ~125s，采样密度掉到 1/6；
    而密度直接决定运行均值结论的可信度，所以必须并发。并发数取保守值以免触发限频，
    真触发了由 get() 的退避重试兜住。
    """
    rows, errs = [], {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(sample_one, b, want_mark): b for b in insts}
        for fut, b in futs.items():
            try:
                row, err = fut.result()
            except Exception as e:                      # 兜住 sample_one 里未预料的异常
                row, err = None, f"{type(e).__name__}: {e}"
            if row:
                rows.append(row)
            else:
                errs[b] = err or "未知"
    rows.sort(key=lambda r: r["inst"])   # 并发完成顺序不定，排序后 CSV 可读且可 diff
    return rows, errs


def health_summary(ok, bad):
    """→ (总成功率%, 需点名的弱标的列表[(inst, 成功率%)])。成功率低于 SUCCESS_WARN_PCT 才点名。"""
    tot_ok, tot_bad = sum(ok.values()), sum(bad.values())
    n = tot_ok + tot_bad
    pct = tot_ok / n * 100 if n else 0.0
    weak = []
    for b in ok:
        m = ok[b] + bad[b]
        if m:
            p = ok[b] / m * 100
            if p < SUCCESS_WARN_PCT:
                weak.append((b, p))
    weak.sort(key=lambda x: x[1])
    return pct, weak


def append_rows(rows):
    new = not os.path.isfile(CSV_PATH)
    with open(CSV_PATH, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerows(rows)


def run(insts, interval, workers=MAX_WORKERS, want_mark=True):
    """采样主循环。

    interval 是**轮次周期**而非轮末等待：睡眠时长按本轮耗时扣减，所以标的数变化时
    采样节奏保持稳定（原版是 sleep(interval) 在采样之后，3 标的实测 15s 设定跑出 19.5s）。
    采样耗时超过 interval 时不睡，退化为背靠背连续采。
    """
    global PROXIES
    PROXIES, name = pick_proxies()
    if PROXIES is None:
        print("❌ 网络出口全部不通，退出")
        return
    print(f"🌐 出口：{name}｜标的 {len(insts)} 个 {','.join(insts)}")
    print(f"   轮次周期 {interval}s｜并发 {workers}｜"
          f"{'含' if want_mark else '不含'} mark/index｜落盘 {CSV_PATH}")
    print("   Ctrl+C 停止。跨过结算点(北京 00/08/16 点)后跑 --report 分析\n")
    n = rnd = 0
    ok = {b: 0 for b in insts}
    bad = {b: 0 for b in insts}
    last_key = {}
    while True:
        t0 = time.time()
        rows, errs = sample_batch(insts, workers, want_mark)
        rnd += 1
        for b in insts:
            (bad if b in errs else ok)[b] += 1
        if rows:
            append_rows(rows)
            n += len(rows)
            for r in rows:
                # 只在「本期费率变了」或「换期了」时打屏，避免刷屏但不漏关键事件
                prev = last_key.get(r["inst"])
                if prev is None or prev[0] != r["fundingTime_bj"]:
                    print(f"🔔 {r['ts_bj']} {r['inst']:<5} 换期 → 本期结算于 {r['fundingTime_bj']}  "
                          f"FR={r['fundingRate_bp']}bp state={r['settState']}")
                elif prev[1] != r["fundingRate_bp"]:
                    print(f"📈 {r['ts_bj']} {r['inst']:<5} FR 变化 {prev[1]} → {r['fundingRate_bp']}bp  "
                          f"(prem={r['premium_bp']}bp state={r['settState']})")
                last_key[r["inst"]] = (r["fundingTime_bj"], r["fundingRate_bp"])
        # 部分失败也要说出来：原版只在「全部失败」时才提示，个别标的静默丢样本看不见
        if errs:
            shown = sorted(errs.items())[:6]
            more = f" …另 {len(errs) - 6} 个" if len(errs) > 6 else ""
            print(f"⚠️ 第{rnd}轮 {len(errs)}/{len(insts)} 个取不到: "
                  + ", ".join(f"{b}({e})" for b, e in shown) + more)
        elapsed = time.time() - t0
        # 进度按【轮次】计，不按行数：原版 n % 100 在 20 标的时变成每 5 轮刷屏一次
        if rnd % HEALTH_EVERY == 0:
            pct, weak = health_summary(ok, bad)
            line = (f"   …第{rnd}轮，已记 {n} 条，本轮耗时 {elapsed:.1f}s，"
                    f"整体成功率 {pct:.1f}%")
            if weak:
                line += (f"\n   ⚠️ 成功率偏低(<{SUCCESS_WARN_PCT:.0f}%): "
                         + ", ".join(f"{b} {p:.0f}%" for b, p in weak[:8]))
            print(line)
        time.sleep(max(0.0, interval - elapsed))


DEAD_ZONE_BP = 5.0      # 死区半宽（利率两侧各 5bp）；权威定义在 pos_funding_monitor
FR_STILL_BP = 0.01      # FR 跨度小于此值视为「全期不动」
MAE_GOOD_BP = 0.5       # 逐点误差低于此值算模型吻合
MAE_OK_BP = 2.0         # 高于此值则模型或参数存疑


def running_model_frs(prems, ir_bp, cap_bp, formula):
    """逐点把「期初到当前的 premium 运行均值」代入公式，返回模型 FR 序列。

    formula 由调用方传入（用 pos_funding_monitor.funding_if_premium_held），
    这样验证的就是生产代码在用的那个公式本身——若在这里另写一份，验证便失去意义。
    """
    out, s = [], 0.0
    for i, p in enumerate(prems, 1):
        s += p
        out.append(formula(s / i, ir_bp, cap_bp))
    return out


def classify_period(fr_span, avg_prem_bp, ir_bp, prem_span):
    """判定本期 FR 行为 → (标签, 说明)。

    早期版本把「FR 不动 + premium 摆动大」直接判成「本期费率已定」，漏掉了第三种可能：
    死区钳制。只要**运行均值** premium 落在 [利率±5bp] 内，funding 就恒等于利率，
    瞬时 premium 摆多大都无关。SOXL 四个周期恒为 0 全是这个原因，却被旧逻辑误判。
    """
    in_dead = abs(avg_prem_bp - ir_bp) <= DEAD_ZONE_BP
    if fr_span < FR_STILL_BP:
        if in_dead:
            return ("死区钳制",
                    (f"运行均值 prem {avg_prem_bp:+.2f}bp 落在死区 "
                     f"[{ir_bp - DEAD_ZONE_BP:+.1f}, {ir_bp + DEAD_ZONE_BP:+.1f}]bp 内，"
                     f"funding 恒等于利率 {ir_bp:g}bp；瞬时摆动 {prem_span:.1f}bp 与结果无关"))
        return ("⚠ 待查",
                (f"运行均值 prem {avg_prem_bp:+.2f}bp 在死区外，但 FR 全期不动 —— "
                 f"这才是「费率锁定」的候选证据，需人工核查"))
    return ("期内变化",
            f"FR 跨度 {fr_span:.3f}bp，与运行均值模型一致（均值随样本累积而移动）")


def _num(v, default=None):
    """单值解析为 float，失败返回 default。

    流式扫描里靠它「记住最后一个可解析值」：`g['ir'] = _num(row['interestRate_bp'], g['ir'])`。
    利率/cap 在一个周期内不变，取末值即可。
    """
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _mae_verdict(mae):
    if mae < MAE_GOOD_BP:
        return "吻合"
    if mae < MAE_OK_BP:
        return "有残差(采样频率/加权方式与交易所不完全一致)"
    return "偏离大，模型或参数存疑"


def scan_periods(path, since=None):
    """流式扫描 CSV → (groups, 总行数, 首ts, 末ts)。groups 键为 (inst, fundingTime_bj)。

    不用 `list(csv.DictReader(f))`：那样每行一个 dict 实测约 5.4KB，20 万行就 1GB，
    73 标的跑一个月约 151 万行 → 约 7.8GB，必然 OOM。
    这里每周期只留 fr/prem 两个 array('d')（16 字节/行，同规模约 24MB），其余聚合成标量。

    since 用字符串直接比较：ts_bj 是 'YYYY-MM-DD HH:MM:SS'，字典序即时间序。
    """
    groups = {}
    total = 0
    first_ts = last_ts = None
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            ts = r.get("ts_bj") or ""
            if since and ts < since:
                continue
            total += 1
            if first_ts is None:
                first_ts = ts
            last_ts = ts
            key = (r.get("inst") or "", r.get("fundingTime_bj") or "")
            g = groups.get(key)
            if g is None:
                g = groups[key] = {"fr": array("d"), "prem": array("d"), "n": 0,
                                   "states": set(), "ir": 0.0, "cap": None,
                                   "first_fr": None, "last_fr": None,
                                   "first_ts": ts, "last_ts": ts}
            g["n"] += 1
            g["last_ts"] = ts
            g["states"].add(r.get("settState") or "")
            g["ir"] = _num(r.get("interestRate_bp"), g["ir"])
            g["cap"] = _num(r.get("maxFR_bp"), g["cap"])
            fr = _num(r.get("fundingRate_bp"))
            pm = _num(r.get("premium_bp"))
            if fr is not None:
                if g["first_fr"] is None:
                    g["first_fr"] = fr
                g["last_fr"] = fr
            # 成对才入列：逐点比模型必须 FR 与 premium 来自同一采样，分别过滤会错位
            if fr is not None and pm is not None:
                g["fr"].append(fr)
                g["prem"].append(pm)
    return groups, total, first_ts, last_ts


def report(since=None):
    """只读 CSV 出结论，不联网。回答：FR 期内变不变、是否死区钳制、与运行均值模型差多少"""
    if not os.path.isfile(CSV_PATH):
        print(f"❌ 没有 {CSV_PATH}，先跑采样")
        return
    # 惰性导入：只有分析路径需要，长跑的采样路径不必依赖 pandas 链
    try:
        from pos_funding_monitor import funding_if_premium_held as formula
    except Exception as e:
        print(f"❌ 无法导入 pos_funding_monitor 的公式，模型对比不可用: {e}")
        return

    groups, total, first_ts, last_ts = scan_periods(CSV_PATH, since)
    if not total:
        print("❌ 没有符合条件的数据"
              + (f"（--since {since} 之后无记录）" if since else "（CSV 为空）"))
        return
    scope = f"（--since {since}）" if since else ""
    print(f"共 {total} 条，{first_ts} → {last_ts}{scope}\n")

    for inst in sorted({k[0] for k in groups}):
        fts = sorted(ft for (i, ft) in groups if i == inst)
        n_inst = sum(groups[(inst, ft)]["n"] for ft in fts)
        print(f"=== {inst}（{n_inst} 条，{len(fts)} 个周期）===")
        for ft in fts:
            g = groups[(inst, ft)]
            frs, prems = g["fr"], g["prem"]
            if not frs:
                print(f"  结算于 {ft}  样本{g['n']:>4}  ⚠️ 无成对的 FR/premium，跳过")
                continue
            moved = max(frs) - min(frs)
            prem_span = max(prems) - min(prems)
            states = ",".join(sorted(s for s in g["states"] if s))
            print(f"  结算于 {ft}  样本{g['n']:>4}  "
                  f"FR {min(frs):+.3f}~{max(frs):+.3f}bp (跨度{moved:.3f})  "
                  f"prem {min(prems):+.2f}~{max(prems):+.2f}bp  state={states}")

            ir, cap = g["ir"], g["cap"]
            avg = sum(prems) / len(prems)
            label, why = classify_period(moved, avg, ir, prem_span)
            print(f"     判定: {label} — {why}")

            model = running_model_frs(prems, ir, cap, formula)
            errs = [abs(m - f) for m, f in zip(model, frs) if m is not None]
            print(f"     模型: 期末运行均值 prem {avg:+.3f}bp → 模型FR {model[-1]:+.3f}bp  "
                  f"实际FR {frs[-1]:+.3f}bp  差 {model[-1] - frs[-1]:+.3f}bp")
            if errs:
                mae = sum(errs) / len(errs)
                # 期初 n 很小时运行均值本身剧烈摆动，误差大是构造使然而非模型失败，
                # 会把「最大误差」污染成误导性数字。后半期均值已稳定，才是能否预测结算值的指标。
                tail = errs[len(errs) // 2:]
                mae_t = sum(tail) / len(tail)
                print(f"     逐点: 全期 MAE {mae:.3f}bp/最大 {max(errs):.3f}bp   "
                      f"后半期 MAE {mae_t:.3f}bp/最大 {max(tail):.3f}bp → {_mae_verdict(mae_t)}")
                print(f"           (n={len(errs)}, 利率 {ir:g}bp, "
                      f"cap {cap if cap is None else f'{cap:g}'}bp)")
        # 换期瞬间：上期最后一条 vs 下期第一条
        for a, b2 in zip(fts, fts[1:]):
            ga, gb = groups[(inst, a)], groups[(inst, b2)]
            print(f"  换期 {a} → {b2}: FR {ga['last_fr']} → {gb['first_fr']}bp"
                  f"  (间隔 {ga['last_ts']} → {gb['first_ts']})")
        print()


def _opt(name, default, cast=str):
    """读取 --name VALUE；缺失或值非法则返回 default（非法时明确报错，不静默用默认值）"""
    if name not in sys.argv:
        return default
    i = sys.argv.index(name) + 1
    if i >= len(sys.argv):
        print(f"❌ {name} 缺少取值，用默认 {default}")
        return default
    try:
        return cast(sys.argv[i])
    except (TypeError, ValueError):
        print(f"❌ {name} 取值 {sys.argv[i]!r} 非法，用默认 {default}")
        return default


if __name__ == "__main__":
    if "--report" in sys.argv:
        report(since=_opt("--since", None))
    else:
        ins = [s.strip().upper()
               for s in _opt("--insts", ",".join(DEFAULT_INSTS)).split(",") if s.strip()]
        try:
            run(ins,
                interval=_opt("--interval", 15, int),
                workers=_opt("--workers", MAX_WORKERS, int),
                want_mark="--no-mark" not in sys.argv)
        except KeyboardInterrupt:
            print("\n🛑 停止。分析：python3 funding_probe.py --report")
