from decimal import Decimal
from hummingbot.script.script_base import ScriptBase
from hummingbot.core.data_type.order_type import OrderType


#
# --------- USER CONFIG SECTION ----------
#

# Which connector + pair this script should guard
CONNECTOR_NAME = "kraken"          # e.g. "kraken", "binance", "coinbase_pro"
TRADING_PAIR   = "SOL-USDT"        # e.g. "SOL-USDT", "BTC-USDT"

# Hard stop price (absolute) – script will only trigger around this level
STOP_PRICE     = Decimal("140")    # change this to your stop level

# How much price must bounce ABOVE stop after first touch to count as a bounce
# 0.003 = 0.3%, 0.005 = 0.5%, 0.01 = 1%
BOUNCE_BUFFER  = Decimal("0.003")

# How long (in seconds) the stop can stay "armed" before auto-reset (no trigger)
MAX_ARM_SECONDS = 300  # 5 minutes – tweak to taste

# Min base balance to consider as a "position" (so dust doesn't trigger anything)
MIN_BASE_TO_CLOSE = Decimal("0.01")

# Use market orders for the exit
USE_MARKET_ORDER = True

#
# ------------- END CONFIG ---------------
#


class SecondTouchStopScript(ScriptBase):
    """
    Second-touch stop-loss controller.

    Behaviour (for LONGs):
        - Detects if you hold >= MIN_BASE_TO_CLOSE of the base asset.
        - First time price <= STOP_PRICE -> state = "armed" (no sell yet).
        - If price then bounces >= STOP_PRICE * (1 + BOUNCE_BUFFER), we mark 'bounced=True'.
        - If after that bounce, price comes back down to <= STOP_PRICE again -> we market-sell all base.
        - If nothing happens within MAX_ARM_SECONDS, we reset back to idle.

    This is meant to run as a protective overlay while you manage entries separately.
    """

    # Tell Hummingbot which markets to initialize
    markets = {
        CONNECTOR_NAME: {TRADING_PAIR}
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # State machine variables
        self.state = "idle"              # "idle", "armed", "triggered"
        self.arm_ts = None               # timestamp when we armed
        self.bounced = False             # whether we've confirmed a bounce after first touch
        self.high_after_arm = None       # highest price seen since arming
        self.base_asset, self.quote_asset = TRADING_PAIR.split("-")

    #
    # --------- CORE LOOP ----------
    #

    def on_tick(self):
        """
        Called every tick by Hummingbot.
        """
        connector = self.connectors[CONNECTOR_NAME]

        try:
            # Mid price (or best ask/bid; Hummingbot treats True as mid in get_price)
            last_price = Decimal(str(connector.get_price(TRADING_PAIR, True)))
        except Exception:
            # If price fetch fails for whatever reason, skip this tick
            return

        # How much base we currently hold
        base_balance = Decimal(str(connector.get_balance(self.base_asset)))
        now = Decimal(str(self.current_timestamp))

        #
        # 1. No position => reset state
        #
        if base_balance < MIN_BASE_TO_CLOSE:
            if self.state != "idle":
                self.logger().info(
                    f"[SecondTouchStop] No meaningful {self.base_asset} balance ("
                    f"{base_balance}); resetting to idle."
                )
            self._reset_state()
            return

        #
        # 2. We do have a position => run the state machine
        #
        if self.state == "idle":
            self._handle_idle(last_price, now)

        elif self.state == "armed":
            self._handle_armed(last_price, now, base_balance)

        elif self.state == "triggered":
            self._handle_triggered(last_price, base_balance)

    #
    # --------- STATE HANDLERS ----------
    #

    def _handle_idle(self, last_price: Decimal, now: Decimal):
        """
        IDLE:
          - Waiting for first touch of STOP_PRICE.
        """
        if last_price <= STOP_PRICE:
            self.state = "armed"
            self.arm_ts = now
            self.bounced = False
            self.high_after_arm = last_price

            self.logger().info(
                f"[SecondTouchStop] FIRST TOUCH: price {last_price} <= stop {STOP_PRICE}. "
                f"Stop ARMED. Waiting for bounce..."
            )

    def _handle_armed(self, last_price: Decimal, now: Decimal, base_balance: Decimal):
        """
        ARMED:
          - We saw price touch the stop once.
          - We want to see a bounce ABOVE stop_price * (1 + BOUNCE_BUFFER).
          - After that bounce, a second touch at or below STOP_PRICE will close us.
        """

        # Update highest price since arming
        if self.high_after_arm is None or last_price > self.high_after_arm:
            self.high_after_arm = last_price

        # Check for bounce
        bounce_threshold = STOP_PRICE * (Decimal("1") + BOUNCE_BUFFER)
        if not self.bounced and self.high_after_arm >= bounce_threshold:
            self.bounced = True
            self.logger().info(
                f"[SecondTouchStop] BOUNCE CONFIRMED: high {self.high_after_arm} >= "
                f"bounce threshold {bounce_threshold}. Waiting for second touch..."
            )

        # Second touch: after bounce, price back at/below stop => close
        if self.bounced and last_price <= STOP_PRICE:
            self.logger().info(
                f"[SecondTouchStop] SECOND TOUCH TRIGGER: price {last_price} <= stop {STOP_PRICE}. "
                f"Closing LONG position of {base_balance} {self.base_asset}."
            )
            self._close_long_position(base_balance)
            self.state = "triggered"
            return

        # Timeout: if we've been armed too long, reset
        if self.arm_ts is not None and (now - self.arm_ts) > MAX_ARM_SECONDS:
            self.logger().info(
                f"[SecondTouchStop] ARM TIMEOUT: {now - self.arm_ts}s > {MAX_ARM_SECONDS}s. "
                f"Resetting to idle."
            )
            self._reset_state()

    def _handle_triggered(self, last_price: Decimal, base_balance: Decimal):
        """
        TRIGGERED:
          - We've already sent the close order.
          - Wait until position is basically gone, then go back to idle so we can
            protect future positions.
        """
        if base_balance < MIN_BASE_TO_CLOSE:
            self.logger().info(
                f"[SecondTouchStop] Position closed (balance {base_balance}). "
                f"Returning to idle state."
            )
            self._reset_state()
        else:
            # Still holding base; maybe order only partially filled or exchange slow.
            # Could add safety logic here if you want to re-send orders, etc.
            pass

    #
    # --------- HELPERS ----------
    #

    def _reset_state(self):
        self.state = "idle"
        self.arm_ts = None
        self.bounced = False
        self.high_after_arm = None

    def _close_long_position(self, base_balance: Decimal):
        connector = self.connectors[CONNECTOR_NAME]
        amount_to_sell = base_balance

        # You could optionally subtract a tiny dust buffer:
        # amount_to_sell = max(Decimal("0"), base_balance - Decimal("0.00001"))

        try:
            if USE_MARKET_ORDER:
                self.logger().info(
                    f"[SecondTouchStop] Sending MARKET SELL {amount_to_sell} {self.base_asset} "
                    f"on {CONNECTOR_NAME} {TRADING_PAIR}."
                )
                connector.sell(
                    TRADING_PAIR,
                    amount_to_sell,
                    order_type=OrderType.MARKET
                )
            else:
                # Limit exit at STOP_PRICE if you really want (more slippage risk)
                self.logger().info(
                    f"[SecondTouchStop] Sending LIMIT SELL {amount_to_sell} {self.base_asset} "
                    f"at {STOP_PRICE} on {CONNECTOR_NAME} {TRADING_PAIR}."
                )
                connector.sell(
                    TRADING_PAIR,
                    amount_to_sell,
                    order_type=OrderType.LIMIT,
                    price=STOP_PRICE
                )
        except Exception as e:
            self.logger().error(
                f"[SecondTouchStop] ERROR sending close order: {e}", exc_info=True
            )
