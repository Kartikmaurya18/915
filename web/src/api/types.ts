/** Response shapes of the REST API and of the WebSocket topics (see docs/REALTIME.md). */

export interface AppConfig {
  underlying: string;
  data_source: string;
  market_data_provider: string;
  greeks_source: string;
  execution_mode: string;
  poll_interval_seconds: number;
  candle_intervals: number[];
}

// -- topics ----------------------------------------------------------------------------

export interface Atm {
  strike: number;
  call_ltp: number | null;
  put_ltp: number | null;
  straddle_premium: number | null;
  combined_value: number | null;
  underlying_ltp: number | null;
  distance_from_spot: number | null;
  method: string;
  call_delta: number | null;
  put_delta: number | null;
}

export interface DataQuality {
  source: string;
  connection_status: string;
  tick_count: number;
  last_tick_time: string | null;
  last_tick_age_seconds: number | null;
  is_stale: boolean;
  stale_threshold_seconds: number;
  persistence_enabled: boolean;
  duplicate_tick_count: number;
  rejected_tick_count: number;
  market_session: string;
  /** Age of the option chain's prices, separate from the index tick. */
  chain_age_seconds?: number | null;
  /** Judged by the wall clock for a real feed. */
  market_open?: boolean;
}

export interface ProviderInfo {
  name: string;
  status: string;
  status_detail: string | null;
  is_simulated: boolean;
  underlying: string;
  /** When the provider's access token stops working (ISO), if known. */
  credentials_expire_at?: string | null;
}

/** `index:<UNDERLYING>` */
export interface IndexTopic {
  underlying: string;
  data_source: string;
  status: string;
  provider: ProviderInfo;
  tick_count: number;
  price: number | null;
  timestamp: string | null;
  data_quality: DataQuality;
  atm: Atm | null;
  calculated_prices: Record<string, number | string | null>;
  settings: Record<string, unknown>;
}

export interface StrikeRow {
  strike: number;
  call_ltp: number | null;
  put_ltp: number | null;
  call_oi: number | null;
  put_oi: number | null;
  call_volume: number | null;
  put_volume: number | null;
  call_delta: number | null;
  put_delta: number | null;
  /** Implied volatility in percentage points (14.2 = 14.2 %). */
  call_iv: number | null;
  put_iv: number | null;
  call_bid: number | null;
  call_ask: number | null;
  put_bid: number | null;
  put_ask: number | null;
  call_gamma: number | null;
  put_gamma: number | null;
  /** Per calendar day. */
  call_theta: number | null;
  put_theta: number | null;
  /** Per 1 volatility point. */
  call_vega: number | null;
  put_vega: number | null;
  /** Open interest change since the previous session's close; null when the provider sends no baseline. */
  call_oi_change: number | null;
  put_oi_change: number | null;
  combined_value: number | null;
  is_atm: boolean;
}

/** `options:<UNDERLYING>` */
export interface OptionsTopic {
  underlying: string;
  underlying_ltp: number | null;
  expiry: string;
  expiries: string[];
  provider: string;
  /** "provider", "calculated" or "provider+calculated": where the Greeks and IV came from. */
  greeks_source?: string | null;
  timestamp: string;
  strikes: StrikeRow[];
}

export interface Candle {
  symbol: string;
  interval_minutes: number;
  open_time: string;
  close_time: string;
  /** Unix seconds (already shifted for IST display by the backend). */
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  tick_count: number;
  is_complete: boolean;
}

/** `candles:<UNDERLYING>:<n>m` */
export interface CandlesTopic {
  active: Candle | null;
  completed: Candle[];
  series: Candle[];
  newly_completed: Candle[];
}

export interface RrgPoint {
  strike: number;
  option_type: "CE" | "PE";
  rs: number;
  rs_ratio: number;
  rs_momentum: number;
  quadrant: string;
}

/** `rrg:<UNDERLYING>` */
export interface RrgTopic {
  rrg: { data_points: RrgPoint[] } | null;
}

// -- paper trading ---------------------------------------------------------------------

export interface PaperPosition {
  id: string;
  option_type: "CE" | "PE";
  side: "BUY" | "SELL";
  strike: number;
  quantity: number;
  entry_price: number;
  current_price: number;
  entry_time: string | null;
  exit_time: string | null;
  exit_price: number | null;
  exit_reason: string | null;
  status: "OPEN" | "EXITED";
  pnl: number;
  pnl_percent: number;
  take_profit_percent: number;
  stop_loss_percent: number;
  take_profit_amount: number | null;
  stop_loss_amount: number | null;
  take_profit_price: number;
  stop_loss_price: number;
  expiry: string | null;
}

export interface PaperConfig {
  take_profit_percent: number;
  stop_loss_percent: number;
  take_profit_amount: number | null;
  stop_loss_amount: number | null;
}

export interface PaperState {
  simulated: boolean;
  config: PaperConfig;
  open_positions: PaperPosition[];
  history: PaperPosition[];
  stats: {
    total_trades: number;
    wins: number;
    losses: number;
    win_rate_percent: number;
    total_pnl: number;
    average_win: number;
    average_loss: number;
    max_drawdown: number;
  };
  pnl_summary: {
    unrealized_pnl: number;
    realized_pnl: number;
    total_pnl: number;
    total_pnl_percent: number;
    buy_turnover: number;
    sell_turnover: number;
    total_turnover: number;
    total_position_value: number;
    open_position_count: number;
  };
}

export interface OpenPositionBody {
  option_type: "CE" | "PE";
  side: "BUY" | "SELL";
  quantity: number;
  strike?: number | null;
}

// -- operator --------------------------------------------------------------------------

export interface LogEntry {
  timestamp: string;
  level?: string;
  event?: string;
  event_type?: string;
  data?: unknown;
}

export interface SettingsPayload {
  data_source: string;
  available_providers: string[];
  atm_method: string;
  delta_threshold: number;
  candle_price_basis: string;
  expiry: string | null;
  replay: { available_sequences: string[]; sequence: string | null; speed: number | null };
}

// -- straddle chart and history ----------------------------------------------------------

export interface Bar {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
}

export type StraddleView = "straddle" | "ce" | "pe";

/** `GET /api/straddle-candles` — all intervals and legs for one strike, so tab switches need no request. */
export interface StraddleCandles {
  strike: number;
  by_interval: Record<string, Partial<Record<StraddleView, Bar[]>>>;
}

/** `GET /api/pair-candles`: candles of CE@ce_strike + PE@pe_strike, tracked from the first request. */
export interface PairCandles {
  ce_strike: number;
  pe_strike: number;
  expiry: string;
  /** When the server started tracking this pair; nothing earlier exists. */
  tracked_since: string;
  by_interval: Record<string, { pair: Bar[] }>;
}

export interface TradingDates {
  dates: string[];
  today: string;
}

export interface AtmSnapshotRow {
  timestamp: string;
  expiry: string | null;
  atm_strike: number;
  atm_call_ltp: number | null;
  atm_put_ltp: number | null;
  straddle_premium: number | null;
}

/** `GET /api/history/session?date=…` */
export interface HistoricalSession {
  trading_date: string;
  has_data: boolean;
  underlying: { symbol: string; open: number | null; high: number | null; low: number | null; close: number | null; last_updated: string | null };
  candles: Record<string, Candle[]>;
  option_chain_snapshots: AtmSnapshotRow[];
  available_strikes: number[];
  straddle_candles: StraddleCandles | null;
  paper_trading: {
    history: PaperPosition[];
    stats: PaperState["stats"];
    pnl_summary: PaperState["pnl_summary"];
  };
}
