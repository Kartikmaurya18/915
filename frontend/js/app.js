const state = {
  selectedInterval: "1",
  lastPrice: null,
  chart: null,
  candleSeries: null,
  reconnectTimer: null,
  atmStrike: null,
  configDirty: false,
  paperInitialized: false,
  knownHistoryIds: new Set(),
  activeView: "overview",
  tickHistory: [], // [{count, at}] for client-side tick-rate estimate
  logPollTimer: null,
  settingsLoaded: false,
  // ATM Straddle chart (candlestick, per-strike)
  straddleInterval: "1",
  straddleView: "straddle", // "straddle" | "ce" | "pe" — which OHLC series to plot; independent of contract selection
  straddleChart: null,
  straddleSeries: null,
  straddleLastFetchedStrike: null,
  straddleFetchInFlight: false,
  straddleCandleData: null, // cached GET /api/straddle-candles response: {strike, by_interval}
  straddleLastValue: null, // tick-over-tick change baseline for the current strike+view
  latestSnapshot: null, // last websocket snapshot, for instant re-renders between ticks
  // THE single source of truth for "which contract(s) are selected" — for
  // Paper Trading AND the Straddle Chart. Keyed by "<strike>_<optionType>"
  // -> {strike, optionType, side, quantity}. Built by checking "Sel" boxes
  // (or clicking a CE/PE price, a shortcut for the same checkbox) in the
  // Option Chain. 1 entry = a single-strike trade; 2+ = multi-strike — both
  // are the exact same state and the exact same execute path
  // (POST /api/paper/open, one call per entry). There is no separate
  // "selected contract" state anywhere else in the app.
  multiSelected: new Map(),
  // Which of the currently-selected strikes the Straddle Chart displays.
  // null = auto-derive (see resolveStraddleFocusStrike()): follow ATM with
  // nothing selected, the strike itself with exactly one selected: only
  // consulted when 2+ distinct strikes are selected, via the picker.
  straddleFocusStrike: null,

  // -- Date/Session selector (historical replay) --------------------------
  // "live" (today, websocket-driven, unchanged existing behavior) or
  // "historical" (a past Asia/Kolkata trading date, read-only, no live
  // ticks applied — see handleSnapshot()'s early-return guard).
  sessionMode: "live",
  todayIso: null, // "YYYY-MM-DD" in Asia/Kolkata, from GET /api/history/dates
  selectedDateIso: null,
  availableDates: [], // dates with persisted data, for "skip to nearest" UX
  historicalSession: null, // last-fetched GET /api/history/session bundle
  historicalStrike: null, // strike currently shown in the historical Straddle Chart
};

const els = {
  symbolTitle: document.getElementById("symbol-title"),
  spotPrice: document.getElementById("spot-price"),
  spotChange: document.getElementById("spot-change"),
  lastUpdated: document.getElementById("last-updated"),
  connStatus: document.getElementById("conn-status"),
  staleBadge: document.getElementById("stale-badge"),
  sessionBadge: document.getElementById("session-badge"),
  tickCount: document.getElementById("tick-count"),
  tickReliability: document.getElementById("tick-reliability"),
  dataSource: document.getElementById("data-source"),
  lastTickAge: document.getElementById("last-tick-age"),
  atmMethodChip: document.getElementById("atm-method-chip"),
  atmStrike: document.getElementById("atm-strike"),
  atmCompare: document.getElementById("atm-compare"),
  atmPriceStrike: document.getElementById("atm-price-strike"),
  atmCall: document.getElementById("atm-call"),
  atmCallDelta: document.getElementById("atm-call-delta"),
  atmPut: document.getElementById("atm-put"),
  atmPutDelta: document.getElementById("atm-put-delta"),
  atmStraddle: document.getElementById("atm-straddle"),
  moLtp: document.getElementById("mo-ltp"),
  moChange: document.getElementById("mo-change"),
  moTicks: document.getElementById("mo-ticks"),
  moTimestamp: document.getElementById("mo-timestamp"),
  chainExpiry: document.getElementById("chain-expiry"),
  optionChainBody: document.getElementById("option-chain-body"),
  candlesBody: document.getElementById("candles-body"),
  candleIntervalLabel: document.getElementById("candle-interval-label"),
  activeCandle: document.getElementById("active-candle"),
  intervalTabs: document.getElementById("interval-tabs"),
  toast: document.getElementById("toast"),
  simBanner: document.getElementById("sim-banner"),
  simBannerText: document.getElementById("sim-banner-text"),
  paperConfigForm: document.getElementById("paper-config-form"),
  ptTp: document.getElementById("pt-tp"),
  ptSl: document.getElementById("pt-sl"),
  ptTpAmount: document.getElementById("pt-tp-amount"),
  ptSlAmount: document.getElementById("pt-sl-amount"),
  ptError: document.getElementById("pt-error"),
  // Multi-Strike Selection — the ONE contract-selection UI (1 or N contracts)
  multiSelectCount: document.getElementById("multi-select-count"),
  multiSelectClearBtn: document.getElementById("multi-select-clear-btn"),
  multiSelectEmpty: document.getElementById("multi-select-empty"),
  multiSelectTableWrap: document.getElementById("multi-select-table-wrap"),
  multiSelectBody: document.getElementById("multi-select-body"),
  multiExecuteBtn: document.getElementById("multi-execute-btn"),
  defaultQty: document.getElementById("default-qty"),
  // P&L Summary strip (portfolio-wide: unrealized + realized, turnover)
  pnlsOpenCount: document.getElementById("pnls-open-count"),
  pnlsInvested: document.getElementById("pnls-invested"),
  pnlsUnrealized: document.getElementById("pnls-unrealized"),
  pnlsRealized: document.getElementById("pnls-realized"),
  pnlsTotal: document.getElementById("pnls-total"),
  pnlsTotalPercent: document.getElementById("pnls-total-percent"),
  pnlsBuyTurnover: document.getElementById("pnls-buy-turnover"),
  pnlsSellTurnover: document.getElementById("pnls-sell-turnover"),
  pnlsTotalTurnover: document.getElementById("pnls-total-turnover"),
  pnlsPositionValue: document.getElementById("pnls-position-value"),
  positionsBody: document.getElementById("positions-body"),
  historyBody: document.getElementById("history-body"),
  chartTooltip: document.getElementById("chart-tooltip"),
  mainTabs: document.getElementById("main-tabs"),
  // ATM Straddle chart
  straddleViewTabs: document.getElementById("straddle-view-tabs"),
  straddleIntervalTabs: document.getElementById("straddle-interval-tabs"),
  straddleCurrentValue: document.getElementById("straddle-current-value"),
  straddleCurrentChange: document.getElementById("straddle-current-change"),
  straddleTrackedStrike: document.getElementById("straddle-tracked-strike"),
  straddleExpiry: document.getElementById("straddle-expiry"),
  straddleModeChip: document.getElementById("straddle-mode-chip"),
  straddleFocusSelect: document.getElementById("straddle-focus-select"),
  straddleChartTooltip: document.getElementById("straddle-chart-tooltip"),
  straddleLegendLabel: document.getElementById("straddle-legend-label"),
  // RRG
  rrgSvg: document.getElementById("rrg-svg"),
  rrgBody: document.getElementById("rrg-body"),
  // Data quality
  dqConnection: document.getElementById("dq-connection"),
  dqSource: document.getElementById("dq-source"),
  dqTickCount: document.getElementById("dq-tick-count"),
  dqTickRate: document.getElementById("dq-tick-rate"),
  dqLastTick: document.getElementById("dq-last-tick"),
  dqStale: document.getElementById("dq-stale"),
  dqSession: document.getElementById("dq-session"),
  dqRejects: document.getElementById("dq-rejects"),
  dqChainStatus: document.getElementById("dq-chain-status"),
  dqPersistence: document.getElementById("dq-persistence"),
  // Logs
  logsBody: document.getElementById("logs-body"),
  logLevelFilter: document.getElementById("log-level-filter"),
  logFollow: document.getElementById("log-follow"),
  // Settings
  setDataSource: document.getElementById("set-data-source"),
  replaySequenceField: document.getElementById("replay-sequence-field"),
  replaySpeedField: document.getElementById("replay-speed-field"),
  setReplaySequence: document.getElementById("set-replay-sequence"),
  setReplaySpeed: document.getElementById("set-replay-speed"),
  setExpiry: document.getElementById("set-expiry"),
  setAtmMethod: document.getElementById("set-atm-method"),
  setDeltaThreshold: document.getElementById("set-delta-threshold"),
  setTp: document.getElementById("set-tp"),
  setSl: document.getElementById("set-sl"),
  setCandleBasis: document.getElementById("set-candle-basis"),
  settingsSourceForm: document.getElementById("settings-source-form"),
  settingsAtmForm: document.getElementById("settings-atm-form"),
  settingsRiskForm: document.getElementById("settings-risk-form"),
  settingsBasisForm: document.getElementById("settings-basis-form"),
  settingsStatus: document.getElementById("settings-status"),
  // Date/Session selector
  sessionBar: document.getElementById("session-bar"),
  sessionPrevBtn: document.getElementById("session-prev-btn"),
  sessionDatePicker: document.getElementById("session-date-picker"),
  sessionDateLabel: document.getElementById("session-date-label"),
  sessionNextBtn: document.getElementById("session-next-btn"),
  sessionTodayBtn: document.getElementById("session-today-btn"),
  sessionModeBadge: document.getElementById("session-mode-badge"),
  sessionNoData: document.getElementById("session-no-data"),
  historicalAtmPanel: document.getElementById("historical-atm-panel"),
  historicalAtmBody: document.getElementById("historical-atm-body"),
  historicalStrikeSelect: document.getElementById("historical-strike-select"),
  appRoot: document.querySelector(".app"),
};

// Shown wherever an expiry field has nothing valid to display — never
// silently fall back to a stale/placeholder date. See fetchOptionChain-side
// (chain.expiry) producers: mock/replay auto-compute a live expiry
// (src/option_chain/expiry.py), Dhan resolves the real one, and NSE passes
// through whatever the exchange returns. An empty string here means none of
// those succeeded (e.g. Dhan expiry fetch failed) — not "no update yet".
const EXPIRY_UNAVAILABLE = "Expiry unavailable";

function fmtExpiry(expiry) {
  return expiry || EXPIRY_UNAVAILABLE;
}

function fmt(value, digits = 2) {
  if (value == null || Number.isNaN(value)) return "—";
  return Number(value).toLocaleString("en-IN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

// Every timestamp from the backend is an IST-offset ISO string (see
// src/timeutil.py / serializers.py::_dt). Pinning timeZone here too means
// display is always Asia/Kolkata regardless of the browser's own locale or
// system timezone — never "server/browser local time".
const IST_TIMEZONE = "Asia/Kolkata";

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleTimeString("en-IN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    timeZone: IST_TIMEZONE,
  });
}

