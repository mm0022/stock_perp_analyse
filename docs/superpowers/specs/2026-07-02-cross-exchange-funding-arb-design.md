# 跨所 Funding 套利 · 持仓视角改造设计

日期：2026-07-02
目标文件：`stock_perp_24hvlum_openclaw.py`（保持单文件）

## 1. 背景与问题

现有脚本采集股票化永续（TSLA/NVDA/XAU/SPX 等）在 Binance/OKX/Bybit/Bitget/Gate 的成交量、价格、funding rate，表3做「跨所套利」判定。

表3 现有判定公式 `收益 = 价差(bp) + 低所FR(bp) + 高所FR(bp)` 存在两个根本错误，且是**快照视角**（判断此刻是否开仓），而跨所 funding 套利需要**持仓一段时间**：

1. **量纲错误**：把「一次性价差(bp)」和「每期 funding(bp)」直接相加。价差只发生一次（且平仓反向再付一次），funding 每 4h/8h 反复发生，两者不能相加。
2. **funding 符号错误**：跨所对冲净 funding 应为「高所FR − 低所FR」，现有为「low_bp + high_bp」相加，两所同为正时高估数倍。
3. **未计手续费**：一次建仓含 4 笔成交（开+平各 2 腿），是决定「持仓多久回本」的关键，现有完全忽略。
4. **未看持续性**：当期 funding 差只是快照，下一期可能收窄或反号，无法支撑持仓决策。

本设计把表3从「快照收益」改为「**回本天数 + 稳定性**」的持仓视角。

## 2. 前置：已完成的年化修正

（已实现并验证，非本 spec 的实现范围，此处仅记录）

结算周期不再写死 8h，改为从各所 API 动态获取：
- Binance：`/fapi/v1/fundingInfo` 的 `fundingIntervalHours`，未列出默认 8h
- OKX：funding-rate 响应 `nextFundingTime − fundingTime`
- Bybit：instruments-info 的 `fundingInterval`(分钟)/60
- 统一 helper `annualize(rate, interval_hours) = rate × (24/周期h) × 365 × 100`

实测验证：SPX/XAU/CL 为 4h 周期，个股为 8h，旧写死 8h 会让 4h 标的年化差一倍。

## 3. 策略定义：跨所 Funding 套利

同一标的在两所建 delta 中性对冲头寸：
- **低 funding 所做多**（付 funding）
- **高 funding 所做空**（收 funding）
- 每期净现金流 = 高所FR − 低所FR（恒 ≥ 0），持仓期间反复收取；
- 价差与手续费为一次性成本；
- 持有到 funding 差消失或价差收敛后平仓。

## 4. 数据层

新增 3 个历史 funding 拉取函数，复用现有 `fetch_json`（走代理）：

| 交易所 | 端点 | 时间字段 | rate 字段 |
|--------|------|---------|-----------|
| Binance | `/fapi/v1/fundingRate?symbol=X&limit=N` | `fundingTime`(ms) | `fundingRate` |
| OKX | `/api/v5/public/funding-rate-history?instId=X-USDT-SWAP&limit=N` | `fundingTime`(ms) | `realizedRate`（实际结算值，优先） |
| Bybit | `/v5/market/funding/history?category=linear&symbol=XUSDT&limit=N` | `fundingRateTimestamp`(ms) | `fundingRate` |

- 窗口 `HISTORY_DAYS = 30`；limit 取足够覆盖 30 天（4h 标的约 180 点，取 limit=200 保险，必要时分页）。
- 已验证：三所历史 API 对股票标的均可用，且 `fundingTime` 时间戳三所一致，可直接按时间戳对齐。
- `build_funding_series(exchange, token)` → 返回 `{ts: rate}` 字典。

## 5. 决策模型

对每个标的，在「有历史数据的所」里两两配对，取净 funding 年化最高的一对。指标定义：

