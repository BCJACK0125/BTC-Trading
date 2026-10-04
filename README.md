# BTC-Trading

BTCUSDT 4 小時的多因子 + SMC（Smart Money Concepts）共振訊號，加上一個 HTML 儀表板：現在該不該進場、止損止盈放哪裡，以及支持這套規則的回測證據。

![dashboard](docs/assets/preview.png)

## 怎麼用

儀表板：`docs/index.html`。直接用瀏覽器打開即可（資料在 `docs/data/latest.js`），或部署到 GitHub Pages（見下方）。

在本機更新資料：

```bash
pip install -r requirements.txt
python scripts/update.py      # 抓最新 K 線、計算訊號與回測，寫入 docs/data/
python scripts/research.py    # 重跑參數研究與過度擬合檢驗（約 2 分鐘），會改寫 config/strategy.json
python -m pytest -q tests     # 測試（含「不偷看未來資料」檢查）
python -m btc_signal.notify "測試"  # 選用：測試 Telegram / Discord 推播設定
```

## 策略

每根 4h K 線收盤後，把 7 個因子各縮放到 -1～+1，加權後得到 -100～+100 的分數：

| 因子 | 權重 | 內容 |
|---|---|---|
| 日線趨勢 | 25 | 日線收盤 vs EMA200、EMA50 vs EMA200 |
| SMC 市場結構 | 20 | 最近一次 BOS / CHoCH 的方向（swing 長度 5） |
| 主週期趨勢 | 15 | 4h EMA21 / EMA55 與價格位置 |
| 動能 | 10 | RSI 與 MACD 柱狀體，RSI 超過 75 時打折 |
| 溢價／折價 + OB/FVG | 10 | 價格在波段區間的位置、是否在多方 Order Block / FVG 內 |
| 趨勢強度 | 10 | ADX 與 ±DI |
| 恐懼貪婪（反向） | 10 | 貪婪扣分、恐懼加分 |

- **進場**：分數 ≥ 40，而且日線在 EMA200 之上；下一根 K 線開盤做多。只做多。
- **止損**：進場價 − 2×ATR。
- **出場**：1.5R 先平一半並把止損移到進場價，剩下一半用「最高價 − 3×ATR」的移動停損；60 根 K 線（約 10 天）後平倉。
- **冷卻**：出場後 3 根 K 線內不重新進場。剛出場時即使分數達標，儀表板會顯示「冷卻中」，與回測行為一致。

SMC 偵測（`btc_signal/smc.py`）用 TradingView MCP 對照過 LuxAlgo「Smart Money Concepts」：在 BINANCE:BTCUSDT 4h 上，目前有效的 Order Block 與最近的 BOS/CHoCH 價位完全一致。K 線資料也與 TradingView 的同一商品逐根核對過。

## 研究結果

864 組參數（週期 1h/4h、swing 長度、權重、門檻、多空、出場方式、止損方式）只用 2019–2023 挑選，2024 年之後當樣本外測試。每筆風險 1%，含 0.05% 手續費與 0.02% 滑價。

| | 樣本內 2019–2023 | 樣本外 2024–2026/10 | 買入持有（樣本外） |
|---|---|---|---|
| 年化報酬 | 10.4% | 12.0% | 28.8% |
| 最大回撤 | -7.5% | -6.9% | -53.0% |
| Sharpe | 1.46 | 1.50 | 0.77 |
| 勝率 / 獲利因子 | 51.7% / 1.97 | 52.6% / 1.94 | |

- 樣本內前 20 名在樣本外 100% 仍獲利，樣本外 Sharpe 中位數 1.27，代表不是單一參數碰巧。
- 4h 明顯優於 1h（中位 Sharpe 0.83 vs 0.08）；加入 1h 確認不會改善結果。
- 分數高不代表結果更好：40–50 分進場平均 +0.58R，70 分以上 +0.39R。
- 報酬和回撤大致隨每筆風險線性放大：風險 3% 時年化約 33%、最大回撤約 -22%。
- 弱點：只做多，牛市大幅落後買入持有（2020 年 +26% vs +299%）；優勢是避開熊市（2022 年 0% vs -65%）。

### 過度擬合檢驗

從 864 組裡挑最好的，本身就會讓回測看起來比實際好。`research.py` 另外做了三項檢驗：