function fmtDate(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleDateString("en-IN", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    timeZone: IST_TIMEZONE,
  });
}

function fmtSigned(value, digits = 2) {
  if (value == null || Number.isNaN(value)) return "—";
  const sign = value > 0 ? "+" : "";
  return `${sign}${fmt(value, digits)}`;
}

function pnlClass(value) {
  if (value > 0) return "pnl-pos";
  if (value < 0) return "pnl-neg";
  return "pnl-flat";
}

function reasonBadge(reason) {
  if (!reason) return "—";
  const map = { TAKE_PROFIT: "reason-tp", STOP_LOSS: "reason-sl", MANUAL: "reason-manual" };
  return `<span class="badge-reason ${map[reason] || "reason-manual"}">${reason.replace("_", " ")}</span>`;
}

function sideBadge(side) {
  const s = (side || "BUY").toUpperCase();
  return `<span class="badge-side ${s === "SELL" ? "side-sell" : "side-buy"}">${s}</span>`;
}

function showToast(message) {
  els.toast.textContent = message;
  els.toast.classList.remove("hidden");
  clearTimeout(showToast._timer);
  showToast._timer = setTimeout(() => els.toast.classList.add("hidden"), 3200);
}

function setConnectionStatus(statusText) {
  if (!statusText || typeof statusText !== "string") return;
  els.connStatus.textContent = statusText;
  if (statusText === "LIVE") {
    els.connStatus.className = "badge badge-live";
  } else if (statusText === "SIMULATED") {
    els.connStatus.className = "badge badge-simulated";
  } else if (statusText.includes("CONNECTING")) {
    els.connStatus.className = "badge badge-connecting";
  } else {
    els.connStatus.className = "badge badge-offline";
  }
}

const SESSION_LABELS = {
  OPEN: "Session Open",
  PRE_OPEN: "Pre-Open",
  CLOSED: "Session Closed",
  WEEKEND: "Weekend",
  HOLIDAY: "Market Holiday",
};

// ---------------------------------------------------------------------------
// Tab navigation
// ---------------------------------------------------------------------------

function bindMainTabs() {
  els.mainTabs.addEventListener("click", (event) => {
    const btn = event.target.closest(".main-tab");
    if (!btn) return;
    const view = btn.dataset.view;
    state.activeView = view;

    els.mainTabs.querySelectorAll(".main-tab").forEach((tab) => tab.classList.toggle("active", tab === btn));
    document.querySelectorAll(".view").forEach((section) => {
      section.classList.toggle("hidden", section.id !== `view-${view}`);
    });

    if (view === "logs") {
      fetchLogs();
    } else if (view === "settings") {
      loadSettings();
    } else if (view === "overview" && state.chart) {
      // Chart can render at 0 width if it was hidden on first paint.
      requestAnimationFrame(resizeChart);
      requestAnimationFrame(resizeStraddleChart);
    }
  });
}

// ---------------------------------------------------------------------------
// Chart
// ---------------------------------------------------------------------------

function resizeChart() {
  const container = document.getElementById("chart");
  if (!state.chart || !container.clientWidth) return;
  state.chart.applyOptions({ width: container.clientWidth, height: container.clientHeight });
}

function initChart() {
  const container = document.getElementById("chart");
  state.chart = LightweightCharts.createChart(container, {
    layout: {
      background: { color: "#151c26" },
      textColor: "#8b9bb0",
      fontFamily: "IBM Plex Mono, monospace",
    },
    grid: {
      vertLines: { color: "rgba(36, 48, 65, 0.65)" },
      horzLines: { color: "rgba(36, 48, 65, 0.65)" },
    },
    rightPriceScale: { borderColor: "#243041" },
    timeScale: {
      borderColor: "#243041",
      timeVisible: true,
      secondsVisible: true,
    },
    handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: true },
    handleScale: { axisPressedMouseMove: true, mouseWheel: true, pinch: true },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
  });

  state.candleSeries = state.chart.addCandlestickSeries({
    upColor: "#22c55e",
    downColor: "#ef4444",
    borderUpColor: "#22c55e",
    borderDownColor: "#ef4444",
    wickUpColor: "#22c55e",
    wickDownColor: "#ef4444",
  });

  window.addEventListener("resize", resizeChart);
  resizeChart();

  // Crosshair OHLC tooltip.
  const chartWrap = container.parentElement;
  state.chart.subscribeCrosshairMove((param) => {
    if (!param.point || !param.time || !param.seriesData || !param.seriesData.has(state.candleSeries)) {
      els.chartTooltip.classList.add("hidden");
      return;
    }
    const bar = param.seriesData.get(state.candleSeries);
    if (!bar) {
      els.chartTooltip.classList.add("hidden");
      return;
    }
    const dir = bar.close >= bar.open ? "up" : "down";
    els.chartTooltip.innerHTML =
      `<span class="tt-o">O <b>${fmt(bar.open)}</b></span>` +
      `<span class="tt-h">H <b>${fmt(bar.high)}</b></span>` +
      `<span class="tt-l">L <b>${fmt(bar.low)}</b></span>` +
      `<span class="tt-c tt-${dir}">C <b>${fmt(bar.close)}</b></span>`;
    els.chartTooltip.classList.remove("hidden");

    const wrapRect = chartWrap.getBoundingClientRect();
    let left = param.point.x + 16;
    let top = param.point.y - 12;
    if (left + 220 > wrapRect.width) left = param.point.x - 220;
    if (top < 0) top = 0;
    els.chartTooltip.style.left = `${left}px`;
    els.chartTooltip.style.top = `${top}px`;
  });
}

function updateSpot(price, timestamp) {
  if (price == null) return;

  els.spotPrice.textContent = fmt(price);
  els.moLtp.textContent = fmt(price);

  if (state.lastPrice != null) {
    const diff = price - state.lastPrice;
    const cls = diff > 0 ? "up" : diff < 0 ? "down" : "neutral";
    const sign = diff > 0 ? "+" : "";
    els.spotChange.textContent = `${sign}${fmt(diff)}`;
    els.spotChange.className = `spot-change ${cls}`;
    els.moChange.textContent = `${sign}${fmt(diff)}`;
    els.moChange.className = `spot-change ${cls}`;
  }

  state.lastPrice = price;
  els.lastUpdated.textContent = `Updated ${fmtTime(timestamp)}`;
}

function fmtDelta(value) {
  if (value == null || Number.isNaN(value)) return "—";
  const sign = value > 0 ? "+" : "";
  return `${sign}${Number(value).toFixed(2)}`;
}

function updateATM(atm) {
  if (!atm) {
    els.atmStrike.textContent = "—";
    els.atmCall.textContent = "—";
    els.atmPut.textContent = "—";
    els.atmStraddle.textContent = "—";
    els.atmCallDelta.textContent = "—";
    els.atmPutDelta.textContent = "—";
    els.atmCompare.classList.add("hidden");
    return;
  }

  els.atmStrike.textContent = fmt(atm.strike, 0);
  els.atmCall.textContent = fmt(atm.call_ltp);
  els.atmPut.textContent = fmt(atm.put_ltp);
  els.atmStraddle.textContent = fmt(atm.combined_value ?? atm.straddle_premium);
  els.atmCallDelta.textContent = fmtDelta(atm.call_delta);
  els.atmPutDelta.textContent = fmtDelta(atm.put_delta);

  els.atmMethodChip.textContent =
    atm.method === "delta" ? `Δ ${fmt(atm.delta_threshold ?? 0.5, 2)}` : "Price-based";

  const showCompare =
    atm.method === "delta" && atm.price_based_strike != null && atm.price_based_strike !== atm.strike;
  els.atmCompare.classList.toggle("hidden", !showCompare);
  if (showCompare) {
    els.atmPriceStrike.textContent = fmt(atm.price_based_strike, 0);
  }
}

// Key into state.multiSelected — one entry per (strike, CE/PE) contract.
function multiSelectKey(strike, optionType) {
  return `${strike}_${optionType}`;
}

function updateOptionChain(chain, atm) {
  if (!chain || !chain.strikes?.length) {
    els.optionChainBody.innerHTML = `<tr><td colspan="8" class="empty">No chain data</td></tr>`;
    els.chainExpiry.textContent = EXPIRY_UNAVAILABLE;
    return;
  }

  els.chainExpiry.textContent = fmtExpiry(chain.expiry);

  const atmStrike = chain.strikes.find((s) => s.is_atm)?.strike;
  const priceStrike = atm?.price_based_strike;
  const sorted = [...chain.strikes].sort((a, b) => a.strike - b.strike);

  let rows = sorted;
  if (atmStrike != null) {
    const idx = sorted.findIndex((s) => s.strike === atmStrike);
    const start = Math.max(0, idx - 4);
    const end = Math.min(sorted.length, idx + 5);
    rows = sorted.slice(start, end);
  }

  els.optionChainBody.innerHTML = rows
    .map((row) => {
      const isPriceComparison = priceStrike != null && row.strike === priceStrike && !row.is_atm;
      const cls = [row.is_atm ? "atm-row" : "", isPriceComparison ? "price-atm-row" : ""].filter(Boolean).join(" ");
      // The "Sel" checkboxes are the ONE selection mechanism for Paper
      // Trading (and, through it, the Straddle Chart) — see
      // state.multiSelected. Clicking the CE/PE price itself is just a
      // shortcut that toggles the same checkbox (bindMultiSelect()).
      const isMultiCall = state.multiSelected.has(multiSelectKey(row.strike, "CE"));
      const isMultiPut = state.multiSelected.has(multiSelectKey(row.strike, "PE"));
      return `
      <tr class="${cls}" data-strike="${row.strike}">
        <td class="chk-col">
          <input type="checkbox" class="multi-select-checkbox" data-strike="${row.strike}" data-option-type="CE" ${isMultiCall ? "checked" : ""} aria-label="Select ${fmt(row.strike, 0)} CE for Paper Trading" />
        </td>
        <td class="call-cell${isMultiCall ? " multi-selected-cell" : ""}" data-option-type="CE">${fmt(row.call_ltp)}</td>
        <td class="delta-cell">${fmtDelta(row.call_delta)}</td>
        <td>${fmt(row.strike, 0)}</td>
        <td class="delta-cell">${fmtDelta(row.put_delta)}</td>
        <td class="put-cell${isMultiPut ? " multi-selected-cell" : ""}" data-option-type="PE">${fmt(row.put_ltp)}</td>
        <td class="chk-col">
          <input type="checkbox" class="multi-select-checkbox" data-strike="${row.strike}" data-option-type="PE" ${isMultiPut ? "checked" : ""} aria-label="Select ${fmt(row.strike, 0)} PE for Paper Trading" />
        </td>
        <td class="combined-cell">${fmt(row.combined_value)}</td>
      </tr>`;
    })
    .join("");
}

