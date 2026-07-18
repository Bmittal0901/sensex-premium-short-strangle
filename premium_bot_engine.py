# premium_bot_engine.py
"""
PremiumSellBot: sells 1 CE + 1 PE, each picked so its live premium is
inside a target band (default 60 +/- 5), with an independent per-leg
stop loss / one-time re-entry, and a combined target-profit exit.

See premium_strategy.py for the exact rules. This file only handles
order placement, polling, and the state machine -- the math lives there.

Mirrors the order-placement / retry / dry-run conventions already used
in bot_engine.py so behaviour is consistent with the existing bot, but
is otherwise a self-contained engine (doesn't import or modify
TradingBot) so it can't affect the existing multi-leg algo.
"""
import threading
import time
from datetime import datetime

import pytz

from premium_strategy import (
    PREMIUM_TARGET,
    PREMIUM_TOLERANCE,
    pick_strike_in_band,
    in_premium_band,
    sell_leg_hit_stop_loss,
    sell_leg_pnl,
    combined_pnl,
)
from utils import list_option_candidates, INDEX_EXCHANGE
from premium_bot_engine import env_dry_run

IST = pytz.timezone("Asia/Kolkata")
ORDER_RETRY_COUNT = 3
ORDER_STATUS_TIMEOUT = 5
POLL_INTERVAL = 5

INDEX_QUOTE_SYMBOL = {
    "SENSEX": "BSE:SENSEX",
    "NIFTY": "NSE:NIFTY 50",
}


