from __future__ import annotations

import asyncio
import os
import queue
import threading
import time
from datetime import datetime
from typing import Optional

try:
    from dhanhq import dhanhq
    from dhanhq.marketfeed import DhanFeed, IDX, Ticker, Quote, Full
except ImportError:
    dhanhq = None
    DhanFeed = IDX = Ticker = Quote = Full = None

from src.data_sources.base import DataSource, ProviderStatus
from src.instruments import get_underlying_spec
from src.models import OptionChainSnapshot, PriceTick
from src.logging import get_logger
from src.timeutil import now_ist


from pathlib import Path
from dotenv import load_dotenv


def _ensure_env_loaded() -> None:
    root_dir = Path(__file__).resolve().parent.parent.parent
    root_env = root_dir / ".env"
    example_env = root_dir / ".env.example"

    # If .env doesn't exist but .env.example exists with credentials, copy/sync
    if not root_env.exists() and example_env.exists():
        try:
            content = example_env.read_text(encoding="utf-8")
            root_env.write_text(content, encoding="utf-8")
        except Exception:
            pass

    for name in [".env", ".env.example"]:
        env_file = root_dir / name
        if env_file.exists():
            load_dotenv(env_file)

    cwd = Path.cwd()
    for name in [".env", ".env.example"]:
        cwd_file = cwd / name
        if cwd_file.exists():
            load_dotenv(cwd_file)

    load_dotenv()


# Dhan underlying security ids (exchange segment IDX_I for all index
# underlyings). Verify against Dhan's instrument master
# (https://images.dhan.co/api-data/api-scrip-master.csv) before enabling a
# new underlying — an unmapped underlying raises instead of silently
# streaming NIFTY.
DHAN_UNDERLYING_IDS: dict[str, int] = {
    "NIFTY": 13,
    "BANKNIFTY": 25,
    "SENSEX": 51,
}


def _unwrap_dhan(payload):
    """dhanhq wraps every API body as {"status", "remarks", "data": <body>},
    and the option-chain / expiry-list bodies are themselves {"data": ...,
    "status": ...}. Descend through `data` until reaching the real content
    (the dict holding "oc", or the expiry list)."""
    cur = payload
    for _ in range(3):
        if isinstance(cur, dict) and "oc" not in cur and "data" in cur:
            cur = cur["data"]
        else:
            break
    return cur