function updateCandlesPanel(snapshot) {
  const bucket = snapshot.candles?.[state.selectedInterval];
  els.candleIntervalLabel.textContent = `${state.selectedInterval}m`;

  if (!bucket) {
    els.candlesBody.innerHTML = `<tr><td colspan="5" class="empty">No candles yet</td></tr>`;
    els.activeCandle.textContent = "Building candle: —";
    return;
  }

  const rows = [...(bucket.completed || [])].slice(-8).reverse();
  if (rows.length === 0) {
    els.candlesBody.innerHTML = `<tr><td colspan="5" class="empty">No closed candles yet</td></tr>`;
  } else {
    els.candlesBody.innerHTML = rows
      .map(
        (c) => `
        <tr>
          <td>${fmtTime(c.open_time)}</td>
          <td>${fmt(c.open)}</td>
          <td>${fmt(c.high)}</td>
          <td>${fmt(c.low)}</td>
          <td>${fmt(c.close)}</td>
        </tr>`
      )
      .join("");
  }

  const active = bucket.active;
  if (active) {
    els.activeCandle.textContent =
      `Building ${state.selectedInterval}m candle · O ${fmt(active.open)} · H ${fmt(active.high)} · L ${fmt(active.low)} · C ${fmt(active.close)} · ticks ${active.tick_count}`;
  } else {
    els.activeCandle.textContent = "Building candle: —";
  }
}

function updateChart(snapshot) {
  const bucket = snapshot.candles?.[state.selectedInterval];
  if (!bucket || !state.candleSeries) return;

  const series = (bucket.series || []).map((c) => ({
    time: c.time,
    open: c.open,
    high: c.high,
    low: c.low,
    close: c.close,
  }));

  if (series.length === 0) return;

  state.candleSeries.setData(series);
  state.chart.timeScale().scrollToRealTime();
}

// ---------------------------------------------------------------------------
// ATM Straddle chart — REAL candlesticks (CE / PE / Straddle), any strike
// picked from the Option Chain, 1m/5m/15m, live from MOCK ticks.
// ---------------------------------------------------------------------------

const STRADDLE_VIEW_LABEL = {
  straddle: "Straddle = CE LTP + PE LTP",
  ce: "Call (CE) LTP",
  pe: "Put (PE) LTP",
};

// Distinct strikes currently in state.multiSelected (the ONE selection
// state — see its declaration), ascending. A strike counts once even if
// both its CE and PE are selected (same strike = one Straddle Chart focus).
function getDistinctSelectedStrikes() {
  const strikes = [...new Set([...state.multiSelected.values()].map((e) => e.strike))];
  strikes.sort((a, b) => a - b);
  return strikes;
}

// Which strike the Straddle Chart shows, derived entirely from
// state.multiSelected (no separate selected-contract state):
//   0 selected  -> follow the live ATM strike, same as before this existed.
//   1 selected  -> that strike, automatically (kept "clean and compact").
//   2+ selected -> state.straddleFocusStrike if it's still one of them,
//                  else the lowest selected strike; the picker lets the
//                  user change it (see renderStraddleFocusPicker()).
function resolveStraddleFocusStrike(snapshot) {
  const strikes = getDistinctSelectedStrikes();
  if (strikes.length === 0) return snapshot?.atm?.strike ?? null;
  if (strikes.length === 1) return strikes[0];
  if (state.straddleFocusStrike != null && strikes.includes(state.straddleFocusStrike)) {
    return state.straddleFocusStrike;
  }
  return strikes[0];
}

// Informational chip (0 or 1 selected) vs. the picker <select> (2+ selected)
// — exactly one of the two is visible at a time.
function renderStraddleFocusPicker(snapshot) {
  const strikes = getDistinctSelectedStrikes();
  const showPicker = strikes.length > 1;

  els.straddleModeChip.classList.toggle("hidden", showPicker);
  els.straddleFocusSelect.classList.toggle("hidden", !showPicker);

  if (!showPicker) {
    els.straddleModeChip.textContent = strikes.length === 1 ? "📌 Tracking selected strike" : "● Following ATM";
    els.straddleFocusSelect.innerHTML = ""; // no stale options left behind once hidden
    return;
  }

  const focus = resolveStraddleFocusStrike(snapshot);
  els.straddleFocusSelect.innerHTML = strikes
    .map((s) => `<option value="${s}"${s === focus ? " selected" : ""}>${fmt(s, 0)}</option>`)
    .join("");
}

function resizeStraddleChart() {
  const container = document.getElementById("straddle-chart");
  if (!state.straddleChart || !container.clientWidth) return;
  state.straddleChart.applyOptions({ width: container.clientWidth, height: container.clientHeight });
}

function initStraddleChart() {
  const container = document.getElementById("straddle-chart");
  state.straddleChart = LightweightCharts.createChart(container, {
    layout: {
      background: { color: "#151c26" },
      textColor: "#8b9bb0",
      fontFamily: "IBM Plex Mono, monospace",
    },
    grid: {
      vertLines: { color: "rgba(36, 48, 65, 0.65)" },
      horzLines: { color: "rgba(36, 48, 65, 0.65)" },
    },
    rightPriceScale: { borderColor: "#243041" },
    timeScale: { borderColor: "#243041", timeVisible: true, secondsVisible: true },
    // Trading-chart interactions: zoom (wheel/pinch), pan (drag), crosshair.
    handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: true },
    handleScale: { axisPressedMouseMove: true, mouseWheel: true, pinch: true },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
  });

  // Real candlesticks — same series type/styling as the main chart.
  state.straddleSeries = state.straddleChart.addCandlestickSeries({
    upColor: "#22c55e",
    downColor: "#ef4444",
    borderUpColor: "#22c55e",
    borderDownColor: "#ef4444",
    wickUpColor: "#22c55e",
    wickDownColor: "#ef4444",
  });

  window.addEventListener("resize", resizeStraddleChart);
  resizeStraddleChart();

  // Crosshair OHLC tooltip — reads straight off the series' own bar data,
  // same pattern as the main candlestick chart's tooltip.
  const chartWrap = container.parentElement;
  state.straddleChart.subscribeCrosshairMove((param) => {
    if (!param.point || !param.time || !param.seriesData || !param.seriesData.has(state.straddleSeries)) {
      els.straddleChartTooltip.classList.add("hidden");
      return;
    }
    const bar = param.seriesData.get(state.straddleSeries);
    if (!bar) {
      els.straddleChartTooltip.classList.add("hidden");
      return;
    }
    const dir = bar.close >= bar.open ? "up" : "down";
    const viewLabel = { straddle: "Straddle", ce: "CE", pe: "PE" }[state.straddleView];
    els.straddleChartTooltip.innerHTML =
      `<span>Strike <b>${fmt(state.straddleLastFetchedStrike, 0)}</b></span>` +
      `<span>${viewLabel}</span>` +
      `<span class="tt-o">O <b>${fmt(bar.open)}</b></span>` +
      `<span class="tt-h">H <b>${fmt(bar.high)}</b></span>` +
      `<span class="tt-l">L <b>${fmt(bar.low)}</b></span>` +
      `<span class="tt-c tt-${dir}">C <b>${fmt(bar.close)}</b></span>`;
    els.straddleChartTooltip.classList.remove("hidden");

    const wrapRect = chartWrap.getBoundingClientRect();
    let left = param.point.x + 16;
    let top = param.point.y - 12;
    if (left + 320 > wrapRect.width) left = param.point.x - 320;
    if (top < 0) top = 0;
    els.straddleChartTooltip.style.left = `${left}px`;
    els.straddleChartTooltip.style.top = `${top}px`;
  });
}

// Renders whatever's cached in state.straddleCandleData for the currently
// selected interval + view — no network call, so interval/view toggles are
// instant (the REST response already carries all 3 intervals × 3 legs).
function renderStraddleChart() {
  if (!state.straddleSeries || !state.straddleCandleData) return;
  const bucket = state.straddleCandleData.by_interval?.[state.straddleInterval];
  const bars = bucket ? bucket[state.straddleView] : null;
  if (!bars || bars.length === 0) return;

  const points = bars.map((b) => ({ time: b.time, open: b.open, high: b.high, low: b.low, close: b.close }));
  state.straddleSeries.setData(points);
  // fitContent(), not scrollToRealTime() — spaces all bars to fill the full
  // chart width instead of bunching them at whatever the default bar
  // spacing leaves at the right edge.
  state.straddleChart.timeScale().fitContent();
}

// Pulls the full OHLC history (all intervals, all legs) for one strike.
// Called on strike switch and on every live tick, so candles keep updating
// continuously from MOCK ticks without duplicating the server's session-
// aligned bucketing logic client-side.
async function refreshStraddleCandles(strike) {
  if (strike == null || state.straddleFetchInFlight) return;
  state.straddleFetchInFlight = true;
  try {
    const res = await fetch(`/api/straddle-candles?strike=${encodeURIComponent(strike)}`);
    if (!res.ok) return; // e.g. a brand-new strike the engine hasn't ticked yet
    state.straddleCandleData = await res.json();
    renderStraddleChart();
  } catch (err) {
    console.error("Failed to load straddle candles", err);
  } finally {
    state.straddleFetchInFlight = false;
  }
}

function updateStraddleChart(snapshot) {
  if (!state.straddleSeries) return;
  renderStraddleFocusPicker(snapshot); // chip or picker, derived from the SAME selection state as Paper Trading
  const strike = resolveStraddleFocusStrike(snapshot);
  els.straddleTrackedStrike.textContent = strike != null ? fmt(strike, 0) : "—";
  // Same expiry as the Option Chain / Selected Contracts / Paper Trading —
  // all read straight off this tick's snapshot.option_chain.expiry, so
  // there is exactly one place the expiry can come from.
  els.straddleExpiry.textContent = fmtExpiry(snapshot.option_chain?.expiry);
  if (strike == null) return;

  if (strike !== state.straddleLastFetchedStrike) {
    state.straddleLastValue = null; // reset the change baseline on strike switch
  }
  state.straddleLastFetchedStrike = strike;
  refreshStraddleCandles(strike);

  // Current value + change: the live leg price straight off this tick's
  // option chain (already streaming — no extra request for this part).
  const leg = snapshot.option_chain?.strikes?.find((s) => s.strike === strike);
  const view = state.straddleView;
  const rawValue = leg
    ? view === "straddle"
      ? leg.combined_value
      : view === "ce"
      ? leg.call_ltp
      : leg.put_ltp
    : null;

  els.straddleCurrentValue.textContent = fmt(rawValue);
  if (state.straddleLastValue != null && rawValue != null) {
    const diff = rawValue - state.straddleLastValue;
    const cls = diff > 0 ? "up" : diff < 0 ? "down" : "neutral";
    const sign = diff > 0 ? "+" : "";
    els.straddleCurrentChange.textContent = `${sign}${fmt(diff)}`;
    els.straddleCurrentChange.className = `spot-change ${cls}`;
  } else {
    els.straddleCurrentChange.textContent = "—";
    els.straddleCurrentChange.className = "spot-change neutral";
  }
  if (rawValue != null) state.straddleLastValue = rawValue;
}

