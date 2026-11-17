"use strict";

// Charts show times in your browser's local time zone.
const TZ = -new Date().getTimezoneOffset() * 60;
const TV_INTERVALS = { "1m": "1", "5m": "5", "15m": "15", "30m": "30", "1h": "60", "4h": "240" };
const COLORS = { up: "#22c55e", down: "#ef4444", fast: "#4c8dff", slow: "#f59e0b", bg: "#131a26", grid: "#1c2535", text: "#8b97ab", border: "#263247" };

const state = {
  status: null, symbols: [], chartSymbol: null, view: "bot", tvLoadedFor: null,
  settings: null, defaults: null, exchanges: [], timeframes: [], settingsDirty: false, tab: "dashboard",
};

// ------------------------------------------------------------------ helpers
const $ = (id) => document.getElementById(id);

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: { "X-Requested-With": "fetch" } };
  if (opts.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.body);
  }
  const r = await fetch(path, init);
  if (r.status === 401) { window.location.href = "/login"; throw new Error("Not logged in"); }
  let data = null;
  try { data = await r.json(); } catch (_) { /* no body */ }
  if (!r.ok) {
    let detail = data && data.detail;
    if (Array.isArray(detail)) detail = detail.map((d) => d.msg).join("; ");
    throw new Error(detail || `Request failed (${r.status})`);
  }
  return data;
}

