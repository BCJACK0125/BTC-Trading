# Leverage flush / crowding study (open-interest proxies)

Generated 2026-10-05T13:38+00:00. OI from Binance's public USDT-M metrics archive since 2020-09-01. In-sample 2020-09-01 ~ 2023-12-31 (baseline re-measured on it), out-of-sample 2024-01-01 ~ 2026-10-05.

## Event study: forward return (%) after a long flush inside a daily uptrend

| rule | period | events | event_24h | event_24h_up_pct | base_24h | event_72h | event_72h_up_pct | base_72h |
|---|---|---|---|---|---|---|---|---|
| 4h 內跌 ≥ 1.5 ATR 且 OI 跌幅前 5% | is | 18 | 1.12 | 61.0 | 0.26 | 1.32 | 67.0 | 0.8 |
| 4h 內跌 ≥ 1.5 ATR 且 OI 跌幅前 5% | oos | 14 | 0.6 | 50.0 | 0.11 | 0.05 | 50.0 | 0.33 |
| 8h 內跌 ≥ 2.5 ATR 且 OI 跌幅前 5% | is | 7 | 0.73 | 57.0 | 0.26 | 0.89 | 57.0 | 0.8 |
| 8h 內跌 ≥ 2.5 ATR 且 OI 跌幅前 5% | oos | 4 | 1.93 | 50.0 | 0.11 | 2.78 | 75.0 | 0.33 |
| 12h 內跌 ≥ 2.5 ATR 且 OI 跌幅前 2% | is | 9 | 0.82 | 56.0 | 0.26 | 0.56 | 78.0 | 0.8 |
| 12h 內跌 ≥ 2.5 ATR 且 OI 跌幅前 2% | oos | 2 | 0.28 | 50.0 | 0.11 | 2.07 | 100.0 | 0.33 |

`base_*` is the average forward return of every bar in a daily uptrend over the same period.

## Strategy variants

Baseline: IS Sharpe 1.19, OOS Sharpe 1.51. Of 18 variants, 9 beat it in-sample, 7 out-of-sample, 3 in both.
Best in-sample variant (多單洗盤後也進場：4h 內跌 ≥ 1.5 ATR 且 OI 跌幅前 5%): deflated Sharpe 62.3% over 882 trials.

| family | label | is_sharpe | oos_sharpe | is_cagr_pct | oos_cagr_pct | is_max_dd_pct | oos_max_dd_pct | is_trades | oos_trades | added_or_blocked |
|---|---|---|---|---|---|---|---|---|---|---|
| flush_add | 多單洗盤後也進場：4h 內跌 ≥ 1.5 ATR 且 OI 跌幅前 5% | 1.36 | 1.37 | 9.79 | 11.19 | -7.41 | -8.67 | 83 | 81 | 30 |
| flush_add | 多單洗盤後也進場：12h 內跌 ≥ 1.5 ATR 且 OI 跌幅前 5% | 1.36 | 1.5 | 9.92 | 12.28 | -7.93 | -6.87 | 86 | 80 | 55 |
| crowd_filter | 槓桿擁擠時不進場：24h OI 增幅前 10% 且價格上漲 | 1.34 | 1.53 | 9.49 | 12.17 | -6.49 | -6.55 | 79 | 74 | 347 |
| flush_add | 多單洗盤後也進場：4h 內跌 ≥ 1.5 ATR 且 OI 跌幅前 2% | 1.32 | 1.47 | 9.51 | 12.03 | -7.41 | -7.7 | 83 | 79 | 21 |
| crowd_filter | 槓桿擁擠時不進場：72h OI 增幅前 10% 且價格上漲 | 1.25 | 1.51 | 8.84 | 12.02 | -7.49 | -6.87 | 79 | 76 | 330 |
| crowd_filter | 槓桿擁擠時不進場：24h OI 增幅前 5% 且價格上漲 | 1.24 | 1.51 | 8.79 | 12.01 | -7.49 | -6.87 | 81 | 76 | 172 |
| flush_add | 多單洗盤後也進場：12h 內跌 ≥ 1.5 ATR 且 OI 跌幅前 2% | 1.24 | 1.48 | 9.07 | 11.9 | -7.93 | -6.87 | 87 | 78 | 37 |
| flush_add | 多單洗盤後也進場：8h 內跌 ≥ 1.5 ATR 且 OI 跌幅前 5% | 1.23 | 1.58 | 8.97 | 13.08 | -7.37 | -6.91 | 85 | 79 | 50 |
| flush_add | 多單洗盤後也進場：12h 內跌 ≥ 2.5 ATR 且 OI 跌幅前 5% | 1.23 | 1.55 | 8.99 | 12.57 | -7.34 | -6.55 | 86 | 77 | 22 |
| baseline | 目前策略 | 1.19 | 1.51 | 8.55 | 12.01 | -7.49 | -6.87 | 82 | 76 | 0 |
| crowd_filter | 槓桿擁擠時不進場：72h OI 增幅前 2% 且價格上漲 | 1.19 | 1.51 | 8.47 | 12.01 | -7.49 | -6.87 | 82 | 76 | 33 |
| crowd_filter | 槓桿擁擠時不進場：72h OI 增幅前 5% 且價格上漲 | 1.18 | 1.51 | 8.46 | 12.01 | -7.49 | -6.87 | 82 | 76 | 147 |
| flush_add | 多單洗盤後也進場：12h 內跌 ≥ 2.5 ATR 且 OI 跌幅前 2% | 1.18 | 1.45 | 8.56 | 11.6 | -7.34 | -6.87 | 86 | 77 | 17 |
| flush_add | 多單洗盤後也進場：4h 內跌 ≥ 2.5 ATR 且 OI 跌幅前 5% | 1.14 | 1.57 | 8.21 | 12.56 | -8.44 | -6.87 | 83 | 75 | 8 |
| flush_add | 多單洗盤後也進場：8h 內跌 ≥ 2.5 ATR 且 OI 跌幅前 2% | 1.14 | 1.6 | 8.23 | 12.81 | -8.38 | -6.87 | 84 | 76 | 9 |
| flush_add | 多單洗盤後也進場：4h 內跌 ≥ 2.5 ATR 且 OI 跌幅前 2% | 1.14 | 1.57 | 8.21 | 12.56 | -8.44 | -6.87 | 83 | 75 | 6 |
| flush_add | 多單洗盤後也進場：8h 內跌 ≥ 2.5 ATR 且 OI 跌幅前 5% | 1.14 | 1.66 | 8.23 | 13.36 | -8.38 | -6.87 | 84 | 75 | 14 |
| crowd_filter | 槓桿擁擠時不進場：24h OI 增幅前 2% 且價格上漲 | 1.14 | 1.51 | 8.13 | 12.01 | -7.49 | -6.87 | 83 | 76 | 63 |
| flush_add | 多單洗盤後也進場：8h 內跌 ≥ 1.5 ATR 且 OI 跌幅前 2% | 1.09 | 1.51 | 7.99 | 12.12 | -8.38 | -6.87 | 86 | 78 | 30 |