function bindStraddleTabs() {
  els.straddleViewTabs.addEventListener("click", (event) => {
    const btn = event.target.closest(".tab");
    if (!btn) return;
    state.straddleView = btn.dataset.straddleView;
    state.straddleLastValue = null; // reset the change baseline on view switch
    els.straddleViewTabs.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === btn));
    els.straddleLegendLabel.textContent = STRADDLE_VIEW_LABEL[state.straddleView];
    renderStraddleChart(); // cached data already has this view — instant
  });

  els.straddleIntervalTabs.addEventListener("click", (event) => {
    const btn = event.target.closest(".tab");
    if (!btn) return;
    state.straddleInterval = btn.dataset.straddleInterval;
    els.straddleIntervalTabs.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === btn));
    renderStraddleChart(); // cached data already has this interval — instant
  });

  // Picker shown only when 2+ distinct strikes are selected (see
  // renderStraddleFocusPicker()) — lets the user choose which one the
  // Straddle Chart displays. Nothing to bind for the 0/1-selected chip: it's
  // purely informational, since with 0 or 1 selected the focus strike is
  // already fully determined by state.multiSelected.
  els.straddleFocusSelect.addEventListener("change", (event) => {
    if (!event.target.value) return; // empty/no options — nothing to switch to
    const strike = Number(event.target.value);
    if (Number.isNaN(strike)) return;
    state.straddleFocusStrike = strike;
    state.straddleLastValue = null; // reset the change baseline on strike switch
    if (state.latestSnapshot) updateStraddleChart(state.latestSnapshot);
  });
}

// ---------------------------------------------------------------------------
// Paper trading
// ---------------------------------------------------------------------------

function showPtError(message) {
  els.ptError.textContent = message;
  els.ptError.classList.remove("hidden");
  clearTimeout(showPtError._timer);
  showPtError._timer = setTimeout(() => els.ptError.classList.add("hidden"), 4000);
}

function renderPaperConfig(cfg) {
  if (!cfg || state.configDirty) return;
  els.ptTp.value = cfg.take_profit_percent;
  els.ptSl.value = cfg.stop_loss_percent;
  // ₹ targets are optional/additional — leave the input blank (not "0")
  // when unset, so submitting the form without touching them keeps them
  // disabled rather than accidentally setting a 0 target.
  els.ptTpAmount.value = cfg.take_profit_amount ?? "";
  els.ptSlAmount.value = cfg.stop_loss_amount ?? "";
}

function renderOpenPositions(positions) {
  if (!positions || positions.length === 0) {
    els.positionsBody.innerHTML = `<tr><td colspan="17" class="empty">No open positions</td></tr>`;
    return;
  }

  els.positionsBody.innerHTML = positions
    .map(
      (p) => `
      <tr>
        <td class="${p.option_type === "CE" ? "call-cell" : "put-cell"}">${p.option_type}</td>
        <td>${sideBadge(p.side)}</td>
        <td>${fmt(p.strike, 0)}</td>
        <td>${p.quantity}</td>
        <td>${fmt(p.entry_price)}</td>
        <td>${fmt(p.current_price)}</td>
        <td>${fmt(p.position_value)}</td>
        <td>${fmt(p.take_profit_price)}</td>
        <td>${fmt(p.stop_loss_price)}</td>
        <td>${p.take_profit_amount != null ? fmt(p.take_profit_amount, 0) : "—"}</td>
        <td>${p.stop_loss_amount != null ? fmt(p.stop_loss_amount, 0) : "—"}</td>
        <td class="${p.distance_to_tp_percent <= 0 ? "pnl-pos" : ""}">${fmt(p.distance_to_tp_percent)}%</td>
        <td class="${p.distance_to_sl_percent <= 0 ? "pnl-neg" : ""}">${fmt(p.distance_to_sl_percent)}%</td>
        <td class="${pnlClass(p.pnl)}">${fmtSigned(p.pnl)}</td>
        <td class="${pnlClass(p.pnl_percent)}">${fmtSigned(p.pnl_percent)}%</td>
        <td>${fmtTime(p.entry_time)}</td>
        <td><button type="button" class="btn-close" data-close-id="${p.id}">Exit</button></td>
      </tr>`
    )
    .join("");
}

function renderHistory(history) {
  if (!history || history.length === 0) {
    els.historyBody.innerHTML = `<tr><td colspan="10" class="empty">No trade history yet</td></tr>`;
    return;
  }

  els.historyBody.innerHTML = history
    .map(
      (p) => `
      <tr>
        <td class="${p.option_type === "CE" ? "call-cell" : "put-cell"}">${p.option_type}</td>
        <td>${sideBadge(p.side)}</td>
        <td>${fmt(p.strike, 0)}</td>
        <td>${p.quantity}</td>
        <td>${fmt(p.entry_price)}</td>
        <td>${fmt(p.exit_price)}</td>
        <td class="${pnlClass(p.pnl)}">${fmtSigned(p.pnl)}</td>
        <td class="${pnlClass(p.pnl_percent)}">${fmtSigned(p.pnl_percent)}%</td>
        <td>${reasonBadge(p.exit_reason)}</td>
        <td>${fmtTime(p.exit_time)}</td>
      </tr>`
    )
    .join("");
}

function renderTradeStats(stats) {
  if (!stats) return;
  document.getElementById("stat-winrate").textContent = `${fmt(stats.win_rate_percent, 1)}%`;
  const totalEl = document.getElementById("stat-totalpnl");
  totalEl.textContent = fmtSigned(stats.total_pnl);
  totalEl.className = `stat-value ${pnlClass(stats.total_pnl)}`;
  document.getElementById("stat-avgwin").textContent = fmtSigned(stats.average_win);
  document.getElementById("stat-avgloss").textContent = fmtSigned(stats.average_loss);
  document.getElementById("stat-drawdown").textContent = fmt(stats.max_drawdown);
  document.getElementById("stat-trades").textContent = `${stats.total_trades} (${stats.wins}W / ${stats.losses}L)`;
}

// Portfolio-wide P&L (realized + unrealized) and turnover — see
// PaperTradingEngine._pnl_summary(). Distinct from renderTradeStats(), which
// covers only *closed*-trade performance (win rate, avg win/loss, drawdown).
function renderPnlSummary(summary, openPositions) {
  if (!summary) return;
  // Open Positions count + Total Invested (capital committed at entry for
  // currently-open positions) — computed client-side from open_positions,
  // which the backend already returns per-position; no backend change
  // needed for these two totals. Distinct from "Total Turnover", which is
  // cumulative buy+sell value across open AND closed positions this session.
  const openCount = summary.open_position_count ?? (openPositions ? openPositions.length : 0);
  const totalInvested = (openPositions || []).reduce((sum, p) => sum + p.entry_price * p.quantity, 0);
  els.pnlsOpenCount.textContent = String(openCount);
  els.pnlsInvested.textContent = fmt(totalInvested);
  els.pnlsUnrealized.textContent = fmtSigned(summary.unrealized_pnl);
  els.pnlsUnrealized.className = `stat-value ${pnlClass(summary.unrealized_pnl)}`;
  els.pnlsRealized.textContent = fmtSigned(summary.realized_pnl);
  els.pnlsRealized.className = `stat-value ${pnlClass(summary.realized_pnl)}`;
  els.pnlsTotal.textContent = fmtSigned(summary.total_pnl);
  els.pnlsTotal.className = `stat-value ${pnlClass(summary.total_pnl)}`;
  els.pnlsTotalPercent.textContent = `${fmtSigned(summary.total_pnl_percent)}%`;
  els.pnlsTotalPercent.className = `stat-value ${pnlClass(summary.total_pnl_percent)}`;
  // Turnover (capital deployed by side) — never colored as profit/loss.
  els.pnlsBuyTurnover.textContent = fmt(summary.buy_turnover);
  els.pnlsSellTurnover.textContent = fmt(summary.sell_turnover);
  els.pnlsTotalTurnover.textContent = fmt(summary.total_turnover);
  els.pnlsPositionValue.textContent = fmt(summary.total_position_value);
}

function updatePaperTrading(pt) {
  if (!pt) return;

  renderPaperConfig(pt.config);
  renderOpenPositions(pt.open_positions);
  renderHistory(pt.history);
  renderTradeStats(pt.stats);
  renderPnlSummary(pt.pnl_summary, pt.open_positions);

  const isFirstLoad = !state.paperInitialized;
  for (const trade of pt.history) {
    if (state.knownHistoryIds.has(trade.id)) continue;
    state.knownHistoryIds.add(trade.id);
    if (!isFirstLoad && (trade.exit_reason === "TAKE_PROFIT" || trade.exit_reason === "STOP_LOSS")) {
      const label = trade.exit_reason === "TAKE_PROFIT" ? "🟢 TAKE PROFIT" : "🔴 STOP LOSS";
      showToast(
        `${label} · ${trade.option_type} ${fmt(trade.strike, 0)} · P&L ${fmtSigned(trade.pnl)} (${fmtSigned(trade.pnl_percent)}%)`
      );
    }
  }
  state.paperInitialized = true;
}

// ---------------------------------------------------------------------------
// Multi-Strike Selection — THE single contract-selection system for Paper
// Trading (and, through resolveStraddleFocusStrike() above, the Straddle
// Chart). Check CE/PE boxes in the Option Chain, adjust BUY/SELL + Qty
// independently per contract here, then Execute (one row) or Execute
// Selected (all pending rows). 1 entry = a single-strike trade; 2+ = multi-
// strike — both are the same state and the same execute path: each becomes
// its own POST /api/paper/open call, so PaperTradingEngine already tracks
// each as a fully independent position (own entry price, own P&L, own
// TP/SL/auto-exit) — no backend change was needed for any of this.
// ---------------------------------------------------------------------------