| 檢驗 | 結果 | 解讀 |
|---|---|---|
| Deflated Sharpe（Bailey & López de Prado） | 77.6% | 864 組毫無真本事的參數，最好的一組樣本內 Sharpe 也預期有 1.14；採用的 1.46 勝過它的機率 77.6%，未達 95%。864 組高度相關，這是最嚴格的算法 |
| 樣本外 PSR | 99.5% | 參數在 2023 年底固定，之後真實 Sharpe > 0 的機率。這是比較乾淨的證據 |
| 蒙地卡羅（交易順序重抽 5,000 次） | 回撤中位 -7.9%，最差 5% 為 -13.3% | 實際逐筆回撤 -6.5%，屬於幸運的一邊；部位規模要用 -13% 左右來準備 |
| Walk-forward（2021 起每年只用過去資料重選參數） | 年化 6.8%、Sharpe 0.97、回撤 -10.3% | 挑參數的方法本身有效，但比固定參數的回測（Sharpe 1.11）差，這個落差才是對未來比較誠實的預期 |

Walk-forward 每年都選到「4h、swing 5、只做多、移動停損、ATR 止損」，只在權重與門檻（40/50）之間切換，參數結構穩定。

完整報告：[reports/research.md](reports/research.md)。

## 資料與自動更新

| 資料 | 來源 | 備援 |
|---|---|---|
| K 線 1h / 4h / 1d | Binance 公開行情 `data-api.binance.vision` | OKX |
| 恐懼貪婪指數 | alternative.me | 缺資料時該因子為 0 |
| 資金費率（僅顯示） | Binance 永續 | OKX |
| 頁面即時價格 | 瀏覽器直接查 Binance / OKX，每 15 秒 | 顯示最新收盤 |

**為什麼不是每天一次**：訊號建立在 4h K 線上，一天只更新一次會讓進場晚最多 20 小時。因此 GitHub Actions 在每根 4h K 線收盤後 7 分鐘執行（UTC 0/4/8/12/16/20 點），每次約 1 分鐘，每月約 180 分鐘，公開 repo 免費。
- `api.binance.com` 會擋美國 IP（GitHub runner 在美國），所以用 `data-api.binance.vision`。
- K 線用 `actions/cache` 增量快取。
- 參數研究不會自動重跑，避免參數在沒人看的情況下漂移。

**訊號紀錄**：每次更新會把當下發布的狀態寫進 `docs/data/history.json`，儀表板的「訊號紀錄」區塊顯示狀態變化與出場結果。這是上線後的真實前測紀錄，不是回測重算。GitHub runner 每次都是乾淨環境，所以腳本會先從已部署的網站（`SITE_URL`）和快取取回舊紀錄再合併。

**推播通知（選用）**：狀態改變時（例如「等待」→「進場做多」、持倉出場）發送通知。在 repo → Settings → Secrets and variables → Actions 新增：

| Secret | 說明 |
|---|---|
| `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID` | 用 @BotFather 建立 bot，chat id 可從 `getUpdates` 取得 |
| `DISCORD_WEBHOOK_URL` | 頻道設定 → 整合 → Webhook |

本機測試：設定環境變數後執行 `python -m btc_signal.notify "測試"`。沒設定就不發送，不影響更新。

**啟用 GitHub Pages**：repo → Settings → Pages → Source 選「GitHub Actions」，然後到 Actions 手動執行一次「Update signal & deploy dashboard」。私人 repo 使用 Pages 需要付費方案。

## 專案結構

```
btc_signal/   data.py 資料源、indicators.py 技術指標、smc.py 結構/OB/FVG、
              signals.py 評分與交易計畫、backtest.py 回測、
              stats.py PSR/DSR 與蒙地卡羅、history.py 訊號紀錄、notify.py 推播
scripts/      update.py 產生儀表板資料、research.py 參數研究
config/       strategy.json 目前採用的參數（由 research.py 產生）
docs/         儀表板（GitHub Pages 根目錄），data/ 為產出的資料
tests/        合成資料測試，含 no-lookahead 檢查
```

## 免責聲明

這是研究用途的程式，不是投資建議。回測已盡量保守（下一根開盤進場、同根 K 線同時碰到止損與止盈視為先止損），但過去績效不代表未來。