def _opt_int(value):
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _opt_float(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class DhanMarketDataProvider(DataSource):
    """
    Live market data provider using DhanHQ API v2.0.2.

    Uses WebSocket (DhanFeed v2) for real-time tick data and REST API for option chain.
    Requires DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN environment variables
    (server-side only — they are never serialized to the frontend).

    Dhan supplies Delta and IV per contract in its option chain; both are
    mapped straight into the normalized chain (see _normalize_leg).
    """

    provider_name = "DHAN"

    # Kept for backward compatibility; the active underlying's id/segment
    # are on the instance (self.security_id / self.segment).
    NIFTY_SECURITY_ID = 13
    NIFTY_SEGMENT = "IDX_I"
    NIFTY_SEGMENT_NUM = IDX  # 0 for IDX

    OPTION_CHAIN_RATE_LIMIT = 3.0  # seconds between REST requests

    def __init__(self, underlying: str = "NIFTY", poll_interval: float = 5.0):
        self.underlying = underlying.upper()
        self.poll_interval = poll_interval
        self._logger = get_logger("INFO")
        if self.underlying not in DHAN_UNDERLYING_IDS:
            raise ValueError(
                f"No Dhan security id mapped for underlying {self.underlying!r}. "
                f"Mapped: {sorted(DHAN_UNDERLYING_IDS)} (see DHAN_UNDERLYING_IDS in src/data_sources/dhan.py)."
            )
        self.security_id = DHAN_UNDERLYING_IDS[self.underlying]
        self.segment = "IDX_I"
        self.exchange = get_underlying_spec(self.underlying).exchange
        self._last_error: str | None = None
        self._disconnected = False

        # Ensure .env file is loaded before reading environment variables
        _ensure_env_loaded()

        self._client_id = os.getenv("DHAN_CLIENT_ID")
        self._access_token = os.getenv("DHAN_ACCESS_TOKEN")

        client_id_present = bool(
            self._client_id and self._client_id.strip() and self._client_id != "your_client_id_here"
        )
        access_token_present = bool(
            self._access_token and self._access_token.strip() and self._access_token != "your_access_token_here"
        )

        # Safe diagnostic reporting (never exposing credential values)
        self._logger.log_system_event(
            "dhan_credential_diagnostic",
            {
                "Client ID present": "yes" if client_id_present else "no",
                "Access token present": "yes" if access_token_present else "no",
            }
        )

        if not client_id_present or not access_token_present:
            diag_msg = f"Client ID present: {'yes' if client_id_present else 'no'}, Access token present: {'yes' if access_token_present else 'no'}"
            raise ValueError(
                f"DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN environment variables are required for Dhan data source. Diagnostic: [{diag_msg}]"
            )

        self.client_id_masked = self._mask_client_id(self._client_id)

        # State tracking
        self._is_authenticated = False
        self._is_connected = False
        self._has_received_tick = False
        self._latest_tick: Optional[PriceTick] = None

        # REST client initialization
        self._dhan: Optional[dhanhq] = None
        self._initialize_rest_client()

        # State for option chain caching & expiry
        self._last_chain: dict | None = None
        self._last_chain_fetch_time: float = 0.0
        self._current_expiry: str | None = None
        self._expiries: list[str] = []
        self._initialize_expiry()

        # WebSocket thread management
        self._feed: Optional[DhanFeed] = None
        self._tick_queue: queue.Queue = queue.Queue(maxsize=1000)
        self._ws_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def _mask_client_id(self, client_id: str) -> str:
        """Mask client ID for safe logging."""
        if len(client_id) <= 4:
            return "****"
        return client_id[:2] + "****" + client_id[-2:]

    def _initialize_rest_client(self) -> None:
        """Initialize Dhan REST API client."""
        try:
            self._dhan = dhanhq(
                client_id=self._client_id,
                access_token=self._access_token
            )
            self._is_authenticated = True
            self._logger.log_system_event(
                "dhan_auth_success",
                {"client_id": self.client_id_masked, "status": "authenticated"}
            )
        except Exception as e:
            self._is_authenticated = False
            self._last_error = "Dhan authentication failed"
            self._logger.log_error(e, {"context": "dhan_rest_init", "client_id": self.client_id_masked})
            raise ConnectionError(f"Failed to authenticate Dhan client: {e}") from e

    def _initialize_expiry(self) -> None:
        """Fetch and set the nearest expiry for the underlying."""
        if not self._dhan:
            return
        try:
            expiry_list = self._dhan.expiry_list(
                under_security_id=self.security_id,
                under_exchange_segment=self.segment
            )
            expiries = _unwrap_dhan(expiry_list)

            if isinstance(expiries, list) and len(expiries) > 0:
                self._expiries = [str(e) for e in expiries]
                self._current_expiry = self._expiries[0]
                self._last_error = None
                self._logger.log_system_event("dhan_expiry_set", {"expiry": self._current_expiry})
            elif isinstance(expiry_list, dict) and expiry_list.get("status") == "failure":
                err_data = str(expiry_list.get("data", {})) + str(expiry_list.get("remarks", ""))
                if "808" in err_data or "Authentication Failed" in err_data:
                    self._last_error = "Dhan authentication failed (808): client id or access token invalid/expired"
                    self._logger.log_system_event(
                        "dhan_auth_error",
                        {
                            "error_code": 808,
                            "message": "Dhan Authentication Failed: Client ID or Access Token invalid/expired. Check credentials in .env file."
                        }
                    )
                elif "806" in err_data or "Data APIs not Subscribed" in err_data:
                    self._last_error = "Dhan Data APIs not subscribed (806)"
                    self._logger.log_system_event(
                        "dhan_subscription_error",
                        {
                            "error_code": 806,
                            "message": "Dhan Data APIs not Subscribed. Enable Data API Access in your DhanHQ portal."
                        }
                    )
                else:
                    self._last_error = "Dhan expiry list fetch failed"
                    self._logger.log_system_event("dhan_expiry_warning", {"message": f"Expiry fetch failed: {expiry_list}"})
            else:
                self._logger.log_system_event("dhan_expiry_warning", {"message": "No expiry dates found"})
        except Exception as e:
            self._logger.log_error(e, {"context": "dhan_expiry_fetch"})
            self._current_expiry = None

    def fetch_option_chain(self) -> dict:
        """
        Fetch option chain from Dhan REST API with rate limiting.
        """
        current_time = time.time()
        time_since_last_fetch = current_time - self._last_chain_fetch_time

        if time_since_last_fetch < self.OPTION_CHAIN_RATE_LIMIT:
            sleep_time = self.OPTION_CHAIN_RATE_LIMIT - time_since_last_fetch
            time.sleep(sleep_time)

        try:
            if not self._current_expiry:
                self._initialize_expiry()
                if not self._current_expiry:
                    raise ValueError("Cannot fetch option chain: no expiry available")

            assert self._dhan is not None
            response = self._dhan.option_chain(
                under_security_id=self.security_id,
                under_exchange_segment=self.segment,
                expiry=self._current_expiry
            )

            self._last_chain_fetch_time = time.time()
            normalized = self._normalize_option_chain(response)
            self._last_chain = normalized
            self._last_error = None
            return normalized

        except Exception as e:
            self._last_error = f"Dhan option chain fetch failed: {e.__class__.__name__}"
            self._logger.log_error(e, {"context": "dhan_option_chain_fetch"})
            if self._last_chain:
                return self._last_chain
            raise ConnectionError(f"Dhan option chain fetch failed: {e}") from e

    @staticmethod
    def _normalize_leg(leg: dict) -> dict:
        """One Dhan CE/PE object -> the common NSE-style leg dict that
        parse_option_chain() reads. Only fields Dhan actually sent are
        populated; absent ones stay None (never a made-up 0)."""
        greeks = leg.get("greeks") or {}
        return {
            "lastPrice": float(leg.get("last_price") or 0.0),
            "openInterest": _opt_int(leg.get("oi")),
            "totalTradedVolume": _opt_int(leg.get("volume")),
            "delta": _opt_float(greeks.get("delta")),
            "impliedVolatility": _opt_float(leg.get("implied_volatility")),
            "bidprice": _opt_float(leg.get("top_bid_price")),
            "askPrice": _opt_float(leg.get("top_ask_price")),
            "securityId": str(leg["security_id"]) if leg.get("security_id") is not None else None,
        }

    def _normalize_option_chain(self, dhan_response: dict) -> dict:
        """
        Map a Dhan option chain response into the common NSE-style
        interchange dict, which DataSource.get_option_chain() turns into the
        normalized OptionChainSnapshot — the same path MOCK uses, so nothing
        downstream has a Dhan-specific branch.
        """
        try:
            if isinstance(dhan_response, dict) and dhan_response.get("status") == "failure":
                raise ValueError(
                    f"Dhan API option chain failed: {dhan_response.get('remarks') or dhan_response.get('data')}"
                )

            data = _unwrap_dhan(dhan_response)
            if not isinstance(data, dict):
                raise ValueError("Dhan option chain response has no data")
            underlying_ltp = data.get("last_price", 0.0)
            oc_data = data.get("oc", {}) or {}

            strikes = []
            for strike_str, strike_data in oc_data.items():
                try:
                    strike = float(strike_str)
                    ce = self._normalize_leg(strike_data.get("ce") or {})
                    pe = self._normalize_leg(strike_data.get("pe") or {})
                    if ce["lastPrice"] > 0 or pe["lastPrice"] > 0:
                        strikes.append({"strikePrice": strike, "CE": ce, "PE": pe})
                except (ValueError, TypeError, AttributeError):
                    continue

            strikes.sort(key=lambda x: x["strikePrice"])

            return {
                "records": {
                    "underlyingValue": float(underlying_ltp),
                    "expiryDates": [self._current_expiry] if self._current_expiry else [],
                    "timestamp": now_ist().strftime("%d-%b-%Y %H:%M:%S"),
                    "data": strikes,
                }
            }

        except Exception as e:
            self._logger.log_error(e, {"context": "dhan_normalization"})
            raise ValueError(f"Failed to normalize Dhan option chain: {e}") from e

    def get_option_chain(self) -> OptionChainSnapshot:
        chain = super().get_option_chain()
        chain.exchange = self.exchange
        if self._expiries:
            chain.expiries = list(self._expiries)
        return chain

    def list_expiries(self) -> list[str]:
        return list(self._expiries)

    def status(self) -> ProviderStatus:
        if self._has_received_tick and self._is_connected:
            return ProviderStatus.LIVE
        if self._last_error:
            return ProviderStatus.ERROR
        if self._disconnected:
            return ProviderStatus.DISCONNECTED
        return ProviderStatus.CONNECTING

    def status_detail(self) -> str | None:
        return self._last_error

    def get_underlying_ltp(self) -> float:
        """Return latest underlying LTP."""
        if self._latest_tick:
            return self._latest_tick.price
        if self._last_chain is None:
            self.fetch_option_chain()
        assert self._last_chain is not None
        return float(self._last_chain["records"]["underlyingValue"])

    def stream_ticks(self):
        """
        Yield PriceTick objects continuously via Dhan WebSocket.
        """
        self._stop_event.clear()
        self._disconnected = False

        # Start WebSocket background thread
        if not self._ws_thread or not self._ws_thread.is_alive():
            self._ws_thread = threading.Thread(
                target=self._run_websocket_loop,
                daemon=True
            )
            self._ws_thread.start()

        while not self._stop_event.is_set():
            try:
                tick = self._tick_queue.get(timeout=1.0)
                if tick is None:
                    break
                yield tick
            except queue.Empty:
                if self._ws_thread and not self._ws_thread.is_alive() and not self._stop_event.is_set():
                    self._logger.log_system_event("dhan_ws_restarting", {"reason": "thread_died"})
                    self._ws_thread = threading.Thread(
                        target=self._run_websocket_loop,
                        daemon=True
                    )
                    self._ws_thread.start()
                continue

    def _run_websocket_loop(self) -> None:
        """Background thread executing async WebSocket event loop."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        try:
            self._loop.run_until_complete(self._async_websocket_handler())
        except Exception as e:
            self._logger.log_error(e, {"context": "dhan_ws_loop_fatal"})
        finally:
            self._is_connected = False
            self._loop.close()
            self._loop = None

    async def _async_websocket_handler(self) -> None:
        """Async handler for DhanFeed WebSocket connection and continuous tick processing."""
        instruments = [(self.NIFTY_SEGMENT_NUM, str(self.security_id), Ticker)]

        retry_delay = 2
        max_retry_delay = 30

        while not self._stop_event.is_set():
            try:
                self._logger.log_system_event(
                    "dhan_ws_connecting",
                    {
                        "client_id": self.client_id_masked,
                        "instruments": f"{self.underlying} (SecID: {self.security_id}, Segment: {self.segment})",
                        "version": "v2"
                    }
                )

                self._feed = DhanFeed(
                    client_id=self._client_id,
                    access_token=self._access_token,
                    instruments=instruments,
                    version="v2"
                )

                await self._feed.connect()
                self._is_connected = True
                retry_delay = 2  # reset delay on successful connection

                self._logger.log_system_event(
                    "dhan_ws_connected",
                    {
                        "status": "connected",
                        "client_id": self.client_id_masked,
                        "subscribed_symbols": [self.underlying]
                    }
                )

                # Continuous receive loop
                while not self._stop_event.is_set():
                    data = await self._feed.get_instrument_data()

                    if getattr(self._feed, "on_close", False):
                        self._logger.log_system_event("dhan_ws_closed_by_server", {"status": "disconnected"})
                        self._is_connected = False
                        break

                    if data and isinstance(data, dict):
                        ltp_val = data.get("LTP") or data.get("last_price")
                        if ltp_val is not None:
                            try:
                                price = float(ltp_val)
                                sec_id = str(data.get("security_id", self.security_id))
                                ltt = str(data.get("LTT", ""))
                                tick_ts = now_ist()

                                tick = PriceTick(
                                    symbol=self.underlying,
                                    price=price,
                                    timestamp=tick_ts,
                                    provider=self.provider_name,
                                    exchange=self.exchange,
                                    instrument_id=sec_id,
                                )

                                self._latest_tick = tick
                                self._has_received_tick = True

                                self._logger.log_price_tick(
                                    tick,
                                    {
                                        "source": "dhan",
                                        "instrument_id": sec_id,
                                        "ltp": price,
                                        "ltt": ltt,
                                        "connected": True
                                    }
                                )

                                try:
                                    self._tick_queue.put_nowait(tick)
                                except queue.Full:
                                    pass
                            except ValueError as ve:
                                self._logger.log_error(ve, {"context": "dhan_tick_parse_value"})

            except (websockets.exceptions.ConnectionClosed, websockets.exceptions.WebSocketException) as ws_err:
                self._is_connected = False
                self._logger.log_error(ws_err, {"context": "dhan_ws_connection_closed", "reconnect_in": retry_delay})
            except Exception as exc:
                self._is_connected = False
                self._logger.log_error(exc, {"context": "dhan_ws_error", "reconnect_in": retry_delay})

            if not self._stop_event.is_set():
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, max_retry_delay)

    def disconnect(self) -> None:
        """Clean disconnect of Dhan WebSocket connection."""
        self._stop_event.set()
        self._disconnected = True
        try:
            self._tick_queue.put_nowait(None)
        except queue.Full:
            pass

        if self._feed and self._feed.ws:
            try:
                if self._loop and self._loop.is_running():
                    asyncio.run_coroutine_threadsafe(self._feed.ws.close(), self._loop)
                self._logger.log_system_event("dhan_ws_disconnected", {"status": "clean"})
            except Exception as e:
                self._logger.log_error(e, {"context": "dhan_disconnect"})

        if self._ws_thread and self._ws_thread.is_alive():
            self._ws_thread.join(timeout=2)
            self._ws_thread = None

        self._is_connected = False


    @property
    def is_authenticated(self) -> bool:
        return self._is_authenticated

    @property
    def is_connected(self) -> bool:
        return self._is_connected

    @property
    def has_received_tick(self) -> bool:
        return self._has_received_tick

    @property
    def latest_tick(self) -> Optional[PriceTick]:
        return self._latest_tick


# Alias for backward compatibility
DhanDataSource = DhanMarketDataProvider