function renderMultiSelectPanel() {
  const entries = [...state.multiSelected.values()];
  const count = entries.length;

  els.multiSelectCount.textContent = `${count} selected`;
  els.multiSelectCount.classList.toggle("hidden", count === 0);
  els.multiExecuteBtn.disabled = count === 0;
  els.multiSelectEmpty.classList.toggle("hidden", count > 0);
  els.multiSelectTableWrap.classList.toggle("hidden", count === 0);

  if (count === 0) {
    els.multiSelectBody.innerHTML = "";
    return;
  }

  const chain = state.latestSnapshot?.option_chain;
  const strikes = chain?.strikes || [];
  const expiry = fmtExpiry(chain?.expiry);
  entries.sort((a, b) => a.strike - b.strike || a.optionType.localeCompare(b.optionType));

  els.multiSelectBody.innerHTML = entries
    .map((entry) => {
      const key = multiSelectKey(entry.strike, entry.optionType);
      const leg = strikes.find((s) => s.strike === entry.strike);
      const isCall = entry.optionType === "CE";
      const ltp = leg ? (isCall ? leg.call_ltp : leg.put_ltp) : null;
      const delta = leg ? (isCall ? leg.call_delta : leg.put_delta) : null;
      return `
      <tr data-key="${key}">
        <td>${fmt(entry.strike, 0)}</td>
        <td class="${isCall ? "call-cell" : "put-cell"}">${entry.optionType}</td>
        <td>${fmt(ltp)}</td>
        <td class="delta-cell">${fmtDelta(delta)}</td>
        <td>${expiry}</td>
        <td>
          <div class="side-toggle mini" data-key="${key}">
            <button type="button" class="side-btn side-buy${entry.side === "BUY" ? " active" : ""}" data-side="BUY">BUY</button>
            <button type="button" class="side-btn side-sell${entry.side === "SELL" ? " active" : ""}" data-side="SELL">SELL</button>
          </div>
        </td>
        <td><input type="number" class="multi-select-qty" min="1" value="${entry.quantity}" data-key="${key}" /></td>
        <td>
          <button type="button" class="btn-execute-row" data-execute-key="${key}">Execute</button>
          <button type="button" class="btn-close" data-remove-key="${key}">Remove</button>
        </td>
      </tr>`;
    })
    .join("");
}

// Adds/removes one contract from the batch and gives instant visual
// feedback on its Option Chain cell — without a full table re-render, which
// would rebuild every checkbox and could steal focus mid-click.
function toggleMultiSelect(checkbox) {
  const strike = Number(checkbox.dataset.strike);
  const optionType = checkbox.dataset.optionType;
  if (Number.isNaN(strike)) return;
  const key = multiSelectKey(strike, optionType);

  if (checkbox.checked) {
    // Capture the contract's identity from the chain it was selected in, so
    // the backend can refuse it if that chain has since moved on (e.g. an
    // expiry rollover) instead of filling a different contract.
    const chain = state.latestSnapshot?.option_chain;
    state.multiSelected.set(key, {
      strike,
      optionType,
      side: "BUY",
      quantity: Number(els.defaultQty.value) || 50,
      expiry: chain?.expiry ?? null,
      underlying: chain?.underlying ?? null,
    });
  } else {
    state.multiSelected.delete(key);
  }

  const row = checkbox.closest("tr[data-strike]");
  const cell = row?.querySelector(optionType === "CE" ? ".call-cell" : ".put-cell");
  if (cell) cell.classList.toggle("multi-selected-cell", checkbox.checked);

  renderMultiSelectPanel();
  // The Straddle Chart's tracked strike is derived from this same selection
  // (see resolveStraddleFocusStrike()) — refresh it immediately rather than
  // waiting for the next MOCK tick's websocket broadcast, same reasoning as
  // renderMultiSelectPanel() above.
  if (state.latestSnapshot) updateStraddleChart(state.latestSnapshot);
}

// Opens exactly one simulated position from one multi-select entry. Shared
// by both "Execute" (one row) and "Execute Selected" (all rows) below —
// there is only one execute path, whether it's called once or N times.
function postOpenPosition(entry) {
  return fetch("/api/paper/open", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      option_type: entry.optionType,
      strike: entry.strike,
      side: entry.side,
      quantity: entry.quantity,
      expiry: entry.expiry ?? null,
      underlying: entry.underlying ?? null,
    }),
  }).then(async (res) => {
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `Failed to open ${entry.optionType} ${entry.strike}`);
    }
    return entry;
  });
}

// Refreshes everything that reflects the selection/position state after an
// execute: the Option Chain (checkboxes/highlights), the pending-selection
// panel, and (immediately, rather than waiting for the next MOCK tick's
// websocket broadcast) the Paper Trading tables themselves.
function refreshAfterExecute() {
  if (state.latestSnapshot) {
    updateOptionChain(state.latestSnapshot.option_chain, state.latestSnapshot.atm);
    updateStraddleChart(state.latestSnapshot); // selection just shrank (or emptied) — refresh instantly
  }
  renderMultiSelectPanel();
  fetch("/api/paper/state").then((r) => r.json()).then(updatePaperTrading).catch(() => {});
}

// Executes a single pending contract (the row's "Execute" button) — a
// single-strike paper trade. Leaves every other pending row untouched.
async function executeOneEntry(key) {
  const entry = state.multiSelected.get(key);
  if (!entry) return;

  const btn = els.multiSelectBody.querySelector(`[data-execute-key="${key}"]`);
  if (btn) btn.disabled = true;

  try {
    await postOpenPosition(entry);
    state.multiSelected.delete(key);
    showToast(`Executed ${entry.side} ${entry.optionType} ${fmt(entry.strike, 0)}`);
  } catch (err) {
    showPtError(err.message);
    if (btn) btn.disabled = false;
    return;
  }
  refreshAfterExecute();
}

// Executes every pending contract at once ("Execute Selected") — 1 entry is
// a single-strike trade, 2+ is multi-strike; this is the same call either way.
async function executeMultiSelect() {
  const entries = [...state.multiSelected.values()];
  if (entries.length === 0) return;

  els.multiExecuteBtn.disabled = true;
  const originalLabel = els.multiExecuteBtn.textContent;
  els.multiExecuteBtn.textContent = "Executing…";

  const results = await Promise.allSettled(entries.map(postOpenPosition));

  const succeeded = results.filter((r) => r.status === "fulfilled").length;
  const failed = results.length - succeeded;

  // Clear the whole batch regardless of outcome — each is now either a real
  // independent position (succeeded) or the user can re-check it to retry
  // (failed); leaving stale entries mixed with fresh ones invites confusion.
  state.multiSelected.clear();
  els.multiExecuteBtn.textContent = originalLabel;

  if (failed === 0) {
    showToast(`Executed ${succeeded} paper position${succeeded === 1 ? "" : "s"} — each independent`);
  } else {
    const firstError = results.find((r) => r.status === "rejected")?.reason?.message;
    showPtError(`${succeeded} executed, ${failed} failed${firstError ? `: ${firstError}` : ""}`);
  }

  refreshAfterExecute();
}

function bindMultiSelect() {
  els.optionChainBody.addEventListener("change", (event) => {
    const checkbox = event.target.closest(".multi-select-checkbox");
    if (!checkbox) return;
    toggleMultiSelect(checkbox);
  });

  // Clicking a CE/PE price is a shortcut for checking that same contract's
  // checkbox — no manual strike input, and no second selection mechanism:
  // it goes through the exact same toggleMultiSelect() as the checkbox.
  els.optionChainBody.addEventListener("click", (event) => {
    if (event.target.closest(".multi-select-checkbox")) return; // checkbox's own change event handles it
    const cell = event.target.closest(".call-cell, .put-cell");
    if (!cell) return;
    const row = cell.closest("tr[data-strike]");
    const checkbox = row?.querySelector(`.multi-select-checkbox[data-option-type="${cell.dataset.optionType}"]`);
    if (!checkbox) return;
    checkbox.checked = !checkbox.checked;
    toggleMultiSelect(checkbox);
  });

  els.multiSelectClearBtn.addEventListener("click", () => {
    state.multiSelected.clear();
    if (state.latestSnapshot) {
      updateOptionChain(state.latestSnapshot.option_chain, state.latestSnapshot.atm);
      updateStraddleChart(state.latestSnapshot); // back to following ATM instantly
    }
    renderMultiSelectPanel();
  });

  els.multiSelectBody.addEventListener("click", (event) => {
    const sideBtn = event.target.closest(".side-btn");
    if (sideBtn) {
      const wrap = sideBtn.closest(".side-toggle");
      const key = wrap.dataset.key;
      const entry = state.multiSelected.get(key);
      if (!entry) return;
      entry.side = sideBtn.dataset.side;
      wrap.querySelectorAll(".side-btn").forEach((b) => b.classList.toggle("active", b === sideBtn));
      return;
    }
    const removeBtn = event.target.closest("[data-remove-key]");
    if (removeBtn) {
      const key = removeBtn.dataset.removeKey;
      state.multiSelected.delete(key);
      if (state.latestSnapshot) {
        updateOptionChain(state.latestSnapshot.option_chain, state.latestSnapshot.atm);
        updateStraddleChart(state.latestSnapshot);
      }
      renderMultiSelectPanel();
      return;
    }
    const executeBtn = event.target.closest("[data-execute-key]");
    if (executeBtn) {
      executeOneEntry(executeBtn.dataset.executeKey);
    }
  });

  els.multiSelectBody.addEventListener("input", (event) => {
    const qtyInput = event.target.closest(".multi-select-qty");
    if (!qtyInput) return;
    const entry = state.multiSelected.get(qtyInput.dataset.key);
    if (!entry) return;
    const qty = Number(qtyInput.value);
    entry.quantity = qty > 0 ? qty : entry.quantity;
  });

  els.multiExecuteBtn.addEventListener("click", executeMultiSelect);
}

function bindPaperTrading() {
  els.ptTp.addEventListener("input", () => {
    state.configDirty = true;
  });
  els.ptSl.addEventListener("input", () => {
    state.configDirty = true;
  });
  els.ptTpAmount.addEventListener("input", () => {
    state.configDirty = true;
  });
  els.ptSlAmount.addEventListener("input", () => {
    state.configDirty = true;
  });

  els.paperConfigForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      // ₹ targets are optional/additional — an empty field means "leave
      // that ₹ trigger disabled", so it's sent as null, not 0.
      const tpAmount = els.ptTpAmount.value.trim();
      const slAmount = els.ptSlAmount.value.trim();
      const res = await fetch("/api/paper/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          take_profit_percent: Number(els.ptTp.value),
          stop_loss_percent: Number(els.ptSl.value),
          take_profit_amount: tpAmount === "" ? null : Number(tpAmount),
          stop_loss_amount: slAmount === "" ? null : Number(slAmount),
        }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || "Failed to update TP/SL");
      }
      state.configDirty = false;
      showToast("TP/SL updated");
    } catch (err) {
      showPtError(err.message);
    }
  });

  els.positionsBody.addEventListener("click", async (event) => {
    const btn = event.target.closest("[data-close-id]");
    if (!btn) return;
    btn.disabled = true;
    try {
      const res = await fetch(`/api/paper/close/${btn.dataset.closeId}`, { method: "POST" });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || "Failed to close position");
      }
      showToast("Position closed (simulated)");
    } catch (err) {
      showPtError(err.message);
      btn.disabled = false;
    }
  });
}

// ---------------------------------------------------------------------------
// RRG tab
// ---------------------------------------------------------------------------

const QUADRANT_CLASS = {
  leading: "quad-leading",
  weakening: "quad-weakening",
  lagging: "quad-lagging",
  improving: "quad-improving",
};