```
① 持续收益（历史）
   高所年化% = mean(高所历史 rate) 按其结算周期年化
   低所年化% = mean(低所历史 rate) 按其结算周期年化
   净funding年化% = 高所年化% − 低所年化%
   每天净收益(bp) = 净funding年化% × 100 / 365

② 稳定性（对齐两所时间序列后逐期计算差值）
   对齐 = 两所 series 按 ts 取交集
   diff_t = 高所 rate_t − 低所 rate_t
   符号一致率 = count(diff_t > 0) / count(全部对齐期)
   波动 = std(diff_t)           # 输出参考，不参与硬判定

③ 一次性成本（bp）
   FEE_MODE = 'mixed'：每次建仓一腿 maker、一腿 taker，自动选更省的分配
   每次成本 = min(低所maker + 高所taker, 低所taker + 高所maker)
   总手续费 = 2 × 每次成本                       # 开 + 平
   开仓价差(bp) = (高所现价 − 低所现价) / 高所现价 × 1e4   # 当前合约价；保守假设平仓收敛到 0
   一次性净成本 = 总手续费 − 开仓价差             # 价差有利则抵扣成本

④ 综合判定
   回本天数 = 一次性净成本(bp) / 每天净收益(bp)    # 每天净收益≤0 则回本天数=∞
   持有N天总收益(bp) = 每天净收益 × N − 一次性净成本   （N = HISTORY_DAYS 展示口径，默认 30）
   推荐 = (净funding年化% > 0) 且 (符号一致率 ≥ SIGN_CONSISTENCY_MIN) 且 (0 < 回本天数 ≤ BREAKEVEN_DAYS_MAX)
```

判定输出附原因：不推荐时标注「funding不稳」（一致率不足）/「回本太慢」（超阈值）/「净funding≤0」。

## 6. 参数配置（配置区新增）

```python
HISTORY_DAYS = 30
FEE = {                              # 单笔单腿费率，单位 bp
  'Binance': {'maker': 0,   'taker': 1.5},   # tradfi VIP4
  'Bybit':   {'maker': 0,   'taker': 1.25},  # tradfi Pro6 / G9
  'OKX':     {'maker': 0.8, 'taker': 2.7},   # tradfi VIP4 / 分组2
}
FEE_MODE = 'mixed'                   # 一腿 maker 一腿 taker，自动选更省分配
SIGN_CONSISTENCY_MIN = 0.8           # 符号一致率阈值
BREAKEVEN_DAYS_MAX = 15              # 回本天数阈值
MIN_VOLUME = 1_000_000               # 沿用成交量过滤
```

参与配对的交易所限定为已有价格+funding+历史数据的三所：Binance / OKX / Bybit。

## 7. 输出

**新表3（跨所 funding 套利 · 持仓视角）**，替换现有快照表：

| Symbol | 做多所(低FR) | 做空所(高FR) | 净年化% | 每天bp | 一致率 | 手续费bp | 价差bp | 回本天数 | 持有30天bp | 判定 |
|--------|-----------|-----------|--------|-------|-------|---------|-------|--------|----------|------|

- Slack：推送同结构精简版（沿用现有 `send_slack_report` 改造）。
- CSV：落盘上述指标，便于跨时段追踪 pair 的稳定性变化。
- 表1（成交量）、表2（期现套利）本次不改动。

## 8. 实现范围与文件结构

保持单文件 `stock_perp_24hvlum_openclaw.py`，新增「历史 funding 与持仓分析」区块：

- `get_binance_funding_history(tokens, days)` / `get_okx_funding_history(...)` / `get_bybit_funding_history(...)`
- `build_funding_series(...)`：对齐两所时间序列
- `analyze_pair(token, ex_low, ex_high, series, prices, ...)`：计算第 5 节全部指标
- 改写 `main()` 表3 段落 + 对应 Slack 发送函数

**不在本次范围**（记录待办，另行处理）：
- symbol 映射 bug：`GOOG` 实为 `GOOGL`USDT、`NG` 实为 `NATGAS`USDT（Binance），会导致这些标的匹配不上。
- 表2 期现套利未真正实现（合约价列写死 `-`）。

## 9. 验证方式

- 语法检查 + 实跑一次，确认三所历史拉取成功、时间对齐正确。
- 抽取一个已知 pair（如 SPX 在 Binance vs OKX）手工核对：净年化差、符号一致率、回本天数与手算一致。
- 边界：某所无该标的 / 历史为空 / 每天净收益≤0（回本天数应为 ∞ 且判定为观察）。
```