class PremiumSellBot:
    """
    SENSEX-only. config keys:
      expiry, lots, target_profit    (the only 3 things the user supplies)
      dry_run                        (optional -- defaults to DRY_RUN env var, same as the main bot)

    Everything else (index, premium band, SL %, square-off time) is fixed
    by design, not user-configurable.
    """
    INDEX = "SENSEX"
    LOT_SIZE = 20
    SL_PREMIUM_TARGET = PREMIUM_TARGET
    SL_PREMIUM_TOLERANCE = PREMIUM_TOLERANCE
    SL_PREMIUM_FALLBACK_TOLERANCE = PREMIUM_TOLERANCE * 2  # 60 +/- 5, fallback 60 +/- 10
    SQUARE_OFF_TIME = "15:20"

    def __init__(self, kite, config: dict):
        self.kite = kite
        self.config = config

        self.dry_run = config.get("dry_run")
        if self.dry_run is None:
            self.dry_run = env_dry_run()

        self.index = self.INDEX
        self.expiry = config["expiry"]
        self.lots = config["lots"]
        self.lot_size = self.LOT_SIZE
        self.qty = self.lots * self.lot_size
        self.target_profit = config["target_profit"]
        self.premium_target = self.SL_PREMIUM_TARGET
        self.premium_tolerance = self.SL_PREMIUM_TOLERANCE
        self.premium_fallback_tolerance = self.SL_PREMIUM_FALLBACK_TOLERANCE
        self.square_off_time = self.SQUARE_OFF_TIME
        self.exchange = INDEX_EXCHANGE[self.index]

        self._lock = threading.Lock()
        self._manual_stop = threading.Event()
        self._thread = None
        self._stop_only = False
        self._exit_requested = False

        self.status = "idle"
        self.error_message = None
        self.index_ltp = None
        self.total_pnl = 0.0
        self.exit_reason = None
        self.started_at = None
        self.ended_at = None

        self._candidates = {"CE": [], "PE": []}
        self.legs = {leg: self._new_leg_state(leg) for leg in ("CE", "PE")}

    @staticmethod
    def _new_leg_state(option_type):
        return {
            "option_type": option_type,
            # searching | in_position | reentry_wait |
            # closed_permanent | closed_target | closed_manual | closed_eod | error
            "phase": "searching",
            "strike": None,
            "symbol": None,
            "token": None,
            "entry_premium": None,
            "current_premium": None,
            "qty": 0,
            "realized_pnl": 0.0,
            "reentry_used": False,
            "sl_count": 0,
            "entry_time": None,
        }

    # ---------------- public control surface ----------------

    def start(self):
        if self._thread and self._thread.is_alive():
            raise RuntimeError("Bot is already running.")
        self._manual_stop.clear()
        self._stop_only = False
        self._exit_requested = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def request_stop(self):
        """Stop monitoring only. Any open positions are left open."""
        self._stop_only = True
        self._manual_stop.set()

    def request_exit(self):
        """Stop monitoring and exit any open positions."""
        self._exit_requested = True
        self._manual_stop.set()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "status": self.status,
                "index": self.index,
                "index_ltp": self.index_ltp,
                "expiry": self.expiry,
                "error_message": self.error_message,
                "dry_run": self.dry_run,
                "exchange": self.exchange,
                "qty": self.qty,
                "lots": self.lots,
                "target_profit": self.target_profit,
                "premium_target": self.premium_target,
                "premium_tolerance": self.premium_tolerance,
                "premium_fallback_tolerance": self.premium_fallback_tolerance,
                "legs": {k: dict(v) for k, v in self.legs.items()},
                "total_pnl": round(self.total_pnl, 2),
                "exit_reason": self.exit_reason,
                "started_at": self.started_at,
                "ended_at": self.ended_at,
            }

    def _set(self, **kwargs):
        with self._lock:
            for k, v in kwargs.items():
                setattr(self, k, v)

    # ---------------- market / timing helpers ----------------

    def _is_market_open(self):
        now = datetime.now(IST)
        if now.weekday() >= 5:
            return False
        market_open = now.replace(hour=9, minute=50, second=0, microsecond=0)
        market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)
        return market_open <= now <= market_close

    def _past_square_off(self):
        now = datetime.now(IST)
        hh, mm = self.square_off_time.split(":")
        cutoff = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        return now >= cutoff

    def _update_index_ltp(self):
        try:
            symbol = INDEX_QUOTE_SYMBOL[self.index]
            quote = self.kite.ltp([symbol])
            self.index_ltp = quote[symbol]["last_price"]
        except Exception as e:
            print(f"Index LTP error: {e}")

    # ---------------- order helpers (mirrors bot_engine.py conventions) ----------------

    def _place_order(self, symbol, qty, transaction_type):
        exec_time = datetime.now(IST).strftime("%d-%m-%Y %H:%M:%S")
        action = "BUY" if transaction_type == self.kite.TRANSACTION_TYPE_BUY else "SELL"

        if self.dry_run:
            print(f"[DRY RUN] {action} {qty} x {symbol} at {exec_time}")
            return "DRY_RUN_ORDER"

        try:
            order_id = self.kite.place_order(
                variety=self.kite.VARIETY_REGULAR,
                exchange=self.exchange,
                tradingsymbol=symbol,
                transaction_type=transaction_type,
                quantity=qty,
                product=self.kite.PRODUCT_NRML,
                order_type=self.kite.ORDER_TYPE_MARKET,
            )
            print(f"[ORDER PLACED] {action} {qty} x {symbol} | Order ID: {order_id} | Time: {exec_time}")
            return order_id
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            print(f"[ORDER FAILED] {action} {qty} x {symbol} | {error}")
            self._set(error_message=error)
            return None

    def _get_ltp_map(self, symbols):
        """Batch-fetch LTP for a list of bare tradingsymbols on self.exchange."""
        if not symbols:
            return {}
        keys = [f"{self.exchange}:{s}" for s in symbols]
        data = self.kite.ltp(keys)
        return {
            s: data[f"{self.exchange}:{s}"]["last_price"]
            for s in symbols
            if f"{self.exchange}:{s}" in data
        }

    def _wait_for_order_completion(self, order_id, symbol, timeout=ORDER_STATUS_TIMEOUT):
        if self.dry_run:
            try:
                return self._get_ltp_map([symbol]).get(symbol, 0.0) or 0.0
            except Exception:
                return 0.0
        start = time.time()
        while time.time() - start < timeout:
            try:
                history = self.kite.order_history(order_id)
                last = history[-1]
                status = last["status"]
                if status == "COMPLETE":
                    return float(last["average_price"])
                if status in ("REJECTED", "CANCELLED"):
                    return False
                time.sleep(1)
            except Exception as e:
                print(f"Error checking order status: {e}")
                time.sleep(1)
        return False

    def _sell_leg(self, leg, symbol, qty, retries=ORDER_RETRY_COUNT):
        for attempt in range(1, retries + 1):
            order_id = self._place_order(symbol, qty, self.kite.TRANSACTION_TYPE_SELL)
            if not order_id:
                continue
            avg = self._wait_for_order_completion(order_id, symbol)
            if avg is not False:
                return avg
            print(f"Retrying entry for {symbol} (leg {leg}), attempt {attempt}...")
        return None

    def _buy_to_close(self, leg, symbol, qty, retries=ORDER_RETRY_COUNT):
        for attempt in range(1, retries + 1):
            order_id = self._place_order(symbol, qty, self.kite.TRANSACTION_TYPE_BUY)
            if not order_id:
                continue
            avg = self._wait_for_order_completion(order_id, symbol)
            if avg is not False:
                return avg
            print(f"Retrying exit for {symbol} (leg {leg}), attempt {attempt}...")
        return None

    # ---------------- strategy steps ----------------

    def _refresh_candidates(self, leg):
        try:
            self._candidates[leg] = list_option_candidates(self.kite, self.index, self.expiry, leg)
        except Exception as e:
            self._set(error_message=f"{leg} candidate list error: {e}")
            self._candidates[leg] = []

    def _try_enter_or_reenter(self, leg):
        state = self.legs[leg]

        if state["phase"] == "reentry_wait":
            # Same strike only -- just poll that one symbol until its
            # premium is back in band (tight band first, then fallback).
            symbol = state["symbol"]
            try:
                ltp = self._get_ltp_map([symbol]).get(symbol)
            except Exception as e:
                self._set(error_message=f"{leg} LTP error: {e}")
                return
            if ltp is None:
                return
            in_tight = in_premium_band(ltp, self.premium_target, self.premium_tolerance)
            in_fallback = in_premium_band(ltp, self.premium_target, self.premium_fallback_tolerance)
            if not (in_tight or in_fallback):
                return  # not back in band (tight or fallback) yet
            candidate_symbol = symbol
        else:
            if not self._candidates[leg]:
                self._refresh_candidates(leg)
                if not self._candidates[leg]:
                    return
            try:
                ltp_map = self._get_ltp_map([c["symbol"] for c in self._candidates[leg]])
            except Exception as e:
                self._set(error_message=f"{leg} LTP error: {e}")
                return
            enriched = [dict(c, ltp=ltp_map[c["symbol"]]) for c in self._candidates[leg] if c["symbol"] in ltp_map]

            # Tight band (e.g. 55-65) first -- picks the strike closest to target.
            pick = pick_strike_in_band(enriched, self.premium_target, self.premium_tolerance)
            if pick is None:
                # Nothing in the tight band this poll -- widen to the
                # fallback band (e.g. 50-70) before giving up for now.
                pick = pick_strike_in_band(enriched, self.premium_target, self.premium_fallback_tolerance)
            if pick is None:
                return  # nothing in tight or fallback band this poll
            candidate_symbol = pick["symbol"]
            state["strike"] = pick["strike"]
            state["token"] = pick["token"]

        avg_price = self._sell_leg(leg, candidate_symbol, self.qty)
        if avg_price is None:
            self._set(error_message=f"{leg} entry order failed for {candidate_symbol}, will retry next poll.")
            return

        state["phase"] = "in_position"
        state["symbol"] = candidate_symbol
        state["qty"] = self.qty
        state["entry_premium"] = avg_price
        state["current_premium"] = avg_price
        state["entry_time"] = datetime.now(IST).isoformat()
        print(f"[{leg}] ENTERED {candidate_symbol} qty={self.qty} @ {avg_price}")

    def _monitor_open_leg(self, leg):
        state = self.legs[leg]
        symbol = state["symbol"]
        try:
            ltp = self._get_ltp_map([symbol]).get(symbol)
        except Exception as e:
            self._set(error_message=f"{leg} LTP error: {e}")
            return
        if ltp is None:
            return
        state["current_premium"] = ltp

        if sell_leg_hit_stop_loss(state["entry_premium"], ltp):
            self._close_leg(leg, phase_on_close=None, reason_label="STOP LOSS")

    def _close_leg(self, leg, phase_on_close, reason_label):
        """
        phase_on_close: the terminal phase to set for a non-SL close
            ("closed_target" / "closed_manual" / "closed_eod"). Pass
            None for an SL close, since that branches into
            "reentry_wait" or "closed_permanent" depending on history.
        """
        state = self.legs[leg]
        symbol, qty = state["symbol"], state["qty"]
        exit_price = self._buy_to_close(leg, symbol, qty)
        if exit_price is None:
            self._set(error_message=f"{leg} exit order failed for {symbol} -- MANUAL ACTION REQUIRED.")
            state["phase"] = "error"
            return

        pnl = sell_leg_pnl(state["entry_premium"], exit_price, qty)
        state["realized_pnl"] += pnl
        state["current_premium"] = exit_price
        print(f"[{leg}] EXITED {symbol} ({reason_label}) pnl={pnl:.2f}")

        if reason_label == "STOP LOSS":
            state["sl_count"] += 1
            if not state["reentry_used"]:
                state["reentry_used"] = True
                state["phase"] = "reentry_wait"
                # keep symbol/strike/token -- re-entry watches the SAME strike
            else:
                state["phase"] = "closed_permanent"
                state["symbol"] = None
        else:
            state["phase"] = phase_on_close
            state["symbol"] = None

    def _combined_pnl(self):
        return combined_pnl(self.legs)

    def _open_legs(self):
        return [leg for leg, s in self.legs.items() if s["phase"] == "in_position"]

    def _active_legs(self):
        return [leg for leg, s in self.legs.items() if s["phase"] in ("searching", "in_position", "reentry_wait")]

    # ---------------- main loop ----------------

    def _run(self):
        self._set(status="waiting_for_market", started_at=datetime.now(IST).isoformat(), error_message=None)

        while not self._is_market_open():
            if self._manual_stop.is_set():
                self._finish_before_entry()
                return
            time.sleep(5)

        self._set(status="searching")

        while True:
            if self._manual_stop.is_set():
                if self._stop_only:
                    self._set(status="stopped", exit_reason="ALGORITHM STOPPED",
                              ended_at=datetime.now(IST).isoformat())
                    print("Algorithm stopped. Any open positions remain open.")
                    return
                if self._exit_requested:
                    for leg in self._open_legs():
                        self._close_leg(leg, phase_on_close="closed_manual", reason_label="MANUAL EXIT")
                    self.total_pnl = self._combined_pnl()
                    self._set(status="exited", exit_reason="MANUAL EXIT",
                              total_pnl=self.total_pnl, ended_at=datetime.now(IST).isoformat())
                    return

            if not self._is_market_open():
                time.sleep(30)
                continue

            self._update_index_ltp()

            if self._past_square_off():
                for leg in self._open_legs():
                    self._close_leg(leg, phase_on_close="closed_eod", reason_label="EOD SQUARE-OFF")
                self.total_pnl = self._combined_pnl()
                self._set(status="exited", exit_reason="EOD SQUARE-OFF",
                          total_pnl=self.total_pnl, ended_at=datetime.now(IST).isoformat())
                return

            for leg in ("CE", "PE"):
                phase = self.legs[leg]["phase"]
                if phase in ("searching", "reentry_wait"):
                    self._try_enter_or_reenter(leg)
                elif phase == "in_position":
                    self._monitor_open_leg(leg)

            self.total_pnl = self._combined_pnl()
            self._set(total_pnl=self.total_pnl, legs={k: dict(v) for k, v in self.legs.items()})

            if self.target_profit is not None and self.total_pnl >= self.target_profit:
                for leg in self._open_legs():
                    self._close_leg(leg, phase_on_close="closed_target", reason_label="TARGET PROFIT")
                self.total_pnl = self._combined_pnl()
                self._set(status="exited", exit_reason="TARGET PROFIT HIT",
                          total_pnl=self.total_pnl, ended_at=datetime.now(IST).isoformat())
                return

            if not self._active_legs():
                self._set(status="exited", exit_reason="BOTH LEGS PERMANENTLY STOPPED OUT",
                          total_pnl=self.total_pnl, ended_at=datetime.now(IST).isoformat())
                return

            self._set(status="monitoring" if self._open_legs() else "searching")
            time.sleep(POLL_INTERVAL)

    def _finish_before_entry(self):
        if self._stop_only:
            self._set(status="stopped", exit_reason="ALGORITHM STOPPED (before entry)",
                      ended_at=datetime.now(IST).isoformat())
        elif self._exit_requested:
            self._set(status="exited", exit_reason="MANUAL EXIT (before entry)",
                      ended_at=datetime.now(IST).isoformat())