function renderRRG(rrg) {
  const points = rrg?.data_points || [];
  if (points.length === 0) {
    els.rrgSvg.innerHTML = "";
    els.rrgBody.innerHTML = `<tr><td colspan="6" class="empty">RRG not yet available — waiting for enough ticks</td></tr>`;
    return;
  }

  // -- Table --
  const sorted = [...points].sort((a, b) => a.strike - b.strike || a.option_type.localeCompare(b.option_type));
  els.rrgBody.innerHTML = sorted
    .map(
      (p) => `
      <tr>
        <td>${fmt(p.strike, 0)}</td>
        <td class="${p.option_type === "CE" ? "call-cell" : "put-cell"}">${p.option_type}</td>
        <td>${fmt(p.rs)}</td>
        <td>${fmt(p.rs_ratio)}</td>
        <td>${fmt(p.rs_momentum)}</td>
        <td><span class="rrg-quad-tag ${QUADRANT_CLASS[p.quadrant] || ""}">${p.quadrant}</span></td>
      </tr>`
    )
    .join("");

  // -- Scatter plot (plain inline SVG, no external library) --
  const ratios = points.map((p) => p.rs_ratio);
  const momentums = points.map((p) => p.rs_momentum);
  const span = Math.max(
    10,
    Math.max(...ratios.map((v) => Math.abs(v - 100))),
    Math.max(...momentums.map((v) => Math.abs(v - 100))),
    1
  ) * 1.25;

  const size = 520;
  const margin = 40;
  const plotSize = size - margin * 2;
  const toX = (v) => margin + ((v - (100 - span)) / (span * 2)) * plotSize;
  const toY = (v) => margin + plotSize - ((v - (100 - span)) / (span * 2)) * plotSize; // invert y

  const cx = toX(100);
  const cy = toY(100);

  let svg = "";
  // Quadrant backgrounds
  svg += `<rect x="${cx}" y="${margin}" width="${margin + plotSize - cx}" height="${cy - margin}" class="rrg-quad-bg quad-leading-bg" />`;
  svg += `<rect x="${margin}" y="${margin}" width="${cx - margin}" height="${cy - margin}" class="rrg-quad-bg quad-improving-bg" />`;
  svg += `<rect x="${margin}" y="${cy}" width="${cx - margin}" height="${margin + plotSize - cy}" class="rrg-quad-bg quad-lagging-bg" />`;
  svg += `<rect x="${cx}" y="${cy}" width="${margin + plotSize - cx}" height="${margin + plotSize - cy}" class="rrg-quad-bg quad-weakening-bg" />`;

  // Axes
  svg += `<line x1="${margin}" y1="${cy}" x2="${margin + plotSize}" y2="${cy}" class="rrg-axis" />`;
  svg += `<line x1="${cx}" y1="${margin}" x2="${cx}" y2="${margin + plotSize}" class="rrg-axis" />`;

  // Labels
  svg += `<text x="${margin + plotSize}" y="${cy - 8}" class="rrg-axis-label" text-anchor="end">RS-Ratio →</text>`;
  svg += `<text x="${cx + 8}" y="${margin + 12}" class="rrg-axis-label">RS-Momentum ↑</text>`;

  // Points
  for (const p of points) {
    const x = toX(p.rs_ratio);
    const y = toY(p.rs_momentum);
    const cls = QUADRANT_CLASS[p.quadrant] || "";
    svg += `<circle cx="${x}" cy="${y}" r="7" class="rrg-point ${cls}"><title>${p.option_type} ${p.strike} · Ratio ${fmt(p.rs_ratio)} · Momentum ${fmt(p.rs_momentum)}</title></circle>`;
    svg += `<text x="${x}" y="${y - 10}" class="rrg-point-label">${fmt(p.strike, 0)}${p.option_type[0]}</text>`;
  }

  els.rrgSvg.innerHTML = svg;
}

// ---------------------------------------------------------------------------
// Data Quality tab
// ---------------------------------------------------------------------------

function estimateTickRate(tickCount) {
  const now = Date.now();
  state.tickHistory.push({ count: tickCount, at: now });
  // Keep last ~20s of samples.
  state.tickHistory = state.tickHistory.filter((s) => now - s.at <= 20000);
  if (state.tickHistory.length < 2) return null;
  const oldest = state.tickHistory[0];
  const elapsedSec = (now - oldest.at) / 1000;
  if (elapsedSec <= 0) return null;
  return (tickCount - oldest.count) / elapsedSec;
}

function updateDataQualityTab(dq, chain, tickCount) {
  if (!dq) return;

  els.dqConnection.textContent = els.connStatus.textContent;
  els.dqConnection.className = `dq-value ${els.connStatus.className.replace("badge", "dq-badge")}`;
  els.dqSource.textContent = dq.source || "—";
  els.dqTickCount.textContent = dq.tick_count ?? "—";

  const rate = estimateTickRate(tickCount);
  els.dqTickRate.textContent = rate == null ? "—" : `${rate.toFixed(2)} ticks/s`;

  els.dqLastTick.textContent =
    dq.last_tick_age_seconds == null
      ? "—"
      : dq.last_tick_age_seconds < 1
      ? "just now"
      : `${dq.last_tick_age_seconds.toFixed(1)}s ago`;

  els.dqStale.textContent = dq.is_stale ? `STALE (>${dq.stale_threshold_seconds}s)` : "Fresh";
  els.dqStale.className = `dq-value ${dq.is_stale ? "dq-bad" : "dq-good"}`;

  els.dqSession.textContent = SESSION_LABELS[dq.market_session] || dq.market_session || "—";

  const dup = dq.duplicate_tick_count || 0;
  const rejected = dq.rejected_tick_count || 0;
  els.dqRejects.textContent = `${dup} duplicate / ${rejected} rejected`;

  const hasChain = !!(chain && chain.strikes?.length);
  els.dqChainStatus.textContent = hasChain ? `OK (${chain.strikes.length} strikes)` : "MISSING";
  els.dqChainStatus.className = `dq-value ${hasChain ? "dq-good" : "dq-bad"}`;

  els.dqPersistence.textContent = dq.persistence_enabled ? "Saving (SQLite)" : "Off";
  els.dqPersistence.className = `dq-value ${dq.persistence_enabled ? "dq-good" : ""}`;
}

// ---------------------------------------------------------------------------
// System Logs tab
// ---------------------------------------------------------------------------

function briefLogDetail(entry) {
  const data = entry.data || {};
  const parts = Object.entries(data)
    .slice(0, 4)
    .map(([k, v]) => `${k}=${typeof v === "number" ? fmt(v) : v}`);
  return parts.join(" · ");
}

function renderLogs(logs) {
  if (!logs || logs.length === 0) {
    els.logsBody.innerHTML = `<tr><td colspan="4" class="empty">No log entries yet</td></tr>`;
    return;
  }
  els.logsBody.innerHTML = logs
    .map(
      (entry) => `
      <tr>
        <td>${fmtTime(entry.timestamp)}</td>
        <td><span class="log-level log-${(entry.level || "info").toLowerCase()}">${entry.level || "INFO"}</span></td>
        <td>${entry.event || entry.event_type}</td>
        <td class="log-detail">${briefLogDetail(entry)}</td>
      </tr>`
    )
    .join("");
}

async function fetchLogs() {
  const level = els.logLevelFilter.value;
  try {
    const url = `/api/logs?limit=200${level ? `&level=${level}` : ""}`;
    const res = await fetch(url);
    const data = await res.json();
    renderLogs(data.logs);
  } catch (err) {
    console.error("Failed to fetch logs", err);
  }
}

function bindLogs() {
  els.logLevelFilter.addEventListener("change", fetchLogs);
  state.logPollTimer = setInterval(() => {
    if (state.activeView === "logs" && els.logFollow.checked) fetchLogs();
  }, 2000);
}

// ---------------------------------------------------------------------------
// Settings tab
// ---------------------------------------------------------------------------

function showSettingsStatus(message, isError = false) {
  els.settingsStatus.textContent = message;
  els.settingsStatus.className = `settings-status ${isError ? "settings-error" : "settings-ok"}`;
  els.settingsStatus.classList.remove("hidden");
  clearTimeout(showSettingsStatus._timer);
  showSettingsStatus._timer = setTimeout(() => els.settingsStatus.classList.add("hidden"), 4000);
}

function applySettingsToForm(settings) {
  els.setDataSource.value = settings.data_source || "mock";
  els.setExpiry.textContent = fmtExpiry(settings.expiry);
  els.setAtmMethod.value = settings.atm_method || "delta";
  els.setDeltaThreshold.value = settings.delta_threshold ?? 0.5;
  els.setTp.value = settings.take_profit_percent ?? 10;
  els.setSl.value = settings.stop_loss_percent ?? 5;
  els.setCandleBasis.value = settings.candle_price_basis || "underlying";

  const sequences = settings.replay?.available_sequences || [];
  els.setReplaySequence.innerHTML = sequences
    .map((s) => `<option value="${s}">${s.replace(/_/g, " ")}</option>`)
    .join("");
  if (settings.replay?.sequence) els.setReplaySequence.value = settings.replay.sequence;
  if (settings.replay?.speed) els.setReplaySpeed.value = String(settings.replay.speed);

  toggleReplayFields();
}

function toggleReplayFields() {
  const isReplay = els.setDataSource.value === "replay";
  els.replaySequenceField.classList.toggle("hidden", !isReplay);
  els.replaySpeedField.classList.toggle("hidden", !isReplay);
}

async function loadSettings() {
  try {
    const res = await fetch("/api/settings");
    const settings = await res.json();
    applySettingsToForm(settings);
    state.settingsLoaded = true;
  } catch (err) {
    console.error("Failed to load settings", err);
  }
}

function bindSettings() {
  els.setDataSource.addEventListener("change", toggleReplayFields);

  els.settingsSourceForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const res = await fetch("/api/settings/data-source", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          data_source: els.setDataSource.value,
          sequence: els.setReplaySequence.value || undefined,
          speed: Number(els.setReplaySpeed.value) || undefined,
        }),
      });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || "Failed to apply data source");
      showSettingsStatus("Data source updated.");
    } catch (err) {
      showSettingsStatus(err.message, true);
    }
  });

  els.settingsAtmForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const res = await fetch("/api/settings/atm", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          atm_method: els.setAtmMethod.value,
          delta_threshold: Number(els.setDeltaThreshold.value),
        }),
      });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || "Failed to apply ATM settings");
      showSettingsStatus("ATM settings updated.");
    } catch (err) {
      showSettingsStatus(err.message, true);
    }
  });

  els.settingsRiskForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const res = await fetch("/api/paper/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          take_profit_percent: Number(els.setTp.value),
          stop_loss_percent: Number(els.setSl.value),
        }),
      });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || "Failed to apply TP/SL");
      showSettingsStatus("TP/SL updated.");
    } catch (err) {
      showSettingsStatus(err.message, true);
    }
  });

  els.settingsBasisForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const res = await fetch("/api/settings/candle-basis", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ candle_price_basis: els.setCandleBasis.value }),
      });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || "Failed to apply candle basis");
      showSettingsStatus("Candle price basis updated.");
    } catch (err) {
      showSettingsStatus(err.message, true);
    }
  });
}

