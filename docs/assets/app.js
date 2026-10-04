(() => {
  "use strict";
  const D = window.BTC_DATA;
  const R = window.BTC_RESEARCH;
  const H = Array.isArray(window.BTC_HISTORY) ? window.BTC_HISTORY : [];
  const L = window.BTC_LTF;
  const F = window.BTC_FLOWS;
  const $ = (id) => document.getElementById(id);
  const root = document.documentElement;

  const fmt = (v, d = 0) => (v == null || Number.isNaN(v)) ? "—"
    : Number(v).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
  const sgn = (v, d = 1, unit = "%") => v == null ? "—" : (v > 0 ? "+" : "") + fmt(v, d) + unit;
  const cls = (v) => v > 0 ? "pos-t" : v < 0 ? "neg-t" : "";
  const cssVar = (n) => getComputedStyle(root).getPropertyValue(n).trim();
  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* storage blocked */ } },
  };
  const dateStr = (sec, withTime = true) => {
    const d = new Date(sec * 1000);
    const p = (n) => String(n).padStart(2, "0");
    const s = `${d.getFullYear()}/${p(d.getMonth() + 1)}/${p(d.getDate())}`;
    return withTime ? `${s} ${p(d.getHours())}:${p(d.getMinutes())}` : s;
  };
  const ago = (sec) => {
    const m = Math.round((Date.now() / 1000 - sec) / 60);
    if (m < 60) return `${m} 分鐘前`;
    const h = Math.floor(m / 60);
    return h < 48 ? `${h} 小時 ${m % 60} 分前` : `${Math.floor(h / 24)} 天前`;
  };

  // ---- theme ---------------------------------------------------------------
  const savedTheme = store.get("theme", null);
  if (savedTheme) root.dataset.theme = savedTheme;
  $("theme-btn").addEventListener("click", () => {
    const dark = root.dataset.theme ? root.dataset.theme === "dark"
      : matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark";
    store.set("theme", root.dataset.theme);
    drawCharts();
  });
  matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", () => { if (!root.dataset.theme) drawCharts(); });

  if (!D) {
    $("verdict").textContent = "沒有資料";
    $("summary").textContent = "找不到 data/latest.js。請先執行 python scripts/update.py 產生資料，再重新整理。";
    return;
  }

  const S = D.signal, P = D.plan, POS = D.position || null;
  const cfg = D.strategy;
  let live = null;

  // ---- header & verdict ----------------------------------------------------
  function renderFreshness() {
    const next = D.last_bar_close + 4 * 3600 + 10 * 60;
    const stale = Date.now() / 1000 > next + 35 * 60;
    const el = $("freshness");
    el.classList.toggle("stale", stale);
    el.textContent = `訊號依據 ${dateStr(D.last_bar_close)} 收盤的 4h K 線（${ago(D.last_bar_close)}）` +
      (stale ? "，資料已過期，請檢查 GitHub Actions" : `，下次更新約 ${dateStr(next)}`);
  }

  function renderVerdict() {
    $("context-line").textContent = `BTCUSDT 4 小時策略，只做多，門檻 ${cfg.threshold} 分`;
    const v = $("verdict");
    v.textContent = S.label;
    v.className = `verdict ${S.tone}`;
    $("summary").textContent = S.summary;

    const pos = (x) => `${(x + 100) / 2}%`;
    $("ruler-zone").style.left = pos(cfg.threshold);
    $("ruler-thr").style.left = pos(cfg.threshold);
    $("ruler-thr").textContent = `進場門檻 ${cfg.threshold}`;
    $("ruler-value").textContent = fmt(S.score, 1);
    $("ruler").setAttribute("aria-label", `共振分數 ${fmt(S.score, 1)}，範圍 -100 到 100，進場門檻 ${cfg.threshold}`);
    requestAnimationFrame(() => { $("ruler-needle").style.left = pos(Math.max(-100, Math.min(100, S.score))); });
    $("ruler-needle").style.left = "50%";

    const htf = D.timeframes["1d"];
    $("ruler-note").textContent = S.action === "STAND_ASIDE"
      ? "日線趨勢轉弱時，不論分數多高都不開新多單。"
      : `日線收盤 ${fmt(htf.close)}，EMA200 在 ${fmt(htf.ema200)}；日線跌破 EMA200 時策略停止做多。`;
  }

  // ---- ticket: price ladder + rules + calculator ---------------------------
  function levels() {
    if (POS) {
      const lv = [
        { k: "entry", label: "持倉進場價", px: POS.entry },
        { k: "stop", label: POS.tp1_hit ? "移動停損" : "止損", px: POS.stop },
      ];
      if (!POS.tp1_hit) lv.push({ k: "tp", label: "TP1 平一半", px: POS.tp1 });
      lv.push({ k: "tp", label: "3R 參考", px: POS.tp2 });
      return { entry: POS.entry, R: (POS.tp1 - POS.entry) / cfg.tp1_r, lv };
    }
    const lv = [
      { k: "entry", label: "進場（收盤價）", px: P.entry },
      { k: "stop", label: "止損", px: P.stop },
      { k: "tp", label: `TP1 ${cfg.tp1_r}R 平一半`, px: P.tp1 },
      { k: "tp", label: `${cfg.tp2_r}R 參考`, px: P.tp2 },
    ];
    if (P.pullback_entry) lv.push({ k: "pull", label: "回測支撐區", px: P.pullback_entry });
    return { entry: P.entry, R: P.entry - P.stop, lv };
  }

  function renderTicket() {
    const { entry, R, lv } = levels();
    const now = live ?? D.price;
    const ladder = $("ladder");
    const H = ladder.clientHeight || 300;
    const prices = [...lv.map((l) => l.px), now];
    const lo = Math.min(...prices), hi = Math.max(...prices);
    const pad = (hi - lo) * 0.08 || 1;
    const y = (p) => ((hi + pad - p) / (hi - lo + 2 * pad)) * H;

    const rungs = [...lv, { k: "now", label: live ? "即時價格" : "最新收盤", px: now }]
      .map((l) => ({ ...l, y: y(l.px) })).sort((a, b) => a.y - b.y);
    for (let i = 1; i < rungs.length; i++) {     // keep labels from colliding
      if (rungs[i].y - rungs[i - 1].y < 26) rungs[i].y = rungs[i - 1].y + 26;
    }
    const overflow = rungs[rungs.length - 1].y - (H - 10);
    if (overflow > 0) rungs.forEach((r) => { r.y -= overflow; });

    const stop = lv.find((l) => l.k === "stop").px;
    const top = Math.max(...lv.filter((l) => l.k === "tp").map((l) => l.px));
    let html = `<div class="band risk" style="top:${y(entry)}px;height:${y(stop) - y(entry)}px"></div>` +
      `<div class="band reward" style="top:${y(top)}px;height:${y(entry) - y(top)}px"></div>`;
    for (const r of rungs) {
      const d = r.k === "now" ? "" : `${sgn((r.px / now - 1) * 100, 2)}` + (R > 0 ? `　${sgn((r.px - entry) / R, 1, "R")}` : "");
      html += `<div class="rung ${r.k}" style="top:${r.y}px"><span class="lbl">${r.label}</span>` +
        `<span class="px">${fmt(r.px, 0)}</span><span class="dist">${r.k === "now" ? "" : d}</span></div>`;
    }
    ladder.innerHTML = html;
    ladder.setAttribute("aria-label", rungs.map((r) => `${r.label} ${fmt(r.px)}`).join("，"));

    // sub line + alerts
    let sub;
    if (POS) {
      sub = `系統 ${dateStr(POS.entry_time - D.chart.bar_seconds)} 進場，已持有 ${POS.bars} 根 K 線，目前 ${sgn(POS.r_now, 2, "R")}。`;
    } else if (S.action === "ENTER_LONG") {
      sub = "訊號成立：下一根 4h K 線開盤進場" + (P.pullback_entry ? `，或掛單在回測支撐 ${fmt(P.pullback_entry)} 附近。` : "。");
    } else if (S.action === "COOLDOWN") {
      sub = "分數已達門檻，但仍在出場後的冷卻期。以下是冷卻結束後若條件仍成立的參考價位。";
    } else {
      sub = "目前沒有訊號。以下是「如果此刻條件成立」的參考價位，不是進場建議。";
    }
    const alerts = [];
    if (live != null) {
      if (live <= stop) alerts.push(`即時價格 ${fmt(live)} 已低於止損 ${fmt(stop)}。`);
      const tp1 = lv.find((l) => l.label.startsWith("TP1"));
      if (tp1 && live >= tp1.px) alerts.push(`即時價格已到 TP1 ${fmt(tp1.px)}，可先平一半並把止損移到進場價。`);
    }
    $("ticket-sub").innerHTML = sub + alerts.map((a) => `<div class="alert">${a}</div>`).join("");

    const rules = [P.tp1_note && `TP1（${cfg.tp1_r}R）${P.tp1_note}`, P.trail_note, P.time_stop_note,
      `止損距離 ${fmt(P.risk_pct, 2)}%（${cfg.sl_atr} 倍 ATR，ATR 約 ${fmt(P.atr)}）`];
    if (P.liquidity_target) rules.push(`上方最近的空方區在 ${fmt(P.liquidity_target)}，可能形成壓力。`);
    $("rules").innerHTML = rules.filter(Boolean).map((r) => `<li>${r}</li>`).join("");
    renderCalc();
  }

  function renderCalc() {
    const acct = parseFloat($("acct").value) || 0;
    const rp = parseFloat($("riskpct").value) || 0;
    store.set("calc", { acct, rp });
    const entry = POS ? POS.entry : P.entry;
    const stop = POS ? POS.stop : P.stop;
    const dist = entry - stop;
    const riskUsd = acct * rp / 100;
    const size = dist > 0 ? riskUsd / dist : 0;
    const notional = size * entry;
    const rows = [
      ["可承受虧損", `${fmt(riskUsd, 2)} USDT`],
      ["倉位大小", `${fmt(size, 4)} BTC`],
      ["名目價值", `${fmt(notional, 0)} USDT`],
      ["需要槓桿", acct ? `${fmt(notional / acct, 2)} 倍` : "—"],
      [`TP1 平一半獲利`, `${fmt(size / 2 * dist * cfg.tp1_r, 2)} USDT`],
    ];
    $("calc-out").innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");
  }

  const calcSaved = store.get("calc", { acct: 10000, rp: 1 });
  $("acct").value = calcSaved.acct;
  $("riskpct").value = calcSaved.rp;
  $("acct").addEventListener("input", renderCalc);
  $("riskpct").addEventListener("input", renderCalc);

  // ---- factors / timeframes / context ----------------------------------------
  function renderFactors() {
    const total = S.factors.reduce((a, f) => a + f.weight, 0);
    const maxC = Math.max(...S.factors.map((f) => f.weight / total * 100));
    $("factors").innerHTML = S.factors.map((f) => {
      const w = Math.abs(f.contribution) / maxC * 50;
      return `<div class="factor"><div class="name">${f.label}<small>權重 ${f.weight}</small></div>
        <div class="bar" role="img" aria-label="${f.label} 貢獻 ${sgn(f.contribution, 1, "")} 分">
          <div class="fill ${f.contribution >= 0 ? "pos" : "neg"}" style="width:${w}%"></div></div>
        <div class="val ${cls(f.contribution)}">${sgn(f.contribution, 1, "")}</div>
        <div class="why">${f.detail}</div></div>`;
    }).join("") + `<div class="factor-total"><span>合計分數</span><span>${fmt(S.score, 1)}</span></div>`;
  }

  function renderTF() {
    const name = { "1h": "1 小時", "4h": "4 小時（下單）", "1d": "日線" };
    const dir = (v, up, dn) => v > 0 ? `<span class="pos-t">${up}</span>` : v < 0 ? `<span class="neg-t">${dn}</span>` : "—";
    $("tf-table").innerHTML = `<thead><tr><th>週期</th><th>分數</th><th>趨勢</th><th>結構</th><th>RSI</th><th>ADX</th><th>波段位置</th></tr></thead><tbody>` +
      ["1h", "4h", "1d"].map((k) => {
        const t = D.timeframes[k];
        return `<tr><td>${name[k]}</td><td class="${cls(t.score)}">${fmt(t.score, 0)}</td><td>${dir(t.trend, "向上", "向下")}</td>
          <td>${dir(t.structure, "多頭", "空頭")}</td><td>${fmt(t.rsi, 0)}</td><td>${fmt(t.adx, 0)}</td>
          <td>${t.range_pos == null ? "—" : fmt(t.range_pos * 100, 0) + "%"}</td></tr>`;
      }).join("") + "</tbody>";

    const fg = D.context.fear_greed;
    const fgLabel = fg == null ? "" : fg >= 75 ? "極度貪婪" : fg >= 55 ? "貪婪" : fg > 45 ? "中性" : fg > 25 ? "恐懼" : "極度恐懼";
    const fr = D.context.funding;
    const rows = [
      ["恐懼貪婪指數", fg == null ? "無資料" : `${fg}（${fgLabel}）。策略把它當反向指標，貪婪時扣分。`],
      ["資金費率", fr ? `${sgn(fr.rate * 100, 4)} / 8 小時（${fr.source}）。${fr.rate > 0.0003 ? "多方擁擠，留意回檔。" : fr.rate < 0 ? "空方付費，偏空情緒。" : "正常範圍。"}` : "無資料"],
    ];
    $("ctx").innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");
  }

  // ---- backtest ---------------------------------------------------------------
  function renderBacktest() {
    const B = D.backtest, oos = B.oos, is = B.is;
    const bh = R?.baselines || {};
    $("bt-lede").textContent = `${B.full.start} 到 ${B.full.end}，每筆交易風險 1% 資金（最高 0.82 倍部位，可用現貨執行，不含資金費率），含手續費與滑價。槓桿與資金費率的影響見下方「公平比較」。` +
      `參數只用 ${is.start}～${is.end} 挑選，${oos.start} 之後是沒看過的樣本外資料。`;
    const kpi = (v, k, c) => `<div class="kpi"><div class="v">${v}</div><div class="k">${k}</div><div class="c">${c}</div></div>`;
    $("kpis").innerHTML =
      kpi(fmt(oos.sharpe, 2), "樣本外 Sharpe", `樣本內 ${fmt(is.sharpe, 2)}，買入持有 ${fmt(bh.buy_hold_oos?.sharpe, 2)}`) +
      kpi(fmt(oos.win_rate_pct, 0) + "%", "樣本外勝率", `${oos.trades} 筆交易，平均 ${sgn(oos.avg_r, 2, "R")}`) +
      kpi(fmt(oos.profit_factor, 2), "樣本外獲利因子", `總獲利 ÷ 總虧損，樣本內 ${fmt(is.profit_factor, 2)}`) +
      kpi(fmt(oos.max_dd_pct, 1) + "%", "樣本外最大回撤", `買入持有 ${fmt(bh.buy_hold_oos?.max_dd_pct, 1)}%`);

    const rows = [
      ["總報酬", "total_return_pct", (v) => sgn(v, 1)], ["年化報酬", "cagr_pct", (v) => sgn(v, 1)],
      ["最大回撤", "max_dd_pct", (v) => fmt(v, 1) + "%"], ["Sharpe", "sharpe", (v) => fmt(v, 2)],
      ["交易次數", "trades", (v) => fmt(v)], ["勝率", "win_rate_pct", (v) => fmt(v, 1) + "%"],
      ["獲利因子", "profit_factor", (v) => fmt(v, 2)], ["平均每筆", "avg_r", (v) => sgn(v, 2, "R")],
      ["在場時間", "exposure_pct", (v) => fmt(v, 0) + "%"],
    ];
    const bhRow = (label, key, f) => `<tr><td>${label}</td><td>${f(bh.buy_hold_is?.[key])}</td><td>${f(bh.buy_hold_oos?.[key])}</td><td></td></tr>`;
    $("stats-table").innerHTML = `<thead><tr><th></th><th>樣本內</th><th>樣本外</th><th>全期間</th></tr></thead><tbody>` +
      rows.map(([l, k, f]) => `<tr><td>${l}</td><td>${f(is[k])}</td><td>${f(oos[k])}</td><td>${f(B.full[k])}</td></tr>`).join("") +
      `<tr><td colspan="4" style="padding-top:14px;color:var(--muted)">對照：買入持有</td></tr>` +
      bhRow("年化報酬", "cagr_pct", (v) => sgn(v, 1)) + bhRow("最大回撤", "max_dd_pct", (v) => fmt(v, 1) + "%") +
      bhRow("Sharpe", "sharpe", (v) => fmt(v, 2)) + "</tbody>";

    $("bucket-table").innerHTML = `<thead><tr><th>進場分數</th><th>筆數</th><th>勝率</th><th>平均</th></tr></thead><tbody>` +
      B.score_buckets.map((b) => `<tr><td>${b.range}</td><td>${b.trades}</td><td>${fmt(b.win_rate, 0)}%</td><td class="${cls(b.avg_r)}">${sgn(b.avg_r, 2, "R")}</td></tr>`).join("") + "</tbody>";

    const reason = { stop: "止損", breakeven: "保本出場", trail: "移動停損", target: "止盈", time: "時間到" };
    $("trades-table").innerHTML = `<thead><tr><th>進場</th><th>出場</th><th>進場價</th><th>出場價</th><th>結果</th><th>資金變化</th><th>出場原因</th><th>持有</th></tr></thead><tbody>` +
      B.recent_trades.map((t) => `<tr><td>${dateStr(t.entry_time - D.chart.bar_seconds)}</td><td>${dateStr(t.exit_time)}</td>
        <td>${fmt(t.entry)}</td><td>${fmt(t.exit)}</td><td class="${cls(t.r)}">${sgn(t.r, 2, "R")}</td>
        <td class="${cls(t.ret_pct)}">${sgn(t.ret_pct, 2)}</td><td>${reason[t.reason] || t.reason}</td><td>${Math.round(t.bars * 4 / 24 * 10) / 10} 天</td></tr>`).join("") + "</tbody>";

    renderYears(B.yearly);
  }

  function renderYears(yearly) {
    $("yr-legend").innerHTML = `<span><i style="background:var(--s1)"></i>策略</span><span><i style="background:var(--s2)"></i>買入持有</span>`;
    const vals = yearly.flatMap((y) => [y.strategy, y.buy_hold]);
    const lo = Math.min(0, ...vals), hi = Math.max(0, ...vals);
    const span = (hi - lo) * 1.18 || 1;  // headroom on the right for the value label
    const zero = (-lo / span) * 100;
    const bar = (v, c) => {
      const w = Math.abs(v) / span * 100;
      const left = v >= 0 ? zero : zero - w;
      // negative values are labelled just right of the zero line so they never run into the year column
      const tx = v >= 0 ? `left:calc(${left + w}% + 4px)` : `left:calc(${zero}% + 4px)`;
      return `<div class="yr-row" style="--zero:${zero}%"><div class="b ${c} ${v >= 0 ? "r" : "l"}" style="left:${left}%;width:${Math.max(w, 0.4)}%"></div>
        <span class="t" style="${tx}">${sgn(v, 0)}</span></div>`;
    };
    $("years").innerHTML = yearly.map((y) => `<div class="yr"><span>${y.year}</span><div class="yr-bars" role="img"
      aria-label="${y.year} 年 策略 ${sgn(y.strategy, 1)}，買入持有 ${sgn(y.buy_hold, 1)}">${bar(y.strategy, "s")}${bar(y.buy_hold, "h")}</div></div>`).join("");
  }

  // ---- like-for-like comparison: leverage, equal drawdown, DCA ----------------------
  const PERIODS = D.compare?.periods || [];
  const cmpState = store.get("cmp", { period: "oos", risk: "1" });
  if (!PERIODS.some((p) => p.key === cmpState.period)) cmpState.period = PERIODS[0]?.key;
  const period = () => PERIODS.find((p) => p.key === cmpState.period);

  function seg(el, options, current, onPick) {
    el.innerHTML = options.map(([v, label]) =>
      `<button type="button" data-v="${v}" aria-pressed="${v === current}">${label}</button>`).join("");
    el.onclick = (ev) => {
      const b = ev.target.closest("button");
      if (b) onPick(b.dataset.v);
    };
  }

  function renderCompare() {
    const per = period();
    if (!per) return;
    seg($("period-seg"), PERIODS.map((p) => [p.key, p.label]), cmpState.period, (v) => {
      cmpState.period = v; store.set("cmp", cmpState); renderCompare(); if (window.LightweightCharts) { equityChart?.remove(); drawEquity(); }
    });
    const risks = Object.keys(per.chart).filter((k) => k.startsWith("strategy_")).map((k) => k.slice(9));
    if (!risks.includes(cmpState.risk)) cmpState.risk = risks[0];
    seg($("risk-seg"), risks.map((r) => [r, `${r}%`]), cmpState.risk, (v) => {
      cmpState.risk = v; store.set("cmp", cmpState); renderCompare(); if (window.LightweightCharts) { equityChart?.remove(); drawEquity(); }
    });

    const hold1 = per.hold.find((h) => h.lev === 1);
    const r1 = per.risk.find((r) => r.risk_pct === 1);
    $("cmp-lede").textContent = `${per.start} 到 ${per.end}。策略每筆只冒 1% 風險、平均部位只有 ${fmt(r1?.avg_lev, 2)} 倍，` +
      `大部分時間空手；買入持有則是 100% 一直在場。直接比報酬會低估策略，只看 Sharpe 又會高估它。` +
      `公平的比法是在同樣的回撤下比報酬，或把兩邊都放大到同樣的槓桿。` +
      (hold1 ? `這段期間買入持有年化 ${sgn(hold1.cagr_pct, 1)}、最大回撤 ${fmt(hold1.max_dd_pct, 1)}%。` : "");

    // equal drawdown: metric rows x (hold, matched strategy) column pairs
    const M = per.matched;
    const head = M.map((m) => `<th>買入持有 ${m.hold_lev}×</th><th class="hl">策略（同回撤）</th>`).join("");
    const row = (label, fh, fs) => `<tr><td>${label}</td>` + M.map((m) => `<td>${fh(m.hold)}</td><td class="hl">${fs(m.strategy)}</td>`).join("") + "</tr>";
    $("match-table").innerHTML = `<thead><tr><th></th>${head}</tr></thead><tbody>` +
      row("最大回撤", (h) => fmt(h.max_dd_pct, 1) + "%", (s) => fmt(s.max_dd_pct, 1) + "%") +
      row("年化報酬", (h) => `<span class="${cls(h.cagr_pct)}">${sgn(h.cagr_pct, 1)}</span>`, (s) => `<b class="${cls(s.cagr_pct)}">${sgn(s.cagr_pct, 1)}</b>`) +
      row("Sharpe", (h) => fmt(h.sharpe, 2), (s) => fmt(s.sharpe, 2)) +
      row("每筆風險", () => "—", (s) => fmt(s.risk_pct, 1) + "%") +
      row("平均／最高槓桿", (h) => `${h.lev}×`, (s) => `${fmt(s.avg_lev, 1)}×／${fmt(s.max_lev, 1)}×`) +
      row("蒙地卡羅最差 5% 回撤", () => "—", (s) => s.mc_dd_p5 == null ? "—" : fmt(s.mc_dd_p5, 1) + "%") + "</tbody>";
    $("match-note").textContent = "策略的每筆風險是事後挑來剛好打平回撤的，實際上無法事先知道會是多少；" +
      "蒙地卡羅那一列是同樣設定換個交易順序的最差 5% 情況，高槓桿時明顯更深。槓桿越高，跳空滑價、插針與交易所風險也越大，回測都沒有算進去。";

    $("risk-table").innerHTML = `<thead><tr><th>每筆風險</th><th>平均／最高槓桿</th><th>年化（未扣資金費率）</th><th class="hl">年化（扣資金費率）</th><th>最大回撤</th><th>蒙地卡羅最差 5%</th><th>Sharpe</th></tr></thead><tbody>` +
      per.risk.map((r) => `<tr><td>${fmt(r.risk_pct, 0)}%</td><td>${fmt(r.avg_lev, 2)}×／${fmt(r.max_lev, 2)}×</td>
        <td>${sgn(r.cagr_no_funding_pct, 1)}</td><td class="hl ${cls(r.cagr_pct)}">${sgn(r.cagr_pct, 1)}</td><td>${fmt(r.max_dd_pct, 1)}%</td>
        <td>${r.mc_dd_p5 == null ? "—" : fmt(r.mc_dd_p5, 1) + "%"}</td><td>${fmt(r.sharpe, 2)}</td></tr>`).join("") + "</tbody>";
    const fa = D.compare.funding_avg_annual_pct;
    $("risk-note").textContent = `槓桿由「每筆風險 ÷ 止損距離」決定，不是另外設定的。以永續合約執行時，持倉每 8 小時支付實際歷史資金費率` +
      (fa != null ? `（2019 年以來平均每年約 ${fmt(fa, 1)}% 的名目部位）` : "") +
      `。風險 1% 時最高槓桿不到 1 倍，可以直接用現貨，不必付資金費率，看「未扣資金費率」那欄即可。報酬與回撤大致隨風險等比例放大，但蒙地卡羅的最差情況放大得更快。`;

    const D_ = per.dca;
    $("hold-table").innerHTML = `<thead><tr><th></th><th>年化</th><th>總報酬</th><th>最大回撤</th><th>Sharpe</th></tr></thead><tbody>` +
      per.hold.map((h) => `<tr><td>買入持有 ${h.lev}×${h.liquidated ? "（爆倉）" : ""}</td><td class="${cls(h.cagr_pct)}">${sgn(h.cagr_pct, 1)}</td>
        <td class="${cls(h.total_return_pct)}">${sgn(h.total_return_pct, 0)}</td><td>${fmt(h.max_dd_pct, 1)}%</td><td>${fmt(h.sharpe, 2)}</td></tr>`).join("") +
      `<tr><td>每月定期定額</td><td class="${cls(D_.irr_pct)}">IRR ${sgn(D_.irr_pct, 1)}</td><td class="${cls(D_.return_on_invested_pct)}">${sgn(D_.return_on_invested_pct, 1)}</td>` +
      `<td>${fmt(D_.value_dd_pct, 1)}%</td><td>—</td></tr></tbody>`;
    $("hold-note").textContent = "槓桿版本每天調整回固定倍數，超過 1 倍以永續合約持有並支付資金費率；單日最低價足以歸零時視為爆倉。" +
      `波動越大，加槓桿越吃虧：同期間 2 倍的報酬常常不到 1 倍的兩倍，回撤卻深得多。` +
      `定期定額是從起點每月第一天投入同樣金額（共 ${D_.months} 次），報酬以資金加權 IRR 計算、總報酬是相對於累計投入，` +
      `和一次投入的年化不能直接相比；期間投入資金最深曾虧 ${fmt(D_.worst_vs_invested_pct, 1)}%。`;
  }

  // ---- forward signal log ---------------------------------------------------------
  const TONE = { ENTER_LONG: "go", IN_POSITION: "hold", WATCH: "watch", COOLDOWN: "watch" };
  const REASON = { stop: "止損", breakeven: "保本出場", trail: "移動停損", target: "止盈", time: "時間到" };

  function renderHistory() {
    if (!H.length) {
      $("hist-lede").textContent = "尚無紀錄。每次自動更新都會把當時發布的訊號記在這裡，累積成真實的前測紀錄。";
      return;
    }
    const bar = D.chart.bar_seconds;
    const expected = Math.round((H[H.length - 1].bar - H[0].bar) / bar) + 1;
    const missing = Math.max(0, expected - H.length);
    const entries = H.filter((e) => e.action === "ENTER_LONG").length;
    $("hist-stats").textContent = `${H.length} 次更新・${entries} 次進場訊號` + (missing ? `・缺漏 ${missing} 根` : "");
    $("hist-lede").textContent = `自 ${dateStr(H[0].bar)} 起，每根 4h K 線收盤後頁面實際發布的狀態（不是事後用回測重算）。` +
      "下表只列出狀態改變或有交易出場的時點，最新在上。回測是規則的歷史模擬，這裡才是上線後的真實紀錄。";
    const shown = H.filter((e, i) => i === 0 || e.action !== H[i - 1].action || e.closed).slice(-30).reverse();
    $("hist-table").innerHTML = `<thead><tr><th>K 線收盤</th><th>狀態</th><th>分數</th><th>價格</th><th>止損</th><th>TP1</th><th>交易出場</th></tr></thead><tbody>` +
      shown.map((e) => `<tr><td>${dateStr(e.bar)}</td><td><span class="tag ${TONE[e.action] || ""}">${e.label}</span></td>
        <td>${fmt(e.score, 1)}</td><td>${fmt(e.price)}</td><td>${fmt(e.stop)}</td><td>${fmt(e.tp1)}</td>
        <td>${e.closed ? `<span class="${cls(e.closed.r)}">${sgn(e.closed.r, 2, "R")}</span>（${REASON[e.closed.reason] || e.closed.reason}）` : ""}</td></tr>`).join("") +
      "</tbody>";
  }

  const W_NAME = { base: "基本", no_location: "不含位置", sentiment: "含情緒" };
  const X_NAME = { fixed_2R: "固定 2R", partial: "1.5R + 3R", trail: "1.5R + 移動停損" };
  const paramText = (t) => [t.tf, `swing ${t.swing_len}`, W_NAME[t.weights] || t.weights, `門檻 ${t.threshold}`,
    t.sides === "long_only" ? "只做多" : "多空", X_NAME[t.exit] || t.exit, t.sl_mode === "atr" ? "ATR 止損" : "結構止損"].join("・");

  function renderRobust() {
    const rb = R?.robustness;
    if (!rb?.deflated) { $("robust").hidden = true; return; }
    const ds = rb.deflated, mc = rb.monte_carlo || {}, wf = rb.walk_forward || {};
    const kpi = (v, k, c, tone = "") => `<div class="kpi"><div class="v ${tone}">${v}</div><div class="k">${k}</div><div class="c">${c}</div></div>`;
    $("rob-kpis").innerHTML =
      kpi(fmt(ds.psr_oos, 1) + "%", "樣本外 PSR", "參數事先固定，樣本外真實 Sharpe > 0 的機率", ds.psr_oos >= 95 ? "pos-t" : "warn-t") +
      kpi(fmt(ds.dsr, 1) + "%", "Deflated Sharpe", `扣掉挑選 ${ds.n_trials} 組的運氣後，樣本內 Sharpe 仍顯著的機率`, ds.dsr >= 95 ? "pos-t" : "warn-t") +
      (mc.max_dd_p5 != null ? kpi(fmt(mc.max_dd_p5, 1) + "%", "蒙地卡羅最差 5% 回撤", `中位 ${fmt(mc.max_dd_p50, 1)}%，實際 ${fmt(mc.actual_trade_dd_pct, 1)}%（逐筆）`) : "") +
      (wf.stitched ? kpi(fmt(wf.stitched.sharpe, 2), "Walk-forward Sharpe", `年化 ${sgn(wf.stitched.cagr_pct, 1)}，最大回撤 ${fmt(wf.stitched.max_dd_pct, 1)}%`) : "");
    $("rob-note").textContent =
      `挑過 ${ds.n_trials} 組參數，最好的那組就算毫無真本事，樣本內 Sharpe 也預期能到 ${fmt(ds.benchmark_sharpe, 2)}；` +
      `採用的設定是 ${fmt(ds.is_sharpe, 2)}，Deflated Sharpe ${fmt(ds.dsr, 1)}%` +
      (ds.dsr >= 95 ? "，超過 95% 的顯著門檻。" : "，未達 95% 的顯著門檻，單看樣本內不足以排除運氣。") +
      `不過這 ${ds.n_trials} 組彼此高度相關（多半只差門檻或權重），當成獨立試驗是最嚴格的算法。` +
      `比較乾淨的證據是樣本外：參數在 2023 年底就固定，之後的 PSR 為 ${fmt(ds.psr_oos, 1)}%。` +
      (mc.n_sims ? `蒙地卡羅把 ${mc.n_trades} 筆交易的順序重抽 ${fmt(mc.n_sims)} 次，年化報酬 5%～95% 區間為 ${sgn(mc.cagr_p5, 1)} 到 ${sgn(mc.cagr_p95, 1)}，虧損機率 ${fmt(mc.prob_loss_pct, 1)}%。` : "");

    if (!wf.years) { $("wf-lede").textContent = ""; return; }
    $("wf-lede").textContent = `每年年初只用之前的資料、用同一套規則重新挑參數，然後實際交易一年，再把各年接起來（${wf.period}）。` +
      `這測的是「挑參數的方法」本身，而不只是某一組參數：接起來的年化 ${sgn(wf.stitched.cagr_pct, 1)}、Sharpe ${fmt(wf.stitched.sharpe, 2)}；` +
      `同期間固定用目前參數為 ${sgn(wf.prod.cagr_pct, 1)}／${fmt(wf.prod.sharpe, 2)}，買入持有 ${sgn(wf.buy_hold.cagr_pct, 1)}／${fmt(wf.buy_hold.sharpe, 2)}（最大回撤 ${fmt(wf.buy_hold.max_dd_pct, 0)}%）。`;
    $("wf-table").innerHTML = `<caption class="note" style="caption-side:bottom;text-align:left;padding-top:8px">* 目前參數是用 2019–2023 挑出的，2024 年以前屬於樣本內，僅供對照。</caption><thead><tr><th>測試年</th><th>訓練資料</th><th>當年選出的參數</th><th>報酬</th><th>Sharpe</th><th>最大回撤</th><th>目前參數</th><th>買入持有</th></tr></thead><tbody>` +
      wf.years.map((y) => `<tr><td>${y.year}${y.partial ? "（至今）" : ""}</td><td>${y.train}</td><td>${paramText(y.params)}</td>
        <td class="${cls(y.return_pct)}">${sgn(y.return_pct, 1)}</td><td>${fmt(y.sharpe, 2)}</td><td>${fmt(y.max_dd_pct, 1)}%</td>
        <td class="${cls(y.prod_return_pct)}">${sgn(y.prod_return_pct, 1)}${y.year < 2024 ? "*" : ""}</td><td class="${cls(y.buy_hold_pct)}">${sgn(y.buy_hold_pct, 1)}</td></tr>`).join("") +
      "</tbody>";
  }

  function renderLtf() {
    if (!L?.best_by_mode) { $("ltf").hidden = true; return; }
    const name = { market: "直接進場（目前）", limit: "掛單等回檔", structure: "等 1h 結構突破" };
    const label = (t) => t.label.replace("market", "下一根開盤").replace("limit zone", "掛在 OB/FVG").replace("limit ", "掛單 ")
      .replace("structure ", "1h ").replace("1h-stop", "1h 止損").replace("4h-stop", "4h 止損");
    const rows = ["market", "limit", "structure"].filter((m) => L.best_by_mode[m]).map((m) => [m, L.best_by_mode[m], L.median_by_mode[m]]);
    const base = L.best_by_mode.market;
    $("ltf-lede").textContent = `4h 訊號不變，改在 1h K 線上用不同方式進場，共測 ${L.n_variants} 種變化（掛單深度、等待時間、1h swing 長度、止損放法）。` +
      `同樣只用 ${L.is_period} 挑選。結果沒有任何一種在樣本內或樣本外贏過直接進場（Sharpe ${fmt(base.is_sharpe, 2)} / ${fmt(base.oos_sharpe, 2)}）。下表是每一類裡樣本內最好的一種。`;
    $("ltf-table").innerHTML = `<thead><tr><th>進場方式</th><th>最佳設定</th><th>樣本內 Sharpe</th><th>樣本外 Sharpe</th><th>同類中位數（內／外）</th>
      <th>樣本外年化</th><th>樣本外回撤</th><th>勝率</th><th>平均每筆</th><th>成交率</th><th>止損（ATR）</th></tr></thead><tbody>` +
      rows.map(([m, b, med], i) => `<tr${i === 0 ? ' style="font-weight:700"' : ""}><td>${name[m]}</td><td>${label(b)}</td>
        <td>${fmt(b.is_sharpe, 2)}</td><td class="${cls(b.oos_sharpe)}">${fmt(b.oos_sharpe, 2)}</td><td>${fmt(med.is_sharpe, 2)}／${fmt(med.oos_sharpe, 2)}</td>
        <td>${sgn(b.oos_cagr_pct, 1)}</td><td>${fmt(b.oos_max_dd_pct, 1)}%</td><td>${fmt(b.is_win_rate_pct, 0)}%</td>
        <td>${sgn(b.is_avg_r, 2, "R")}</td><td>${fmt(b.is_fill_rate_pct, 0)}%</td><td>${fmt(b.is_avg_stop_atr, 2)}</td></tr>`).join("") + "</tbody>";
    const a = (L.adverse_selection || []).find((x) => x.pullback_atr === 0.5);
    $("ltf-note").textContent = (a ? `原因是逆向選擇：直接進場的交易中，8 小時內曾回檔 0.5 倍 ATR、掛單會成交的只有 ${fmt(a.would_fill_pct, 0)}%，` +
      `這些交易平均 ${sgn(a.avg_r_filled, 2, "R")}；從不回頭的那些平均 ${sgn(a.avg_r_missed, 2, "R")}，貢獻了總獲利的 ${fmt(a.share_r_missed_pct, 0)}%。` +
      `掛單只會買到比較弱的行情。` : "") +
      (L.median_by_mode.limit?.is_avg_r != null ? `多數變化的每筆表現也變差：同類中位數勝率 ${fmt(L.median_by_mode.limit.is_win_rate_pct, 0)}%／${fmt(L.median_by_mode.structure.is_win_rate_pct, 0)}%、` +
        `平均 ${sgn(L.median_by_mode.limit.is_avg_r, 2, "R")}／${sgn(L.median_by_mode.structure.is_avg_r, 2, "R")}（掛單／結構），直接進場是 ${fmt(base.is_win_rate_pct, 0)}%、${sgn(base.is_avg_r, 2, "R")}。` +
        `各類最好的設定在樣本內只是接近直接進場（掛單 ${fmt(L.best_by_mode.limit.is_sharpe, 2)}、結構 ${fmt(L.best_by_mode.structure.is_sharpe, 2)}，對比 ${fmt(base.is_sharpe, 2)}），` +
        `到樣本外差距反而拉大（${fmt(L.best_by_mode.limit.oos_sharpe, 2)}、${fmt(L.best_by_mode.structure.oos_sharpe, 2)}，對比 ${fmt(base.oos_sharpe, 2)}），多了複雜度卻沒有好處。` : "") +
      "1h 止損比較近，同樣風險下部位較大，但也更容易被雜訊掃掉。這是趨勢跟隨策略的特性：獲利來自少數不回頭的大波段，追求更好的進場價反而會錯過它們。" +
      "勝率、平均每筆、成交率、止損為樣本內數字。";
  }

  function renderFlows() {
    if (!F?.best_by_family) { $("flows").hidden = true; return; }
    const fam = { prem_filter: "Coinbase 溢價過濾", prem_factor: "Coinbase 溢價因子", fund_filter: "資金費率擁擠過濾", fund_factor: "資金費率反向因子" };
    const b = F.baseline;
    $("flows-lede").textContent = `「法人動向」常見的指標裡，ETF 流量只有 2024 年以後的資料、COT 每週一次且大多是套利部位，無法做樣本內／樣本外驗證，所以只測能回溯到 2019 年的兩種：` +
      `Coinbase 溢價（美國現貨買盤）與永續資金費率（槓桿多單擁擠程度），各當成進場過濾或第 8 個因子，共 ${F.n_variants} 種設定。` +
      `結果 ${F.beat_is} 種在樣本內、${F.beat_oos} 種在樣本外勝過目前策略，兩者都勝過的有 ${F.beat_both} 種。`;
    const rows = [["目前策略", "—", b, null], ...Object.entries(F.best_by_family).map(([k, v]) => [fam[k] || k, v.label, v, F.median_by_family[k]])];
    $("flows-table").innerHTML = `<thead><tr><th>方式</th><th>樣本內最好的設定</th><th>樣本內 Sharpe</th><th>樣本外 Sharpe</th><th>同類中位數（內／外）</th>
      <th>樣本外年化</th><th>樣本外回撤</th><th>樣本外交易</th><th>訊號變動</th></tr></thead><tbody>` +
      rows.map(([name, label, v, med], i) => `<tr${i === 0 ? ' style="font-weight:700"' : ""}><td>${name}</td><td>${label}</td>
        <td>${fmt(v.is_sharpe, 2)}</td><td class="${cls(v.oos_sharpe)}">${fmt(v.oos_sharpe, 2)}</td><td>${med ? `${fmt(med.is_sharpe, 2)}／${fmt(med.oos_sharpe, 2)}` : "—"}</td>
        <td>${sgn(v.oos_cagr_pct, 1)}</td><td>${fmt(v.oos_max_dd_pct, 1)}%</td><td>${v.oos_trades}</td><td>${fmt(v.changed_pct, 0)}%</td></tr>`).join("") + "</tbody>";
    const ff = F.best_by_family.fund_filter, d = F.deflated_best;
    $("flows-note").textContent =
      (ff ? `表現最好的是資金費率擁擠過濾（${ff.label}）：樣本內 Sharpe ${fmt(ff.is_sharpe, 2)} 對 ${fmt(b.is_sharpe, 2)}，靠的是避開 2020–2021 年資金費率過熱時的進場；` +
        `但 2022 年以後資金費率很少這麼高，樣本外它幾乎沒有觸發，結果 ${fmt(ff.oos_sharpe, 2)} 對 ${fmt(b.oos_sharpe, 2)}。` +
        "門檻之間也不一致（0.03% 有幫助、0.05% 反而變差），比較像雜訊。" : "") +
      (d ? `把這 ${F.n_variants} 種也算進試驗次數後，它的 Deflated Sharpe 只有 ${fmt(d.dsr, 1)}%。` : "") +
      "Coinbase 溢價則是兩種用法都沒有幫助：溢價為正、資金費率高，常常正是趨勢最強、這套策略最賺錢的時候。結論是不加入策略；頁面上方的資金費率仍可當擁擠程度的參考。";
  }

  function renderResearch() {
    if (!R) { $("res-lede").textContent = "找不到研究資料（docs/data/research.js），請執行 python scripts/research.py。"; $("robust").hidden = true; return; }
    $("res-lede").textContent = `共測試 ${R.n_configs} 組參數（週期、swing 長度、權重、門檻、多空方向、出場方式、止損方式），` +
      `只用 ${R.is_period} 的資料挑選。樣本內前 20 名在 ${R.oos_period} 有 ${R.oos_share_profitable_top20}% 仍然獲利，` +
      `樣本外 Sharpe 中位數 ${R.oos_median_sharpe_top20}；全部 ${R.n_configs} 組裡則有 ${R.oos_share_profitable_all}% 樣本外獲利。下表是樣本內排名前 10 名，第一列是目前採用的設定。`;
    const wName = W_NAME, xName = X_NAME;
    $("research-table").innerHTML = `<thead><tr><th>週期</th><th>Swing</th><th>權重</th><th>門檻</th><th>方向</th><th>出場</th><th>止損</th>
      <th>樣本內 Sharpe</th><th>樣本外 Sharpe</th><th>樣本外交易</th><th>樣本外 PF</th><th>樣本外回撤</th></tr></thead><tbody>` +
      R.top.slice(0, 10).map((t, i) => `<tr${i === 0 ? ' style="font-weight:700"' : ""}><td>${t.tf}</td><td>${t.swing_len}</td><td>${wName[t.weights] || t.weights}</td>
        <td>${t.threshold}</td><td>${t.sides === "long_only" ? "只做多" : "多空"}</td><td>${xName[t.exit] || t.exit}</td><td>${t.sl_mode === "atr" ? "ATR" : "結構"}</td>
        <td>${fmt(t.is_sharpe, 2)}</td><td class="${cls(t.oos_sharpe)}">${fmt(t.oos_sharpe, 2)}</td><td>${t.oos_trades}</td>
        <td>${fmt(t.oos_profit_factor, 2)}</td><td>${fmt(t.oos_max_dd_pct, 1)}%</td></tr>`).join("") + "</tbody>";
  }

  // ---- charts -----------------------------------------------------------------
  let priceChart, equityChart, candleSeries, markerSets = {}, zoneLines = [];

  function chartBase(el, extra = {}) {
    const LW = window.LightweightCharts;
    return LW.createChart(el, {
      autoSize: true,
      layout: { background: { color: cssVar("--bg") }, textColor: cssVar("--ink-2"), fontFamily: cssVar("--sans"), fontSize: 12 },
      grid: { vertLines: { color: "transparent" }, horzLines: { color: cssVar("--line") } },
      rightPriceScale: { borderColor: cssVar("--line") },
      timeScale: { borderColor: cssVar("--line"), timeVisible: true, secondsVisible: false },
      crosshair: { mode: 0 },
      localization: { locale: "zh-TW", priceFormatter: (p) => fmt(p, 0) },
      ...extra,
    });
  }

  function drawPrice() {
    const el = $("price-chart");
    el.innerHTML = "";
    const C = D.chart, off = C.bar_seconds;
    const up = cssVar("--up"), down = cssVar("--down");
    priceChart = chartBase(el);
    candleSeries = priceChart.addCandlestickSeries({
      upColor: up, downColor: down, borderUpColor: up, borderDownColor: down, wickUpColor: up, wickDownColor: down,
    });
    candleSeries.priceScale().applyOptions({ scaleMargins: { top: 0.05, bottom: 0.24 } });
    candleSeries.setData(C.candles.map(([t, o, h, l, c]) => ({ time: t, open: o, high: h, low: l, close: c })));

    const emaColors = [cssVar("--s1"), cssVar("--s2"), cssVar("--s3")];
    [["ema21", "EMA21"], ["ema55", "EMA55"], ["ema200", "EMA200"]].forEach(([k], i) => {
      const s = priceChart.addLineSeries({ color: emaColors[i], lineWidth: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
      s.setData(C[k].map((v, j) => v == null ? null : { time: C.candles[j][0], value: v }).filter(Boolean));
    });
    $("chart-legend").innerHTML = ["EMA21", "EMA55", "EMA200"].map((n, i) => `<span><i style="background:${emaColors[i]}"></i>${n}</span>`).join("") +
      `<span id="ohlc-readout"></span>`;

    const score = priceChart.addHistogramSeries({ priceScaleId: "score", priceFormat: { type: "price", precision: 0, minMove: 1 }, priceLineVisible: false, lastValueVisible: false });
    priceChart.priceScale("score").applyOptions({ scaleMargins: { top: 0.8, bottom: 0 }, visible: false });
    score.setData(C.score.map((v, j) => v == null ? null : {
      time: C.candles[j][0], value: v,
      color: v >= cfg.threshold ? up : v >= 0 ? up + "70" : down + "70",
    }).filter(Boolean));

    // plan / position lines
    const { lv } = levels();
    const lineColor = { entry: cssVar("--hold"), stop: down, tp: up, pull: cssVar("--s2") };
    lv.forEach((l) => candleSeries.createPriceLine({ price: l.px, color: lineColor[l.k], lineWidth: 1, lineStyle: 0, axisLabelVisible: true, title: l.label }));

    // markers
    const ev = C.events.slice(-14).map((e) => ({
      time: e.time - off, position: e.dir > 0 ? "aboveBar" : "belowBar", color: cssVar("--muted"),
      shape: "circle", size: 0.4, text: e.type,
    }));
    const tr = C.trades.flatMap((t) => [
      { time: t.entry_time - off, position: "belowBar", color: cssVar("--hold"), shape: "arrowUp", text: "進" },
      { time: t.exit_time - off, position: "aboveBar", color: t.r >= 0 ? up : down, shape: "arrowDown", text: sgn(t.r, 1, "R") },
    ]);
    if (POS) tr.push({ time: POS.entry_time - off, position: "belowBar", color: cssVar("--hold"), shape: "arrowUp", text: "持倉" });
    markerSets = { ev, tr };
    applyMarkers();
    applyZones();

    priceChart.subscribeCrosshairMove((p) => {
      const out = $("ohlc-readout");
      if (!out) return;
      const b = p.seriesData?.get(candleSeries);
      const s = p.seriesData?.get(score);
      out.textContent = b ? `開 ${fmt(b.open)} 高 ${fmt(b.high)} 低 ${fmt(b.low)} 收 ${fmt(b.close)}${s ? `　分數 ${fmt(s.value, 0)}` : ""}` : "";
    });
    priceChart.timeScale().setVisibleLogicalRange({ from: C.candles.length - 180, to: C.candles.length + 6 });
  }

  function applyMarkers() {
    const showTrades = $("tg-trades").checked;
    const m = [...markerSets.ev, ...(showTrades ? markerSets.tr : [])].sort((a, b) => a.time - b.time);
    candleSeries.setMarkers(m);
  }

  function applyZones() {
    zoneLines.forEach((l) => candleSeries.removePriceLine(l));
    zoneLines = [];
    if (!$("tg-zones").checked) return;
    const price = D.price;
    const active = D.chart.zones.filter((z) => z.end == null);
    const nearest = (kind, n) => active.filter((z) => z.kind === kind)
      .sort((a, b) => Math.abs((a.top + a.bottom) / 2 - price) - Math.abs((b.top + b.bottom) / 2 - price)).slice(0, n);
    const up = cssVar("--up"), down = cssVar("--down");
    for (const z of [...nearest("bull_ob", 2), ...nearest("bear_ob", 2)]) {
      const c = z.kind === "bull_ob" ? up : down, name = z.kind === "bull_ob" ? "多方 OB" : "空方 OB";
      zoneLines.push(candleSeries.createPriceLine({ price: z.top, color: c, lineWidth: 1, lineStyle: 2, axisLabelVisible: false, title: name }));
      zoneLines.push(candleSeries.createPriceLine({ price: z.bottom, color: c, lineWidth: 1, lineStyle: 2, axisLabelVisible: false, title: "" }));
    }
    for (const z of [...nearest("bull_fvg", 2), ...nearest("bear_fvg", 2)]) {
      const c = z.kind === "bull_fvg" ? up : down;
      zoneLines.push(candleSeries.createPriceLine({ price: (z.top + z.bottom) / 2, color: c, lineWidth: 1, lineStyle: 1, axisLabelVisible: false, title: "FVG" }));
    }
  }

  function drawEquity() {
    const el = $("equity-chart");
    el.innerHTML = "";
    const per = period();
    if (!per) return;
    equityChart = chartBase(el, {
      rightPriceScale: { borderColor: cssVar("--line"), mode: 1 },
      localization: { locale: "zh-TW", priceFormatter: (v) => sgn((v - 1) * 100, 0) },
      timeScale: { borderColor: cssVar("--line"), minBarSpacing: 0.05 },  // ~2,800 daily points must fit
    });
    const C = per.chart, risk = cmpState.risk;
    const lines = [
      [`strategy_${risk}`, "--s1", `策略（每筆風險 ${risk}%）`],
      ["hold_1", "--s2", "買入持有 1×"],
      ["hold_2", "--s3", "買入持有 2×"],
    ].filter(([k]) => C[k]);
    const series = lines.map(([k, c, name]) => {
      const s = equityChart.addLineSeries({ color: cssVar(c), lineWidth: k.startsWith("strategy") ? 2.5 : 1.5, priceLineVisible: false, lastValueVisible: true, title: "" });
      s.setData(C.time.map((t, i) => ({ time: t, value: Math.max(C[k][i], 0.001) })));
      return s;
    });
    equityChart.timeScale().fitContent();
    requestAnimationFrame(() => equityChart.timeScale().fitContent()); // after autoSize has measured the box
    const oosT = Date.parse(D.backtest.oos.start) / 1000;
    const tOos = C.time.find((t) => t >= oosT);
    if (per.key === "full" && tOos) {
      series[0].setMarkers([{ time: tOos, position: "aboveBar", color: cssVar("--muted"), shape: "arrowDown", text: "樣本外開始" }]);
    }
    $("eq-legend").innerHTML = lines.map(([, c, name]) => `<span><i style="background:${cssVar(c)}"></i>${name}</span>`).join("");
  }

  function drawCharts() {
    if (!window.LightweightCharts) {
      $("price-chart").innerHTML = '<p class="note">圖表程式庫載入失敗（需要網路連線到 unpkg.com）。其餘數據不受影響。</p>';
      return;
    }
    priceChart?.remove(); equityChart?.remove();
    drawPrice();
    drawEquity();
  }

  $("tg-trades").addEventListener("change", applyMarkers);
  $("tg-zones").addEventListener("change", applyZones);

  // ---- live price ---------------------------------------------------------------
  const SOURCES = [
    ["Binance", "https://data-api.binance.vision/api/v3/ticker/price?symbol=BTCUSDT", (j) => +j.price],
    ["Binance", "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", (j) => +j.price],
    ["OKX", "https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT", (j) => +j.data[0].last],
  ];
  async function poll() {
    for (const [name, url, pick] of SOURCES) {
      try {
        const r = await fetch(url, { cache: "no-store" });
        if (!r.ok) continue;
        const p = pick(await r.json());
        if (!p) continue;
        const el = $("live-price");
        el.classList.remove("tick-up", "tick-down");
        if (live != null && p !== live) el.classList.add(p > live ? "tick-up" : "tick-down");
        live = p;
        el.textContent = fmt(p, 2);
        $("live-src").textContent = `${name} 即時`;
        renderTicket();
        return;
      } catch { /* try next source */ }
    }
    $("live-src").textContent = "即時價格無法取得，顯示最新收盤";
  }

  // ---- method text derived from the live config ---------------------------------------
  function renderMeta() {
    $("live-price").textContent = fmt(D.price, 2);
    $("m-signal").textContent = `每根 4h K 線收盤後計算共振分數（-100 到 +100）。日線收盤在 EMA200 之上、` +
      `且分數達 ${cfg.threshold} 分時發出做多訊號，下一根 K 線開盤進場。` +
      (cfg.sides === "long_only" ? "只做多，日線轉空時整個策略空手。" : "日線轉空時改為做空。");
    const sl = cfg.sl_mode === "atr" ? `進場價下方 ${cfg.sl_atr} 倍 ATR` : "最近的波段低點下方（限制在 1 到 3 倍 ATR）";
    const exit = cfg.exit_mode === "trail"
      ? `到 ${cfg.tp1_r}R 先平一半並把止損移到進場價，剩下一半用「最高價減 ${cfg.trail_atr} 倍 ATR」的移動停損跟隨趨勢。`
      : cfg.exit_mode === "partial" ? `到 ${cfg.tp1_r}R 先平一半並移到保本，剩下在 ${cfg.tp2_r}R 全部出場。`
      : `在 ${cfg.tp1_r}R 一次出場。`;
    $("m-exit").textContent = `止損放在${sl}。${exit}持有超過一定時間未出場則平倉（約 10 天）。`;
    $("foot").textContent = `資料產生於 ${dateStr(Date.parse(D.generated_at) / 1000)}。策略參數：門檻 ${cfg.threshold}、swing ${cfg.swing_len}、` +
      `止損 ${cfg.sl_atr}×ATR、TP1 ${cfg.tp1_r}R、移動停損 ${cfg.trail_atr}×ATR。本頁僅為研究用途，不構成投資建議。`;
  }

  renderMeta();
  renderFreshness();
  renderVerdict();
  renderTicket();
  renderFactors();
  renderTF();
  renderBacktest();
  renderResearch();
  renderRobust();
  renderLtf();
  renderFlows();
  renderCompare();
  renderHistory();
  drawCharts();
  poll();
  setInterval(poll, 15000);
  setInterval(renderFreshness, 60000);
  window.addEventListener("resize", () => renderTicket());
})();
