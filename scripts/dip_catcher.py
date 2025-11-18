# dip_catcher.py (CRASH-DEPTH TUNED, API CORRECTED)
# Hummingbot custom script: laddered buys on dips, auto TP sells on bounces
# Place in: hummingbot/scripts/dip_catcher.py
# Run: hummingbot > import script dip_catcher > start script dip_catcher

from decimal import Decimal
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Set
from hummingbot.script.script_base import ScriptStrategyBase
import time


@dataclass
class MarketParams:
    connector: str
    trading_pair: str
    budget: Decimal
    buy_levels: List[Decimal]
    buy_split: List[Decimal]
    tp_levels: List[Decimal]
    tp_split: List[Decimal]
    min_order_size: Decimal = Decimal("10")
    cancel_after_sec: int = 600
    cooldown_sec: int = 10
    emergency_stop: Optional[Decimal] = None


@dataclass
class ScriptConfig:
    markets: List[MarketParams] = field(default_factory=list)
    tick_interval: float = 1.0


DEFAULT_CONFIG = ScriptConfig(
    markets=[
        # ETH (anchor, deeper rungs, back-weighted)
        MarketParams(
            connector="kraken",
            trading_pair="ETH-USDC",
            budget=Decimal("220"),
            buy_levels=[Decimal("0.03"), Decimal("0.06"), Decimal("0.10")],
            buy_split=[Decimal("0.30"), Decimal("0.35"), Decimal("0.35")],
            tp_levels=[Decimal("0.04"), Decimal("0.08")],
            tp_split=[Decimal("0.65"), Decimal("0.35")],
            min_order_size=Decimal("10"),
            cancel_after_sec=600,
            cooldown_sec=8,
            emergency_stop=Decimal("0.12"),
        ),
        # SOL (growth engine, deeper rungs)
        MarketParams(
            connector="kraken",
            trading_pair="SOL-USDC",
            budget=Decimal("220"),
            buy_levels=[Decimal("0.04"), Decimal("0.08"), Decimal("0.12")],
            buy_split=[Decimal("0.30"), Decimal("0.35"), Decimal("0.35")],
            tp_levels=[Decimal("0.045"), Decimal("0.09")],
            tp_split=[Decimal("0.65"), Decimal("0.35")],
            min_order_size=Decimal("10"),
            cancel_after_sec=600,
            cooldown_sec=10,
            emergency_stop=Decimal("0.13"),
        ),
        # TAO (high beta, controlled)
        MarketParams(
            connector="kraken",
            trading_pair="TAO-USD",
            budget=Decimal("160"),
            buy_levels=[Decimal("0.09"), Decimal("0.14"), Decimal("0.20")],
            buy_split=[Decimal("0.30"), Decimal("0.35"), Decimal("0.35")],
            tp_levels=[Decimal("0.09"), Decimal("0.15")],
            tp_split=[Decimal("0.60"), Decimal("0.40")],
            min_order_size=Decimal("20"),
            cancel_after_sec=600,
            cooldown_sec=12,
            emergency_stop=Decimal("0.22"),
        ),
        # BTC (ballast / hedge)
        MarketParams(
            connector="kraken",
            trading_pair="BTC-USDC",
            budget=Decimal("50"),
            buy_levels=[Decimal("0.02"), Decimal("0.035"), Decimal("0.06")],
            buy_split=[Decimal("0.30"), Decimal("0.35"), Decimal("0.35")],
            tp_levels=[Decimal("0.025"), Decimal("0.045")],
            tp_split=[Decimal("0.60"), Decimal("0.40")],
            min_order_size=Decimal("10"),
            cancel_after_sec=600,
            cooldown_sec=8,
            emergency_stop=Decimal("0.08"),
        ),
    ],
    tick_interval=1.0,
)