// ---------------------------------------------------------------------------
// Date/Session selector — Today vs. a past Asia/Kolkata trading date.
// "live" reuses the existing websocket-driven pipeline unchanged; picking a
// past date switches into "historical" mode: a single read-only fetch from
// GET /api/history/session, rendered through the same chart/table renderers
// used above, with all trading actions visually disabled (see
// ".app.is-historical" in styles.css) and every subsequent live tick ignored
// (see handleSnapshot()'s early-return guard).
// ---------------------------------------------------------------------------

// Today's Asia/Kolkata calendar date as "YYYY-MM-DD". The "en-CA" locale
// formats as ISO by construction; passing `timeZone` explicitly (not relying
// on the browser's own zone) is what keeps this correct regardless of where
// the browser happens to be running — same principle as fmtTime()/fmtDate().
function todayIsoIST() {
  return new Date().toLocaleDateString("en-CA", { timeZone: IST_TIMEZONE });
}

// Trading dates that can actually be navigated to: every persisted date
// (state.availableDates, from GET /api/history/dates) plus today — today is
// always navigable since it's live MOCK data, whether or not anything has
// been persisted for it yet. Sorted ascending.
function getNavigableDates() {
  const set = new Set(state.availableDates || []);
  if (state.todayIso) set.add(state.todayIso);
  return [...set].sort();
}

// Nearest persisted/navigable date strictly before `iso`, or null if there
// is none — this is the actual "skip empty dates" behavior: Previous Day
// jumps straight to the last date that has data, not just iso-1.
function previousAvailableDate(iso) {
  let prev = null;
  for (const d of getNavigableDates()) {
    if (d < iso) prev = d;
    else break;
  }
  return prev;
}

// Nearest persisted/navigable date strictly after `iso`, or null if there
// is none.
function nextAvailableDate(iso) {
  for (const d of getNavigableDates()) {
    if (d > iso) return d;
  }
  return null;
}

// A date typed/picked directly (the date-picker input) might land on a day
// with no data — snap to the nearest available one instead of showing an
// empty historical view: prefer the nearest earlier date, then the nearest
// later one, then fall back to today.
function snapToAvailableDate(iso) {
  const dates = getNavigableDates();
  if (dates.includes(iso)) return iso;
  return previousAvailableDate(iso) || nextAvailableDate(iso) || state.todayIso;
}

function setSessionMode(mode) {
  state.sessionMode = mode;
  els.appRoot.classList.toggle("is-historical", mode === "historical");
  els.historicalAtmPanel.classList.toggle("hidden", mode !== "historical");
  els.historicalStrikeSelect.classList.toggle("hidden", mode !== "historical");

  if (mode === "live") {
    els.sessionModeBadge.textContent = "● LIVE";
    els.sessionModeBadge.className = "session-mode-badge session-mode-live";
  } else {
    els.sessionModeBadge.textContent = "◼ HISTORICAL (READ-ONLY)";
    els.sessionModeBadge.className = "session-mode-badge session-mode-historical";
    // The 2+ selected-strikes picker is part of the live Multi-Strike
    // Selection system (state.multiSelected) — not meaningful historically;
    // the Straddle Chart's own historicalStrikeSelect takes over instead.
    els.straddleModeChip.classList.add("hidden");
    els.straddleFocusSelect.classList.add("hidden");
    if (els.simBanner) els.simBanner.classList.add("hidden");
  }
}

// "YYYY-MM-DD" -> "DD-MM-YYYY" by string reordering only — no Date object,
// so there's no locale/timezone reinterpretation risk (see fmtDate's IST
// pinning above; this is the same convention applied without ever touching
// a Date at all, since the source string is already a plain calendar date).
function fmtDateDDMMYYYY(iso) {
  if (!iso) return "—";
  const [y, m, d] = iso.split("-");
  return `${d}-${m}-${y}`;
}

function updateSessionNavButtons() {
  const iso = state.selectedDateIso;
  els.sessionPrevBtn.disabled = !iso || previousAvailableDate(iso) == null;
  els.sessionNextBtn.disabled = !iso || nextAvailableDate(iso) == null;
  if (iso) els.sessionDatePicker.value = iso;
  if (state.todayIso) els.sessionDatePicker.max = state.todayIso;
  if (els.sessionDateLabel) {
    els.sessionDateLabel.textContent = fmtDateDDMMYYYY(iso);
    els.sessionDateLabel.title =
      iso && state.todayIso && iso === state.todayIso ? "Today (live MOCK mode)" : "Historical trading date";
  }
}

async function loadAvailableDates() {
  try {
    const res = await fetch("/api/history/dates");
    const data = await res.json();
    state.todayIso = data.today || todayIsoIST();
    state.availableDates = data.dates || [];
  } catch (err) {
    console.error("Failed to load available trading dates", err);
    state.todayIso = state.todayIso || todayIsoIST();
  }
  if (!state.selectedDateIso) state.selectedDateIso = state.todayIso;
  updateSessionNavButtons();
}

function reshapeHistoricalCandles(candlesByInterval) {
  const reshaped = {};
  for (const [interval, series] of Object.entries(candlesByInterval || {})) {
    reshaped[interval] = { active: null, completed: (series || []).slice(-20), series: series || [] };
  }
  return reshaped;
}

function renderHistoricalCandles(session) {
  const reshaped = reshapeHistoricalCandles(session.candles);
  updateCandlesPanel({ candles: reshaped });
  updateChart({ candles: reshaped });
}

function renderHistoricalUnderlying(session) {
  const u = session.underlying || {};
  els.symbolTitle.textContent = u.symbol || "NIFTY";
  els.spotPrice.textContent = fmt(u.close);
  els.spotChange.textContent = "—";
  els.spotChange.className = "spot-change neutral";
  els.moLtp.textContent = fmt(u.close);
  els.moChange.textContent = "—";
  els.lastUpdated.textContent = u.last_updated
    ? `Session ${fmtDate(u.last_updated)} · O ${fmt(u.open)} H ${fmt(u.high)} L ${fmt(u.low)} C ${fmt(u.close)}`
    : "No data for this session";
  els.moTimestamp.textContent = u.last_updated ? `Session close ${fmtTime(u.last_updated)}` : "—";
  els.moTicks.textContent = "—";

  els.connStatus.textContent = "HISTORICAL";
  els.connStatus.className = "badge badge-simulated";
  els.staleBadge.classList.add("hidden");
  els.sessionBadge.textContent = "Historical Session";
  els.sessionBadge.className = "badge badge-session";
  els.tickReliability.textContent = "";
  els.dataSource.textContent = "HISTORICAL (persisted)";
  els.lastTickAge.textContent = "—";
  els.tickCount.textContent = "—";
}

function renderHistoricalAtmSummary(session) {
  const rows = session.option_chain_snapshots || [];
  const last = rows[rows.length - 1];

  els.atmStrike.textContent = last ? fmt(last.atm_strike, 0) : "—";
  els.atmCall.textContent = last ? fmt(last.atm_call_ltp) : "—";
  els.atmPut.textContent = last ? fmt(last.atm_put_ltp) : "—";
  els.atmStraddle.textContent = last ? fmt(last.straddle_premium) : "—";
  els.atmCallDelta.textContent = "—";
  els.atmPutDelta.textContent = "—";
  els.atmMethodChip.textContent = "Session close";
  els.atmCompare.classList.add("hidden");
  els.chainExpiry.textContent = fmtExpiry(last?.expiry);

  // The full multi-strike Option Chain grid reflects live in-memory state
  // and isn't persisted tick-by-tick (only ATM-focused snapshots are — see
  // PersistenceStore.option_chain_snapshots); point at the ATM history table
  // below instead of showing stale/misleading live rows.
  els.optionChainBody.innerHTML =
    `<tr><td colspan="8" class="empty">Historical session — full option chain replay isn't stored. See "Session ATM History" below for this day's recorded ATM strike/CE/PE, and the Straddle Chart for any persisted strike's own OHLC.</td></tr>`;

  const tail = rows.slice(-100).reverse();
  els.historicalAtmBody.innerHTML = tail.length
    ? tail
        .map(
          (r) => `
          <tr>
            <td>${fmtTime(r.timestamp)}</td>
            <td>${fmt(r.atm_strike, 0)}</td>
            <td>${fmt(r.atm_call_ltp)}</td>
            <td>${fmt(r.atm_put_ltp)}</td>
            <td>${fmt(r.straddle_premium)}</td>
          </tr>`
        )
        .join("")
    : `<tr><td colspan="5" class="empty">No data</td></tr>`;
}

function populateHistoricalStrikePicker(session) {
  const strikes = session.available_strikes || [];
  const focus = session.straddle_candles?.strike ?? state.historicalStrike ?? strikes[0] ?? null;
  state.historicalStrike = focus;

  if (strikes.length === 0) {
    els.historicalStrikeSelect.innerHTML = "";
    return;
  }
  els.historicalStrikeSelect.innerHTML = strikes
    .map((s) => `<option value="${s}"${s === focus ? " selected" : ""}>${fmt(s, 0)}</option>`)
    .join("");
}

function renderHistoricalStraddleChart(session) {
  populateHistoricalStrikePicker(session);
  const strike = state.historicalStrike;
  els.straddleTrackedStrike.textContent = strike != null ? fmt(strike, 0) : "—";
  els.straddleExpiry.textContent = fmtExpiry(session.option_chain_snapshots?.slice(-1)[0]?.expiry);
  els.straddleCurrentChange.textContent = "—";
  els.straddleCurrentChange.className = "spot-change neutral";

  if (!session.straddle_candles) {
    els.straddleCurrentValue.textContent = "—";
    state.straddleCandleData = null;
    return;
  }
  state.straddleCandleData = session.straddle_candles;
  state.straddleLastFetchedStrike = session.straddle_candles.strike;
  renderStraddleChart();

  const bucket = session.straddle_candles.by_interval?.[state.straddleInterval];
  const bars = bucket ? bucket[state.straddleView] : null;
  const lastBar = bars && bars.length ? bars[bars.length - 1] : null;
  els.straddleCurrentValue.textContent = lastBar ? fmt(lastBar.close) : "—";
}

// The historical counterpart of updatePaperTrading() — deliberately doesn't
// touch state.knownHistoryIds/paperInitialized (those drive the live "TP/SL
// hit" toast diffing) so switching between dates never fires a spurious
// toast for a trade that's simply new to this session's history.
function renderHistoricalPaperTrading(session) {
  const pt = session.paper_trading || {};
  renderOpenPositions([]); // a past, closed session never has open positions
  renderHistory(pt.history || []);
  renderTradeStats(pt.stats);
  renderPnlSummary(pt.pnl_summary, []);
}