function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function num(v) { return typeof v === "number" && isFinite(v); }
function fmtMoney(v, quote) {
  if (!num(v)) return "–";
  const s = v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return quote ? `${s} ${quote}` : s;
}
function fmtSigned(v, quote) { return num(v) ? (v > 0 ? "+" : "") + fmtMoney(v, quote) : "–"; }
function fmtPct(v) { return num(v) ? `${v > 0 ? "+" : ""}${v.toFixed(2)}%` : "–"; }
function fmtPrice(p) {
  if (!num(p)) return "–";
  const a = Math.abs(p);
  const d = a >= 1000 ? 2 : a >= 1 ? 4 : a >= 0.01 ? 6 : 8;
  return p.toLocaleString(undefined, { minimumFractionDigits: Math.min(d, 2), maximumFractionDigits: d });
}
function fmtAmount(a) { return num(a) ? a.toLocaleString(undefined, { maximumFractionDigits: 8 }) : "–"; }
function fmtTime(sec) {
  if (!num(sec)) return "–";
  return new Date(sec * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}
function pnlClass(v) { return !num(v) || v === 0 ? "" : v > 0 ? "up" : "down"; }
function setMsg(el, text, ok) {
  el.textContent = text || "";
  el.className = text ? (ok ? "msg-ok" : "msg-err") : "";
}
function priceFormat(p) {
  const a = Math.abs(p || 0);
  const precision = a >= 1000 ? 2 : a >= 10 ? 3 : a >= 1 ? 4 : a >= 0.01 ? 6 : 8;
  return { type: "price", precision, minMove: Math.pow(10, -precision) };
}
function uniqueTimes(points) {
  // Lightweight Charts needs strictly increasing times.
  const out = [];
  for (const p of points) {
    if (out.length && out[out.length - 1].time >= p.time) out[out.length - 1] = { ...p, time: out[out.length - 1].time };
    else out.push(p);
  }
  return out;
}

// ------------------------------------------------------------------ confirm dialog
function confirmDialog({ title, html, word, okText }) {
  return new Promise((resolve) => {
    const dlg = $("confirm-dialog");
    $("confirm-title").textContent = title;
    $("confirm-body").innerHTML = html;
    $("confirm-ok").textContent = okText || "Confirm";
    $("confirm-input").value = "";
    $("confirm-label").classList.toggle("hidden", !word);
    $("confirm-word").textContent = word || "";
    const ok = $("confirm-ok");
    const check = () => { ok.disabled = !!word && $("confirm-input").value.trim().toUpperCase() !== word; };
    check();
    $("confirm-input").oninput = check;
    const finish = (value) => { dlg.close(); resolve(value); };
    $("confirm-cancel").onclick = () => finish(null);
    $("confirm-form").onsubmit = (ev) => { ev.preventDefault(); if (!ok.disabled) finish(word ? $("confirm-input").value.trim() : true); };
    dlg.oncancel = (ev) => { ev.preventDefault(); finish(null); };
    dlg.showModal();
    if (word) $("confirm-input").focus();
  });
}

// ------------------------------------------------------------------ charts
function baseChart(el) {
  return LightweightCharts.createChart(el, {
    autoSize: true,
    layout: { background: { color: COLORS.bg }, textColor: COLORS.text },
    grid: { vertLines: { color: COLORS.grid }, horzLines: { color: COLORS.grid } },
    timeScale: { timeVisible: true, secondsVisible: false, borderColor: COLORS.border },
    rightPriceScale: { borderColor: COLORS.border },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
  });
}
function priceChart(el) {
  const chart = baseChart(el);
  const candles = chart.addCandlestickSeries({
    upColor: COLORS.up, downColor: COLORS.down, borderVisible: false, wickUpColor: COLORS.up, wickDownColor: COLORS.down,
  });
  const line = { lineWidth: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false };
  const fast = chart.addLineSeries({ ...line, color: COLORS.fast });
  const slow = chart.addLineSeries({ ...line, color: COLORS.slow });
  return { chart, candles, fast, slow, key: null };
}
function equityChart(el) {
  const chart = baseChart(el);
  const series = chart.addAreaSeries({
    lineColor: COLORS.fast, topColor: "rgba(76,141,255,0.35)", bottomColor: "rgba(76,141,255,0.02)", lineWidth: 2,
  });
  return { chart, series };
}
function marker(time, side, price, withText = true) {
  const buy = side === "buy";
  return {
    time, position: buy ? "belowBar" : "aboveBar", color: buy ? COLORS.up : COLORS.down,
    shape: buy ? "arrowUp" : "arrowDown", text: withText ? `${buy ? "BUY" : "SELL"} ${fmtPrice(price)}` : "",
  };
}

let mainChart = null;
let eqChart = null;
let btChart = null;
let btEqChart = null;

async function loadMainChart(forceFit) {
  const sym = state.chartSymbol;
  if (!sym || state.view !== "bot") return;
  const d = await api(`/api/candles?symbol=${encodeURIComponent(sym)}&limit=300`);
  if (sym !== state.chartSymbol) return; // user switched pair meanwhile
  if (!mainChart) mainChart = priceChart($("chart-bot"));
  const last = d.candles.length ? d.candles[d.candles.length - 1].close : 0;
  const pf = priceFormat(last);
  [mainChart.candles, mainChart.fast, mainChart.slow].forEach((s) => s.applyOptions({ priceFormat: pf }));
  mainChart.candles.setData(d.candles.map((c) => ({ ...c, time: c.time + TZ })));
  mainChart.fast.setData(d.fast.map((p) => ({ time: p.time + TZ, value: p.value })));
  mainChart.slow.setData(d.slow.map((p) => ({ time: p.time + TZ, value: p.value })));
  const first = d.candles.length ? d.candles[0].time : 0;
  const tf = d.timeframe_seconds;
  const markers = d.markers
    .filter((o) => o.time >= first)
    .map((o) => marker(Math.floor(o.time / tf) * tf + TZ, o.side, o.price))
    .sort((a, b) => a.time - b.time);
  mainChart.candles.setMarkers(markers);
  $("lg-fast").textContent = `EMA ${d.fast_period}`;
  $("lg-slow").textContent = `EMA ${d.slow_period}`;
  const key = `${sym}|${d.timeframe}`;
  if (forceFit || mainChart.key !== key) {
    mainChart.chart.timeScale().fitContent();
    mainChart.key = key;
  }
}

async function loadEquity() {
  const days = $("equity-days").value;
  const d = await api(`/api/equity?days=${days}`);
  if (!eqChart) eqChart = equityChart($("chart-equity"));
  eqChart.series.setData(uniqueTimes(d.points.map((p) => ({ time: Math.floor(p.time) + TZ, value: p.equity }))));
  eqChart.chart.timeScale().fitContent();
}

function loadTradingView() {
  const st = state.status;
  if (!st || !state.chartSymbol) return;
  const [base, quote] = state.chartSymbol.split("/");
  const tvSymbol = `${st.tradingview_prefix}:${base}${quote}`;
  const key = `${tvSymbol}|${st.timeframe}`;
  if (state.tvLoadedFor === key) return;
  state.tvLoadedFor = key;
  const box = $("chart-tv");
  box.innerHTML = "";
  const wrap = document.createElement("div");
  wrap.className = "tradingview-widget-container";
  wrap.style.cssText = "height:100%;width:100%";
  const inner = document.createElement("div");
  inner.className = "tradingview-widget-container__widget";
  inner.style.cssText = "height:100%;width:100%";
  wrap.appendChild(inner);
  const script = document.createElement("script");
  script.type = "text/javascript";
  script.src = "https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js";
  script.async = true;
  script.text = JSON.stringify({
    autosize: true, symbol: tvSymbol, interval: TV_INTERVALS[st.timeframe] || "5", timezone: "Etc/UTC",
    theme: "dark", style: "1", locale: "en", allow_symbol_change: true, support_host: "https://www.tradingview.com",
  });
  wrap.appendChild(script);
  box.appendChild(wrap);
}

function setView(view) {
  state.view = view;
  document.querySelectorAll(".seg-btn").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  $("chart-bot").classList.toggle("hidden", view !== "bot");
  $("chart-tv").classList.toggle("hidden", view !== "tv");
  document.querySelector(".legend").classList.toggle("hidden", view !== "bot");
  if (view === "tv") loadTradingView();
  else loadMainChart(false).catch(showChartError);
}

function showChartError(e) { setErrorBanner(`Chart: ${e.message}`); }

// ------------------------------------------------------------------ status rendering
function setErrorBanner(text) {
  const el = $("error-banner");
  el.textContent = text || "";
  el.classList.toggle("hidden", !text);
}

function fillSymbolSelect(sel, symbols, current) {
  const existing = Array.from(sel.options).map((o) => o.value).join(",");
  if (existing !== symbols.join(",")) {
    sel.innerHTML = symbols.map((s) => `<option value="${esc(s)}">${esc(s)}</option>`).join("");
  }
  if (current && symbols.includes(current)) sel.value = current;
}

function renderStatus(st) {
  state.status = st;
  const q = st.quote;

  // header
  const badge = $("mode-badge");
  badge.textContent = st.mode === "live" ? "LIVE" : "PAPER";
  badge.className = `badge ${st.mode}`;
  const pill = $("run-status");
  let label = "Stopped", cls = "stopped";
  if (!st.engine_alive) { label = "Engine offline"; cls = "offline"; }
  else if (st.halted_today) { label = "Halted: daily loss limit"; cls = "halted"; }
  else if (st.trading_enabled) { label = "Running"; cls = "running"; }
  pill.textContent = `${label} · ${st.exchange} · ${st.timeframe}`;
  pill.className = `pill ${cls}`;
  $("btn-start").disabled = st.trading_enabled;
  $("btn-stop").disabled = !st.trading_enabled;
  $("btn-emergency").disabled = !st.trading_enabled && st.positions.length === 0;
  $("live-banner").classList.toggle("hidden", st.mode !== "live");
  $("live-exchange").textContent = st.exchange;

  const err = st.last_error;
  const recent = err && Date.now() / 1000 - err.time < 600;
  setErrorBanner(recent ? `Last problem (${fmtTime(err.time)}): ${err.message}` : "");

  // stats
  $("st-equity").textContent = fmtMoney(st.equity, q);
  $("st-cash").textContent = fmtMoney(st.cash, q);
  const day = $("st-day");
  day.textContent = fmtSigned(st.day_pnl, q);
  day.className = `stat-value ${pnlClass(st.day_pnl)}`;
  const tot = $("st-pnl");
  tot.textContent = fmtSigned(st.stats.total_pnl, q);
  tot.className = `stat-value ${pnlClass(st.stats.total_pnl)}`;
  $("st-winrate").textContent = st.stats.count
    ? `${st.stats.win_rate.toFixed(0)}% (${st.stats.count} trade${st.stats.count === 1 ? "" : "s"})` : "No trades yet";
  $("st-open").textContent = String(st.positions.length);

  // positions
  $("positions-body").innerHTML = st.positions.length ? st.positions.map((p) => `
    <tr>
      <td><b>${esc(p.symbol)}</b></td>
      <td>${fmtAmount(p.amount)}</td>
      <td>${fmtPrice(p.entry_price)}</td>
      <td>${fmtPrice(p.price)}</td>
      <td>${fmtMoney(p.value)}</td>
      <td class="${pnlClass(p.pnl)}">${fmtSigned(p.pnl)} (${fmtPct(p.pnl_pct)})</td>
      <td title="${esc(p.stop_label)}">${fmtPrice(p.stop)}${p.stop_label === "trailing stop" ? " (trail)" : ""}</td>
      <td>${p.target ? fmtPrice(p.target) : "off"}</td>
      <td>${fmtTime(p.entry_time)}</td>
      <td><button class="btn btn-ghost" data-close="${p.id}" data-symbol="${esc(p.symbol)}">Sell now</button></td>
    </tr>`).join("") : `<tr><td colspan="10" class="empty">No open positions</td></tr>`;

  // trades
  $("trades-body").innerHTML = st.trades.length ? st.trades.map((t) => `
    <tr>
      <td>${fmtTime(t.exit_time)}</td>
      <td>${esc(t.symbol)}</td>
      <td>${fmtPrice(t.entry_price)}</td>
      <td>${fmtPrice(t.exit_price)}</td>
      <td class="${pnlClass(t.pnl)}">${fmtSigned(t.pnl)} (${fmtPct(t.pnl_pct)})</td>
      <td class="wrap">${esc(t.exit_reason)}</td>
    </tr>`).join("") : `<tr><td colspan="6" class="empty">No trades yet</td></tr>`;

  // log
  $("log").innerHTML = st.logs.length ? st.logs.map((l) =>
    `<div class="${esc(l.level)}"><span class="t">${fmtTime(l.time)}</span>${esc(l.message)}</div>`).join("")
    : `<div class="muted">Nothing yet</div>`;

  // latest signal per pair
  $("signals").innerHTML = st.symbols.map((s) => {
    const sig = st.signals[s];
    const px = st.prices[s];
    const price = px ? fmtPrice(px.last) : "–";
    if (!sig) return `<span><b>${esc(s)}</b> ${price} · waiting for the next closed candle</span>`;
    const rsi = num(sig.rsi) ? ` · RSI ${sig.rsi.toFixed(1)}` : "";
    return `<span><b>${esc(s)}</b> ${price}${rsi} · ${esc(sig.reason)}</span>`;
  }).join("");

  // pair pickers
  state.symbols = st.symbols;
  if (!state.chartSymbol || !st.symbols.includes(state.chartSymbol)) state.chartSymbol = st.symbols[0];
  fillSymbolSelect($("chart-symbol"), st.symbols, state.chartSymbol);
  fillSymbolSelect($("bt-symbol"), st.symbols, $("bt-symbol").value || st.symbols[0]);

  // mode tab
  $("mode-current").textContent = st.mode === "live" ? `LIVE (real money on ${st.exchange})` : "PAPER (simulated)";
  $("btn-go-live").disabled = st.mode === "live";
  $("btn-go-paper").disabled = st.mode === "paper";
  $("btn-reset-paper").disabled = st.mode !== "paper";

  if (state.view === "tv") loadTradingView();
}

async function refreshStatus() {
  try {
    renderStatus(await api("/api/status"));
  } catch (e) {
    setErrorBanner(`Cannot reach the bot: ${e.message}`);
  }
}

// ------------------------------------------------------------------ settings form
const SETTINGS_SCHEMA = [
  { group: "Market", fields: [
    { key: "exchange", label: "Exchange", type: "select", options: () => state.exchanges,
      help: "Where prices come from and, in live mode, where orders are placed. Close all positions before changing it." },
    { key: "symbols", label: "Pairs to trade", type: "text",
      help: "Comma-separated, e.g. BTC/USD, ETH/USD. All pairs must use the same quote currency." },
    { key: "timeframe", label: "Candle size", type: "select", options: () => state.timeframes,
      help: "The strategy checks for signals each time a candle of this size closes. Day trading: 1m–15m." },
  ]},
  { group: "Strategy: EMA crossover with RSI filter", fields: [
    { key: "fast_ema", label: "Fast EMA length (candles)", type: "int", help: "BUY when this average crosses above the slow one; SELL when it crosses below." },
    { key: "slow_ema", label: "Slow EMA length (candles)", type: "int", help: "Must be bigger than the fast EMA." },
    { key: "rsi_period", label: "RSI length (candles)", type: "int", help: "How many candles the RSI momentum indicator looks at." },
    { key: "rsi_overbought", label: "Skip buys when RSI is above", type: "num", help: "Avoids buying right after a big spike. 100 = never skip." },
  ]},
  { group: "Risk management", fields: [
    { key: "position_size_pct", label: "Trade size (% of account)", type: "num", help: "How much of your account value goes into each trade." },
    { key: "max_trade_value", label: "Max trade size ({quote})", type: "num", help: "Hard cap per trade, whatever the % says. A good safety net for live trading." },
    { key: "max_open_positions", label: "Max open positions", type: "int", help: "The most trades allowed open at the same time." },
    { key: "stop_loss_pct", label: "Stop-loss %", type: "num", help: "Sell if the price falls this far below the buy price." },
    { key: "take_profit_pct", label: "Take-profit %", type: "num", help: "Sell if the price rises this far above the buy price. 0 = off." },
    { key: "trailing_stop_pct", label: "Trailing stop %", type: "num", help: "Sell if the price falls this far below its highest point since buying. 0 = off." },
    { key: "max_daily_loss_pct", label: "Daily loss limit %", type: "num", help: "If the account drops this much in one day (UTC), sell everything and stop until tomorrow." },
  ]},
  { group: "Paper trading & backtest simulation", fields: [
    { key: "paper_starting_balance", label: "Paper starting balance ({quote})", type: "num", help: "Takes effect when you reset the paper account (Paper / Live tab)." },
    { key: "fee_pct", label: "Exchange fee per trade %", type: "num", help: "Charged on every simulated buy and sell. Also used to size live orders. Kraken: about 0.4%." },
    { key: "slippage_pct", label: "Slippage %", type: "num", help: "Simulated price slippage on every paper fill." },
  ]},
  { group: "Engine", fields: [
    { key: "poll_interval_seconds", label: "Check prices every (seconds)", type: "int", help: "How often stop-loss and take-profit levels are checked." },
  ]},
];
const FIELD_LABELS = {};
SETTINGS_SCHEMA.forEach((g) => g.fields.forEach((f) => { FIELD_LABELS[f.key] = f.label; }));

function quoteOf(settings) {
  const first = (settings.symbols || [])[0] || "";
  return first.split("/")[1] || "";
}

function buildSettingsForm(values) {
  const q = quoteOf(values);
  $("settings-fields").innerHTML = SETTINGS_SCHEMA.map((g) => `
    <fieldset><legend>${esc(g.group)}</legend><div class="fields">
      ${g.fields.map((f) => {
        const label = esc(f.label.replace("{quote}", q));
        let input;
        if (f.type === "select") {
          input = `<select id="set-${f.key}">${f.options().map((o) => `<option value="${esc(o)}">${esc(o)}</option>`).join("")}</select>`;
        } else if (f.type === "text") {
          input = `<input type="text" id="set-${f.key}" spellcheck="false">`;
        } else {
          input = `<input type="number" id="set-${f.key}" step="${f.type === "int" ? "1" : "any"}" inputmode="decimal">`;
        }
        return `<div class="field"><label for="set-${f.key}">${label}</label>${input}<small>${esc(f.help)}</small></div>`;
      }).join("")}
    </div></fieldset>`).join("");
  fillSettingsForm(values);
  $("settings-fields").querySelectorAll("input, select").forEach((el) => {
    el.addEventListener("input", () => { state.settingsDirty = true; });
  });
}

function fillSettingsForm(values) {
  SETTINGS_SCHEMA.forEach((g) => g.fields.forEach((f) => {
    const el = $(`set-${f.key}`);
    const v = values[f.key];
    el.value = Array.isArray(v) ? v.join(", ") : v;
  }));
}

function readSettingsForm() {
  const out = {};
  SETTINGS_SCHEMA.forEach((g) => g.fields.forEach((f) => {
    const raw = $(`set-${f.key}`).value;
    if (f.type === "select" || f.type === "text") out[f.key] = raw;
    else out[f.key] = raw.trim() === "" ? null : Number(raw);
  }));
  return out;
}

function friendlyError(msg) {
  return msg.split("; ").map((part) => {
    const i = part.indexOf(": ");
    if (i > 0 && FIELD_LABELS[part.slice(0, i)]) return `${FIELD_LABELS[part.slice(0, i)].replace(" ({quote})", "")}: ${part.slice(i + 2)}`;
    return part;
  }).join(" · ");
}

async function loadSettings() {
  const d = await api("/api/settings");
  state.settings = d.settings;
  state.defaults = d.defaults;
  state.exchanges = d.exchanges;
  state.timeframes = d.timeframes;
  buildSettingsForm(d.settings);
  state.settingsDirty = false;
}

// ------------------------------------------------------------------ backtest
function stat(label, value, cls) {
  return `<div class="stat"><div class="stat-label">${esc(label)}</div><div class="stat-value ${cls || ""}">${value}</div></div>`;
}

async function runBacktest() {
  const msg = $("bt-msg");
  const btn = $("bt-run");
  btn.disabled = true;
  setMsg(msg, "");
  msg.textContent = "Downloading history and running… (can take up to a minute)";
  msg.className = "muted";
  try {
    const r = await api("/api/backtest", { method: "POST", body: {
      symbol: $("bt-symbol").value, candles: Number($("bt-candles").value) || 1000 } });
    const q = state.status ? state.status.quote : "";
    const days = (r.end_time - r.start_time) / 86400;
    $("bt-stats").innerHTML = [
      stat("Strategy return", fmtPct(r.return_pct), pnlClass(r.return_pct)),
      stat("Buy & hold return", fmtPct(r.buy_and_hold_pct), pnlClass(r.buy_and_hold_pct)),
      stat("Final value", fmtMoney(r.final_equity, q)),
      stat("Trades", String(r.trades_count)),
      stat("Win rate", num(r.win_rate) ? `${r.win_rate.toFixed(0)}%` : "–"),
      stat("Profit factor", num(r.profit_factor) ? r.profit_factor.toFixed(2) : "–"),
      stat("Max drawdown", num(r.max_drawdown_pct) ? `-${r.max_drawdown_pct.toFixed(2)}%` : "–", r.max_drawdown_pct > 0 ? "down" : ""),
      stat("Fees paid", fmtMoney(r.fees_paid, q)),
    ].join("");
    let note = `${r.symbol} · ${r.candles} × ${r.timeframe} candles · ${fmtTime(r.start_time)} → ${fmtTime(r.end_time)} (${days.toFixed(1)} days)`;
    if (r.candles < r.requested_candles) note += ` · the exchange only provides ${r.candles} candles of history at this size`;
    if (r.open_position) note += " · a position was still open at the end (valued at the last price)";
    setMsg(msg, "");
    msg.textContent = note;
    msg.className = "muted";
    $("bt-results").classList.remove("hidden");

    if (!btChart) btChart = priceChart($("bt-chart"));
    const last = r.candles_data.length ? r.candles_data[r.candles_data.length - 1].close : 0;
    btChart.candles.applyOptions({ priceFormat: priceFormat(last) });
    btChart.candles.setData(r.candles_data.map((c) => ({ ...c, time: c.time + TZ })));
    btChart.fast.setData([]);
    btChart.slow.setData([]);
    const markers = [];
    const labels = r.trades.length <= 12; // price labels overlap when there are many trades
    r.trades.forEach((t) => {
      markers.push(marker(t.entry_time + TZ, "buy", t.entry_price, labels));
      markers.push(marker(t.exit_time + TZ, "sell", t.exit_price, labels));
    });
    markers.sort((a, b) => a.time - b.time);
    btChart.candles.setMarkers(markers);
    btChart.chart.timeScale().fitContent();

    if (!btEqChart) btEqChart = equityChart($("bt-equity"));
    btEqChart.series.setData(uniqueTimes(r.equity_curve.map((p) => ({ time: p.time + TZ, value: p.equity }))));
    btEqChart.chart.timeScale().fitContent();

    $("bt-trades").innerHTML = r.trades.length ? r.trades.slice().reverse().map((t) => `
      <tr>
        <td>${fmtTime(t.entry_time)}</td><td>${fmtTime(t.exit_time)}</td>
        <td>${fmtPrice(t.entry_price)}</td><td>${fmtPrice(t.exit_price)}</td>
        <td class="${pnlClass(t.pnl)}">${fmtSigned(t.pnl)} (${fmtPct(t.pnl_pct)})</td>
        <td>${esc(t.exit_reason)}</td>
      </tr>`).join("") : `<tr><td colspan="6" class="empty">The strategy made no trades in this period</td></tr>`;
  } catch (e) {
    setMsg(msg, e.message, false);
  } finally {
    btn.disabled = false;
  }
}

// ------------------------------------------------------------------ mode & keys
async function loadKeys() {
  try {
    const k = await api("/api/keys");
    $("keys-status").textContent = k.set ? `Saved (key ${k.api_key})` : "Not set";
  } catch (e) {
    $("keys-status").textContent = e.message;
  }
}

async function goLive() {
  const st = state.status;
  const msg = $("mode-msg");
  setMsg(msg, "");
  if (!st.api_keys_set) { setMsg(msg, "Save your exchange API keys below first.", false); return; }
  if (st.trading_enabled) { setMsg(msg, "Press Stop first.", false); return; }
  let s = state.settings;
  if (!s) { await loadSettings(); s = state.settings; }
  const q = st.quote;
  const typed = await confirmDialog({
    title: "Switch to LIVE trading?",
    word: "LIVE",
    okText: "Switch to live",
    html: `<ul>
      <li>Real market orders will be placed on <b>${esc(st.exchange)}</b> using your API keys.</li>
      <li>Each trade uses <b>${s.position_size_pct}%</b> of your ${esc(q)} balance, capped at <b>${fmtMoney(s.max_trade_value, q)}</b>.</li>
      <li>Stop-loss <b>${s.stop_loss_pct}%</b>, daily loss limit <b>${s.max_daily_loss_pct}%</b>.</li>
      <li>The bot will <b>not</b> start trading until you press <b>Start</b>.</li>
      <li>Stop-loss and take-profit are handled by this bot, so they only work while it is running.</li>
    </ul>`,
  });
  if (!typed) return;
  msg.textContent = "Checking your API keys with the exchange…";
  msg.className = "muted";
  try {
    await api("/api/mode", { method: "POST", body: { mode: "live", confirm: typed } });
    setMsg(msg, "You are now in LIVE mode. Press Start when you're ready.", true);
  } catch (e) {
    setMsg(msg, e.message, false);
  }
  refreshAll();
}

async function goPaper() {
  const msg = $("mode-msg");
  try {
    await api("/api/mode", { method: "POST", body: { mode: "paper" } });
    setMsg(msg, "Switched to PAPER mode.", true);
  } catch (e) {
    setMsg(msg, e.message, false);
  }
  refreshAll();
}

// ------------------------------------------------------------------ wiring
function showTab(tab) {
  state.tab = tab;
  document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  document.querySelectorAll(".tab-panel").forEach((p) => p.classList.toggle("hidden", p.id !== `tab-${tab}`));
  if (tab === "settings" && !state.settingsDirty) {
    loadSettings().catch((e) => setMsg($("settings-msg"), e.message, false));
  }
  if (tab === "mode") loadKeys();
  if (tab === "dashboard") refreshAll();
}

function refreshAll() {
  refreshStatus().then(() => {
    loadMainChart(false).catch(showChartError);
    loadEquity().catch(() => {});
  });
}

function bind() {
  document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
  document.querySelectorAll(".seg-btn").forEach((b) => b.addEventListener("click", () => setView(b.dataset.view)));

  $("chart-symbol").addEventListener("change", (ev) => {
    state.chartSymbol = ev.target.value;
    if (state.view === "tv") loadTradingView();
    else loadMainChart(true).catch(showChartError);
  });
  $("equity-days").addEventListener("change", () => loadEquity().catch(() => {}));

  $("btn-start").addEventListener("click", async () => {
    try {
      if (state.status && state.status.mode === "live") {
        const ok = await confirmDialog({ title: "Start LIVE trading?", okText: "Start",
          html: `<p>The bot will place <b>real orders</b> on ${esc(state.status.exchange)} when it sees signals.</p>` });
        if (!ok) return;
      }
      await api("/api/start", { method: "POST" });
    } catch (e) { alert(e.message); }
    refreshStatus();
  });
  $("btn-stop").addEventListener("click", async () => {
    try { await api("/api/stop", { method: "POST" }); } catch (e) { alert(e.message); }
    refreshStatus();
  });
  $("btn-emergency").addEventListener("click", async () => {
    const ok = await confirmDialog({ title: "Emergency stop", okText: "Sell all & stop",
      html: "<p>Sell <b>all</b> open positions at the market price right now and stop the bot?</p>" });
    if (!ok) return;
    try {
      const r = await api("/api/emergency-stop", { method: "POST" });
      if (!r.ok) alert(`Could not close: ${r.failures.join(", ")}. Check the activity log and your exchange.`);
    } catch (e) { alert(e.message); }
    refreshAll();
  });
  $("btn-logout").addEventListener("click", async () => {
    try { await api("/api/logout", { method: "POST" }); } catch (_) { /* ignore */ }
    window.location.href = "/login";
  });

  $("positions-body").addEventListener("click", async (ev) => {
    const btn = ev.target.closest("button[data-close]");
    if (!btn) return;
    const ok = await confirmDialog({ title: `Sell ${btn.dataset.symbol} now?`, okText: "Sell now",
      html: "<p>The position will be sold at the current market price.</p>" });
    if (!ok) return;
    btn.disabled = true;
    try { await api(`/api/positions/${btn.dataset.close}/close`, { method: "POST" }); } catch (e) { alert(e.message); }
    refreshAll();
  });

  // settings
  $("settings-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const msg = $("settings-msg");
    const btn = $("settings-save");
    btn.disabled = true;
    msg.textContent = "Saving…";
    msg.className = "muted";
    try {
      const r = await api("/api/settings", { method: "POST", body: readSettingsForm() });
      state.settings = r.settings;
      buildSettingsForm(r.settings);
      state.settingsDirty = false;
      setMsg(msg, "Saved. The bot is using the new settings.", true);
      state.tvLoadedFor = null;
      refreshAll();
    } catch (e) {
      setMsg(msg, friendlyError(e.message), false);
    } finally {
      btn.disabled = false;
    }
  });
  $("settings-reload").addEventListener("click", () => {
    loadSettings().then(() => setMsg($("settings-msg"), "Changes undone.", true))
      .catch((e) => setMsg($("settings-msg"), e.message, false));
  });
  $("settings-defaults").addEventListener("click", () => {
    if (!state.defaults) return;
    fillSettingsForm(state.defaults);
    state.settingsDirty = true;
    setMsg($("settings-msg"), "Defaults filled in. Press Save to use them.", true);
  });

  // mode & keys
  $("btn-go-live").addEventListener("click", goLive);
  $("btn-go-paper").addEventListener("click", goPaper);
  $("keys-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const msg = $("keys-msg");
    try {
      await api("/api/keys", { method: "POST", body: {
        api_key: $("key-api").value, api_secret: $("key-secret").value, api_password: $("key-pass").value } });
      $("key-api").value = ""; $("key-secret").value = ""; $("key-pass").value = "";
      setMsg(msg, "Keys saved. Press Test connection to check them.", true);
    } catch (e) { setMsg(msg, e.message, false); }
    loadKeys();
    refreshStatus();
  });
  $("btn-test-keys").addEventListener("click", async () => {
    const msg = $("keys-msg");
    msg.textContent = "Connecting…";
    msg.className = "muted";
    try {
      const r = await api("/api/keys/test", { method: "POST" });
      setMsg(msg, `Connected. ${r.quote} balance: ${fmtMoney(r.free)} available, ${fmtMoney(r.total)} total.`, true);
    } catch (e) { setMsg(msg, e.message, false); }
  });
  $("btn-delete-keys").addEventListener("click", async () => {
    const ok = await confirmDialog({ title: "Remove API keys?", okText: "Remove",
      html: "<p>The saved keys will be deleted from this machine.</p>" });
    if (!ok) return;
    const msg = $("keys-msg");
    try { await api("/api/keys", { method: "DELETE" }); setMsg(msg, "Keys removed.", true); }
    catch (e) { setMsg(msg, e.message, false); }
    loadKeys();
    refreshStatus();
  });
  $("btn-reset-paper").addEventListener("click", async () => {
    const typed = await confirmDialog({ title: "Reset paper account?", word: "RESET", okText: "Reset",
      html: "<p>All paper trades and history will be deleted and the balance set back to the paper starting balance.</p>" });
    if (!typed) return;
    const msg = $("reset-msg");
    try { await api("/api/paper/reset", { method: "POST", body: { confirm: typed } }); setMsg(msg, "Paper account reset.", true); }
    catch (e) { setMsg(msg, e.message, false); }
    refreshAll();
  });

  // password
  $("password-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const msg = $("pw-msg");
    if ($("pw-new").value !== $("pw-new2").value) { setMsg(msg, "The new passwords don't match.", false); return; }
    try {
      await api("/api/password", { method: "POST", body: { current: $("pw-current").value, new: $("pw-new").value } });
      ev.target.reset();
      setMsg(msg, "Password changed.", true);
    } catch (e) { setMsg(msg, e.message, false); }
  });

  $("bt-run").addEventListener("click", runBacktest);
}

// ------------------------------------------------------------------ polling loops
function every(ms, fn) {
  const loop = async () => {
    if (!document.hidden) { try { await fn(); } catch (_) { /* shown elsewhere */ } }
    setTimeout(loop, ms);
  };
  setTimeout(loop, ms);
}

bind();
refreshAll();
every(5000, refreshStatus);
every(15000, () => (state.tab === "dashboard" ? loadMainChart(false) : null));
every(60000, () => (state.tab === "dashboard" ? loadEquity() : null));