class DipCatcher(ScriptStrategyBase):
    """
    Laddered limit buys on dips, automatic TP sells on bounces.
    - Places resting buys at % offsets below mid-price
    - On fill, splits acquired base into TP sell orders above fill price
    - Periodically refreshes buy orders to follow mid-price
    - Optional emergency stop if market tanks further after fill
    """

    markets_config: ScriptConfig = DEFAULT_CONFIG

    def __init__(self):
        super().__init__()
        self._last_refresh_ts: Dict[str, float] = {}
        self._cooldown_ts: Dict[str, float] = {}  # Per-market cooldown
        # Track buy orders per market: key -> list of order_ids
        self._active_buy_orders: Dict[str, Set[str]] = {}
        # Track entry prices and base acquired per market
        self._entry_tracking: Dict[str, Dict] = {}  # key -> {entry_price, base_filled}
        self.logger.info("DipCatcher initialized")

    @classmethod
    def init_params(cls, config: ScriptConfig):
        cls.markets_config = config

    def on_tick(self):
        """Main loop: refresh buys, check fills, place TPs, check emergency stop."""
        now = time.time()

        for mp in self.markets_config.markets:
            key = f"{mp.connector}|{mp.trading_pair}"

            try:
                # Ensure market is added
                self.add_market(mp.connector, [mp.trading_pair])

                # Get mid price
                mid_price = self._get_mid_price(mp)
                if mid_price is None:
                    continue

                # Per-market cooldown check
                if now < self._cooldown_ts.get(key, 0.0):
                    continue

                # Refresh buy ladder if time
                last_refresh = self._last_refresh_ts.get(key, 0.0)
                if now - last_refresh >= mp.cancel_after_sec:
                    self.logger.info(
                        f"[{key}] Refreshing buy ladder at mid={mid_price}"
                    )
                    self._cancel_buy_orders(mp)
                    self._place_buy_ladder(mp, mid_price)
                    self._last_refresh_ts[key] = now
                    self._cooldown_ts[key] = now + mp.cooldown_sec
                    continue

                # Check for fills and place TPs if needed
                self._place_take_profits_if_needed(mp, mid_price)

                # Check emergency stop
                self._check_emergency_stop(mp, mid_price)

            except Exception as e:
                self.logger.error(f"Error in on_tick for {key}: {e}")
                continue

        self.tick_interval = self.markets_config.tick_interval

    # ==================== HELPER METHODS ====================

    def _get_mid_price(self, mp: MarketParams) -> Optional[Decimal]:
        """Get mid price from order book."""
        try:
            connector = self.connectors.get(mp.connector)
            if connector is None:
                self.logger.warning(f"Connector {mp.connector} not found")
                return None
            # Use order_book mid price if available
            order_book = connector.order_book_bid_asks(mp.trading_pair)
            if order_book is None or len(order_book) == 0:
                return None
            best_bid = order_book[0].bid_price
            best_ask = order_book[0].ask_price
            if best_bid is None or best_ask is None:
                return None
            mid = (best_bid + best_ask) / Decimal("2")
            return mid
        except Exception as e:
            self.logger.error(f"Failed to get mid price for {mp.trading_pair}: {e}")
            return None

    def _place_buy_ladder(self, mp: MarketParams, mid: Decimal):
        """
        Place laddered limit buy orders below mid price.
        Back-weighted sizing: more at lower prices.
        """
        key = f"{mp.connector}|{mp.trading_pair}"
        self._active_buy_orders[key] = set()

        # Normalize split weights
        total_split = sum(mp.buy_split)
        if total_split == 0:
            self.logger.error(f"[{key}] buy_split sums to 0")
            return

        weights = [w / total_split for w in mp.buy_split]

        for i, (offset, weight) in enumerate(zip(mp.buy_levels, weights)):
            quote_amount = (mp.budget * weight).quantize(Decimal("0.00000001"))

            # Skip if below minimum
            if quote_amount < mp.min_order_size:
                self.logger.warning(
                    f"[{key}] Level {i}: quote {quote_amount} < min {mp.min_order_size}"
                )
                continue

            # Calculate price and amount
            price = (mid * (Decimal("1") - offset)).quantize(Decimal("0.00000001"))
            amount = (quote_amount / price).quantize(Decimal("0.00000001"))

            if amount <= Decimal("0"):
                self.logger.warning(f"[{key}] Level {i}: amount is 0")
                continue

            try:
                # Place buy order
                order = self.buy(
                    connector_name=mp.connector,
                    trading_pair=mp.trading_pair,
                    amount=amount,
                    order_type="limit",
                    price=price,
                )
                if order is not None:
                    order_id = order.client_order_id
                    self._active_buy_orders[key].add(order_id)
                    self.logger.info(
                        f"[{key}] Buy L{i}: {amount} @ {price} (offset {offset*100}%) | ID: {order_id}"
                    )
                else:
                    self.logger.warning(f"[{key}] Buy order returned None")
            except Exception as e:
                self.logger.error(f"[{key}] Failed to place buy order: {e}")

    def _cancel_buy_orders(self, mp: MarketParams):
        """Cancel all active buy orders for this market."""
        key = f"{mp.connector}|{mp.trading_pair}"
        order_ids = self._active_buy_orders.get(key, set()).copy()

        if not order_ids:
            return

        for order_id in order_ids:
            try:
                # Cancel via connector
                self.cancel_order(mp.connector, mp.trading_pair, order_id)
                self.logger.info(f"[{key}] Cancelled buy order: {order_id}")
                self._active_buy_orders[key].discard(order_id)
            except Exception as e:
                self.logger.error(f"[{key}] Failed to cancel {order_id}: {e}")

    def _place_take_profits_if_needed(self, mp: MarketParams, mid: Decimal):
        """
        Check if we hold base. If so, and no active sells, place TP ladder.
        TPs placed relative to current mid (conservative proxy for entry).
        """
        key = f"{mp.connector}|{mp.trading_pair}"
        connector = self.connectors[mp.connector]

        # Get base balance
        base, quote = mp.trading_pair.split("-")
        try:
            base_bal = connector.get_available_balance(base)
        except Exception as e:
            self.logger.error(f"[{key}] Failed to get balance for {base}: {e}")
            return

        if base_bal is None or base_bal <= Decimal("0.00001"):
            return

        # Check if active sell orders exist
        try:
            active_orders = self.get_active_orders(mp.connector, mp.trading_pair)
            active_sells = [o for o in active_orders if o.is_sell or not o.is_buy]
            if active_sells:
                return  # Already have sells posted
        except Exception as e:
            self.logger.error(f"[{key}] Failed to check active orders: {e}")
            return

        # Normalize TP split
        total_tp_split = sum(mp.tp_split)
        if total_tp_split == 0:
            self.logger.error(f"[{key}] tp_split sums to 0")
            return
        tp_weights = [w / total_tp_split for w in mp.tp_split]

        # Place TP ladder
        self.logger.info(
            f"[{key}] Placing TP ladder for {base_bal} base at mid {mid}"
        )
        for i, (offset, weight) in enumerate(zip(mp.tp_levels, tp_weights)):
            tp_amount = (base_bal * weight).quantize(Decimal("0.00000001"))
            if tp_amount <= Decimal("0"):
                continue

            tp_price = (mid * (Decimal("1") + offset)).quantize(
                Decimal("0.00000001")
            )

            try:
                order = self.sell(
                    connector_name=mp.connector,
                    trading_pair=mp.trading_pair,
                    amount=tp_amount,
                    order_type="limit",
                    price=tp_price,
                )
                if order is not None:
                    order_id = order.client_order_id
                    self.logger.info(
                        f"[{key}] TP L{i}: {tp_amount} @ {tp_price} (offset +{offset*100}%) | ID: {order_id}"
                    )
                else:
                    self.logger.warning(f"[{key}] TP order returned None")
            except Exception as e:
                self.logger.error(f"[{key}] Failed to place TP order: {e}")

    def _check_emergency_stop(self, mp: MarketParams, mid: Decimal):
        """
        If emergency_stop is set and we hold base, check if price has dropped
        beyond the threshold. If so, market-sell all base immediately.
        """
        if mp.emergency_stop is None:
            return

        key = f"{mp.connector}|{mp.trading_pair}"
        connector = self.connectors[mp.connector]

        # Get base balance
        base, quote = mp.trading_pair.split("-")
        try:
            base_bal = connector.get_available_balance(base)
        except Exception as e:
            self.logger.error(f"[{key}] Failed to get balance for emergency stop: {e}")
            return

        if base_bal is None or base_bal <= Decimal("0.00001"):
            return

        # Calculate stop price
        stop_price = (mid * (Decimal("1") - mp.emergency_stop)).quantize(
            Decimal("0.00000001")
        )

        # Check if current price <= stop price
        # Use mid as proxy (could use last_price if available)
        if mid <= stop_price:
            self.logger.critical(
                f"[{key}] EMERGENCY STOP TRIGGERED: mid {mid} <= stop {stop_price}. "
                f"Market-selling {base_bal} {base}"
            )
            try:
                order = self.sell(
                    connector_name=mp.connector,
                    trading_pair=mp.trading_pair,
                    amount=base_bal,
                    order_type="market",
                )
                if order is not None:
                    self.logger.critical(
                        f"[{key}] Emergency market sell executed: {order.client_order_id}"
                    )
            except Exception as e:
                self.logger.error(f"[{key}] Emergency stop market sell failed: {e}")


# ==================== CONFIGURATION VALIDATION ====================

def validate_config(config: ScriptConfig):
    """Validate configuration for obvious errors."""
    for mp in config.markets:
        # Check split sums
        if abs(sum(mp.buy_split) - 1.0) > 0.001:
            raise ValueError(
                f"[{mp.trading_pair}] buy_split must sum to ~1.0, got {sum(mp.buy_split)}"
            )
        if abs(sum(mp.tp_split) - 1.0) > 0.001:
            raise ValueError(
                f"[{mp.trading_pair}] tp_split must sum to ~1.0, got {sum(mp.tp_split)}"
            )
        # Check level counts match
        if len(mp.buy_levels) != len(mp.buy_split):
            raise ValueError(
                f"[{mp.trading_pair}] buy_levels and buy_split length mismatch"
            )
        if len(mp.tp_levels) != len(mp.tp_split):
            raise ValueError(
                f"[{mp.trading_pair}] tp_levels and tp_split length mismatch"
            )


# Validate on import
try:
    validate_config(DEFAULT_CONFIG)
except ValueError as e:
    raise RuntimeError(f"Configuration error: {e}")