function renderHistoricalSession(session) {
  renderHistoricalUnderlying(session);
  renderHistoricalAtmSummary(session);
  renderHistoricalCandles(session);
  renderHistoricalStraddleChart(session);
  renderHistoricalPaperTrading(session);
}

async function loadHistoricalSession(iso, strike) {
  try {
    const url = `/api/history/session?date=${encodeURIComponent(iso)}${
      strike != null ? `&strike=${encodeURIComponent(strike)}` : ""
    }`;
    const res = await fetch(url);
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || "Failed to load session");
    const session = await res.json();
    state.historicalSession = session;
    els.sessionNoData.classList.toggle("hidden", session.has_data);
    renderHistoricalSession(session);
  } catch (err) {
    console.error("Failed to load historical session", err);
    els.sessionNoData.classList.remove("hidden");
  }
}

async function selectDate(iso) {
  if (!iso) return;
  state.selectedDateIso = iso;
  updateSessionNavButtons();

  if (state.todayIso && iso === state.todayIso) {
    setSessionMode("live");
    els.sessionNoData.classList.add("hidden");
    if (els.simBanner) els.simBanner.classList.remove("hidden");
    try {
      const snapshot = await fetch("/api/snapshot").then((r) => r.json());
      handleSnapshot(snapshot); // safe: sessionMode is already "live" here
    } catch (err) {
      console.error("Failed to load live snapshot", err);
    }
    return;
  }

  setSessionMode("historical");
  await loadHistoricalSession(iso);
}

function bindSessionBar() {
  // Previous/Next jump straight to the nearest trading date that actually
  // has persisted data (re-fetching the date list first so a long-running
  // session picks up dates that gained data after page load) — empty dates
  // are never landed on, per the "skip it and find the previous/next
  // available trading date" requirement.
  els.sessionPrevBtn.addEventListener("click", async () => {
    await loadAvailableDates();
    const prev = previousAvailableDate(state.selectedDateIso);
    if (prev) selectDate(prev);
  });

  els.sessionNextBtn.addEventListener("click", async () => {
    await loadAvailableDates();
    const next = nextAvailableDate(state.selectedDateIso);
    if (next) selectDate(next);
  });

  els.sessionTodayBtn.addEventListener("click", () => {
    if (state.todayIso) selectDate(state.todayIso);
  });

  els.sessionDatePicker.addEventListener("change", async (event) => {
    if (!event.target.value) return;
    await loadAvailableDates();
    selectDate(snapToAvailableDate(event.target.value));
  });

  // Recognize an IST midnight rollover without requiring a page reload:
  // poll the wall clock (cheap, local — no network) and only re-sync with
  // the server (new today, refreshed available-dates list, and — if the
  // user was on "Today" — follow it forward to the new day) once the IST
  // calendar date actually changes. A user parked on a historical date is
  // left exactly where they are; only `todayIso`/the picker's max and the
  // Today button's target move.
  state.dayRolloverTimer = setInterval(() => {
    const nowIso = todayIsoIST();
    if (nowIso === state.todayIso) return;
    const wasOnToday = state.selectedDateIso === state.todayIso;
    loadAvailableDates().then(() => {
      if (wasOnToday) selectDate(state.todayIso);
    });
  }, 60_000);

  // Switches which persisted strike's CE/PE/Straddle candles the Straddle
  // Chart shows within the already-loaded historical session — a lighter
  // fetch than reloading the whole session bundle.
  els.historicalStrikeSelect.addEventListener("change", async (event) => {
    const strike = Number(event.target.value);
    if (Number.isNaN(strike) || !state.selectedDateIso) return;
    state.historicalStrike = strike;
    try {
      const res = await fetch(
        `/api/straddle-candles?strike=${encodeURIComponent(strike)}&date=${encodeURIComponent(state.selectedDateIso)}`
      );
      if (!res.ok) return;
      state.straddleCandleData = await res.json();
      state.straddleLastFetchedStrike = strike;
      renderStraddleChart();
      els.straddleTrackedStrike.textContent = fmt(strike, 0);
    } catch (err) {
      console.error("Failed to load historical straddle candles", err);
    }
  });
}

// ---------------------------------------------------------------------------
// Snapshot dispatch
// ---------------------------------------------------------------------------

function handleSnapshot(snapshot) {
  // Historical dates are read-only — never let a live tick (websocket push
  // or a stray poll) overwrite the frozen historical view. Live ticks keep
  // arriving in the background regardless (the engine doesn't stop for a
  // viewer looking at the past), they're just not applied to the UI here.
  if (state.sessionMode === "historical") return;
  state.latestSnapshot = snapshot; // for instant re-renders outside the tick cadence (e.g. selection changes)
  els.symbolTitle.textContent = snapshot.underlying || "NIFTY";
  els.tickCount.textContent = snapshot.tick_count ?? 0;
  els.dataSource.textContent = snapshot.data_source || "MOCK";
  els.moTicks.textContent = snapshot.tick_count ?? 0;
  els.moTimestamp.textContent = snapshot.timestamp ? `As of ${fmtTime(snapshot.timestamp)}` : "—";
  if (snapshot.status) {
    setConnectionStatus(snapshot.status);
    // Reason behind ERROR/DISCONNECTED etc. (e.g. "Dhan Data APIs not
    // subscribed"), shown on hover rather than adding new layout.
    els.connStatus.title = snapshot.provider?.status_detail || "";
  }

  updateSpot(snapshot.price, snapshot.timestamp);

  els.staleBadge.classList.toggle("hidden", !snapshot.data_quality?.is_stale);
  const session = snapshot.data_quality?.market_session;
  els.sessionBadge.textContent = SESSION_LABELS[session] || "—";
  els.sessionBadge.className = `badge badge-session session-${(session || "unknown").toLowerCase()}`;
  els.lastTickAge.textContent =
    snapshot.data_quality?.last_tick_age_seconds == null
      ? "—"
      : snapshot.data_quality.last_tick_age_seconds < 1
      ? "just now"
      : `${snapshot.data_quality.last_tick_age_seconds.toFixed(1)}s ago`;
  const dup = snapshot.data_quality?.duplicate_tick_count || 0;
  const rejected = snapshot.data_quality?.rejected_tick_count || 0;
  els.tickReliability.textContent = dup + rejected > 0 ? ` (${dup} dup · ${rejected} rejected)` : "";

  updateATM(snapshot.atm);
  updateOptionChain(snapshot.option_chain, snapshot.atm);
  renderMultiSelectPanel(); // refresh live LTP/Delta/Expiry for any pending selection
  updateCandlesPanel(snapshot);
  updateChart(snapshot);
  updateStraddleChart(snapshot);
  updatePaperTrading(snapshot.paper_trading);
  updateDataQualityTab(snapshot.data_quality, snapshot.option_chain, snapshot.tick_count ?? 0);
  if (state.activeView === "rrg") renderRRG(snapshot.rrg);

  state.atmStrike = snapshot.atm?.strike ?? state.atmStrike;

  if (els.simBanner) {
    const isReplay = snapshot.settings?.data_source === "replay";
    els.simBanner.classList.toggle("hidden", snapshot.status !== "SIMULATED" && !isReplay);
    if (isReplay) {
      els.simBannerText.textContent = `REPLAY / TEST MODE — deterministic mock sequence "${snapshot.settings.replay?.sequence || "?"}" @ ${snapshot.settings.replay?.speed || 1}×. No real Dhan orders are ever placed.`;
    } else {
      els.simBannerText.textContent =
        "SIMULATED MARKET DATA — mock ticks only. Paper trading is practice-only; no real Dhan orders are ever placed.";
    }
  }

  if (snapshot.newly_completed?.length) {
    for (const candle of snapshot.newly_completed) {
      showToast(
        `${candle.interval_minutes}m candle closed · O ${fmt(candle.open)} H ${fmt(candle.high)} L ${fmt(candle.low)} C ${fmt(candle.close)}`
      );
    }
  }
}

let fallbackPollTimer = null;

function startFallbackPolling() {
  if (fallbackPollTimer) return;
  fallbackPollTimer = setInterval(async () => {
    if (state.sessionMode === "historical") return;
    try {
      const res = await fetch("/api/snapshot");
      if (res.ok) {
        const snapshot = await res.json();
        handleSnapshot(snapshot);
      }
    } catch (err) {
      // transient network error
    }
  }, 2000);
}

function stopFallbackPolling() {
  if (fallbackPollTimer) {
    clearInterval(fallbackPollTimer);
    fallbackPollTimer = null;
  }
}

function connectWebSocket() {
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${protocol}://${window.location.host}/ws`);

  ws.onopen = () => {
    stopFallbackPolling();
    // Pre-first-snapshot placeholder; the real LIVE/SIMULATED/OFFLINE label
    // arrives (and overwrites this) on the first message. Must be a string —
    // setConnectionStatus() calls .includes() on it.
    setConnectionStatus("CONNECTING");
    if (state.reconnectTimer) {
      clearTimeout(state.reconnectTimer);
      state.reconnectTimer = null;
    }
  };

  ws.onmessage = (event) => {
    try {
      const snapshot = JSON.parse(event.data);
      handleSnapshot(snapshot);
    } catch (err) {
      console.error("Failed to parse snapshot", err);
    }
  };

  ws.onclose = () => {
    setConnectionStatus(false);
    startFallbackPolling();
    state.reconnectTimer = setTimeout(connectWebSocket, 5000);
  };

  ws.onerror = () => {
    ws.close();
  };
}

function bindTabs() {
  els.intervalTabs.addEventListener("click", (event) => {
    const btn = event.target.closest(".tab");
    if (!btn) return;

    state.selectedInterval = btn.dataset.interval;
    els.intervalTabs.querySelectorAll(".tab").forEach((tab) => {
      tab.classList.toggle("active", tab === btn);
    });

    if (state.sessionMode === "historical") {
      if (state.historicalSession) renderHistoricalCandles(state.historicalSession);
      return;
    }

    fetch("/api/snapshot")
      .then((r) => r.json())
      .then(handleSnapshot)
      .catch(console.error);
  });
}

async function bootstrap() {
  initChart();
  initStraddleChart();
  bindTabs();
  bindStraddleTabs();
  bindMainTabs();
  bindPaperTrading();
  bindMultiSelect();
  bindLogs();
  bindSettings();
  bindSessionBar();

  state.todayIso = todayIsoIST();
  state.selectedDateIso = state.todayIso;
  setSessionMode("live");
  await loadAvailableDates(); // may refine todayIso from the server's clock

  try {
    const snapshot = await fetch("/api/snapshot").then((r) => r.json());
    handleSnapshot(snapshot);
  } catch (err) {
    console.error(err);
  }

  connectWebSocket();
}

bootstrap();
