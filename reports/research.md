# Strategy research

Generated 2026-10-04T14:50+00:00. In-sample 2019-01-01 ~ 2023-12-31, out-of-sample 2024-01-01 ~ 2026-10-04.
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
| total_return_pct | 63.56 | 36.69 |
| cagr_pct | 10.35 | 12.0 |
| max_dd_pct | -7.49 | -6.87 |
| sharpe | 1.46 | 1.5 |
| trades | 116 | 76 |
| win_rate_pct | 51.7 | 52.6 |
| profit_factor | 1.97 | 1.94 |
| avg_r | 0.455 | 0.444 |
| exposure_pct | 25.8 | 33.0 |

## Baselines

| baseline | period | return % | CAGR % | max DD % | Sharpe |
|---|---|---|---|---|---|
| buy_hold | is | 1008.4 | 61.8 | -76.6 | 1.06 |
| buy_hold | oos | 101.1 | 28.8 | -53.0 | 0.77 |
| ema200_filter | is | 725.5 | 52.6 | -55.2 | 1.1 |
| ema200_filter | oos | 90.1 | 26.2 | -34.5 | 0.81 |

## Robustness

- Out-of-sample profitable: 76.3% of all configs, 100.0% of the in-sample top 20.
- Median out-of-sample Sharpe of the in-sample top 20: 1.27.

## Median Sharpe by dimension

**tf**: 1h → IS 0.12 / OOS 0.08, 4h → IS 0.56 / OOS 0.83

**swing_len**: 5 → IS 0.44 / OOS 0.44, 10 → IS 0.29 / OOS 0.51, 20 → IS 0.38 / OOS 0.46

**weights**: base → IS 0.36 / OOS 0.44, no_location → IS 0.35 / OOS 0.47, sentiment → IS 0.34 / OOS 0.52

**threshold**: 30 → IS 0.37 / OOS 0.48, 40 → IS 0.33 / OOS 0.44, 50 → IS 0.32 / OOS 0.39, 60 → IS 0.4 / OOS 0.56

**sides**: both → IS 0.2 / OOS 0.48, long_only → IS 0.48 / OOS 0.46

**exit**: fixed_2R → IS 0.44 / OOS 0.44, partial → IS 0.32 / OOS 0.5, trail → IS 0.29 / OOS 0.48

**sl_mode**: atr → IS 0.34 / OOS 0.4, structure → IS 0.35 / OOS 0.49

## Overfitting checks

- **Deflated Sharpe** (864 trials): in-sample Sharpe 1.46 vs. 1.14 expected from the best of 864 skill-less configs, DSR 77.6% (> 95% is strong evidence). Without deflation PSR is 100.0%; out-of-sample PSR (config fixed in advance) is 99.5%.
- **Monte Carlo** (5000 trade-order bootstraps of 192 trades, 1% risk): CAGR 5th/50th/95th pct 6.18 / 11.32 / 16.63%, max drawdown -13.3 / -7.9 / -5.07% (actual trade-to-trade -6.46%), P(loss) 0.0%.
- **Walk-forward** 2021-01-01 ~ 2026-10-04: the same selection rule re-run every year on data up to the previous year end. Stitched: CAGR 6.8%, Sharpe 0.97, max DD -10.3%. Fixed production config, same span: CAGR 7.8%, Sharpe 1.11, max DD -7.4%. Buy & hold: CAGR 20.6%, Sharpe 0.61, max DD -76.6%.

| test year | trained on | selected config | return % | Sharpe | max DD % | production % | buy & hold % |
|---|---|---|---|---|---|---|---|
| 2021 | 2019–2020 | 4h / 5 / no_location / 50 / long_only / trail / atr | 1.9 | 0.3 | -5.4 | 2.7 | 59.8 |
| 2022 | 2019–2021 | 4h / 5 / no_location / 50 / long_only / trail / atr | 0.0 | 0.0 | 0.0 | 0.0 | -64.2 |
| 2023 | 2019–2022 | 4h / 5 / no_location / 50 / long_only / trail / atr | 5.5 | 0.68 | -10.1 | 9.6 | 155.6 |
| 2024 | 2019–2023 | 4h / 5 / sentiment / 40 / long_only / trail / atr | 20.1 | 2.02 | -6.4 | 20.1 | 121.3 |
| 2025 | 2019–2024 | 4h / 5 / no_location / 50 / long_only / trail / atr | 10.9 | 1.23 | -7.7 | 11.6 | -6.3 |
| 2026 (partial) | 2019–2025 | 4h / 5 / sentiment / 40 / long_only / trail / atr | 2.0 | 1.0 | -1.1 | 2.0 | -3.3 |

Production-config returns before 2024 are in-sample.

Strategy risk is 1% of equity per trade; returns scale roughly linearly with that choice, drawdowns too. Fees 0.05%/side + 0.02% slippage on market fills.