# Strategy research

Generated 2026-10-02T17:40+00:00. In-sample 2019-01-01 ~ 2023-12-31, out-of-sample 2024-01-01 ~ 2026-10-02.
864 configurations evaluated, 574 passed the in-sample filter (>= 40 trades, profit factor >= 1.1).

## Chosen configuration (selected on in-sample only)

| param | value |
|---|---|
| tf | 4h |
| swing_len | 5 |
| weights | sentiment |
| threshold | 40 |
| sides | long_only |
| exit | trail |
| sl_mode | atr |

| metric | in-sample | out-of-sample |
|---|---|---|
| total_return_pct | 63.56 | 36.75 |
| cagr_pct | 10.35 | 12.05 |
| max_dd_pct | -7.49 | -6.87 |
| sharpe | 1.46 | 1.51 |
| trades | 116 | 75 |
| win_rate_pct | 51.7 | 52.0 |
| profit_factor | 1.97 | 1.93 |
| avg_r | 0.455 | 0.445 |
| exposure_pct | 25.8 | 32.1 |

## Baselines

| baseline | period | return % | CAGR % | max DD % | Sharpe |
|---|---|---|---|---|---|
| buy_hold | is | 1008.4 | 61.8 | -76.6 | 1.06 |
| buy_hold | oos | 101.4 | 29.0 | -53.0 | 0.77 |
| ema200_filter | is | 725.5 | 52.6 | -55.2 | 1.1 |
| ema200_filter | oos | 90.4 | 26.4 | -34.5 | 0.81 |

## Robustness

- Out-of-sample profitable: 76.0% of all configs, 100.0% of the in-sample top 20.
- Median out-of-sample Sharpe of the in-sample top 20: 1.26.

## Median Sharpe by dimension

**tf**: 1h → IS 0.12 / OOS 0.08, 4h → IS 0.56 / OOS 0.83

**swing_len**: 5 → IS 0.44 / OOS 0.45, 10 → IS 0.29 / OOS 0.51, 20 → IS 0.38 / OOS 0.46

**weights**: base → IS 0.36 / OOS 0.43, no_location → IS 0.35 / OOS 0.46, sentiment → IS 0.34 / OOS 0.52

**threshold**: 30 → IS 0.37 / OOS 0.47, 40 → IS 0.33 / OOS 0.43, 50 → IS 0.32 / OOS 0.39, 60 → IS 0.4 / OOS 0.56

**sides**: both → IS 0.2 / OOS 0.48, long_only → IS 0.48 / OOS 0.46

**exit**: fixed_2R → IS 0.44 / OOS 0.45, partial → IS 0.32 / OOS 0.5, trail → IS 0.29 / OOS 0.48

**sl_mode**: atr → IS 0.34 / OOS 0.39, structure → IS 0.35 / OOS 0.49

Strategy risk is 1% of equity per trade; returns scale roughly linearly with that choice, drawdowns too. Fees 0.05%/side + 0.02% slippage on market fills.