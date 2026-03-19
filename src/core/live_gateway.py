"""Live Kalshi order gateway.

Implements the ``ExecutionEngine`` ABC for real order placement on the
Kalshi V2 API.  Supports limit and market orders with order lifecycle
tracking.

Sprint 4, Task 4.3 of Money Printer V2.

Safety
------
- **Demo by default**: uses ``demo-api.kalshi.co`` unless explicitly
  switched to production with ``use_production=True``.
- **Read-only guard**: rejects all writes if the underlying provider
  is in ``read_only`` mode.
"""

import logging
from datetime import datetime
from typing import List, Optional

from src.core.interfaces import ExecutionEngine, TradeSignal

logger = logging.getLogger(__name__)


class LiveKalshiGateway(ExecutionEngine):
    """Places real orders on Kalshi via the V2 REST API.

    Parameters
    ----------
    kalshi_provider : KalshiProvider
        Authenticated provider with RSA signing.
    use_production : bool
        If ``True``, orders go to the real production API.
        Default ``False`` routes to the demo sandbox.
    """

    DEMO_API = "https://demo-api.kalshi.co/trade-api/v2"
    PROD_API = "https://api.elections.kalshi.com/trade-api/v2"

    def __init__(self, kalshi_provider, use_production: bool = False):
        self._provider = kalshi_provider
        self.use_production = use_production
        self._api_url = self.PROD_API if use_production else self.DEMO_API

        # Order history for audit trail
        self._orders: List[dict] = []
        self._order_counter = 0

    # ------------------------------------------------------------------
    # ExecutionEngine interface
    # ------------------------------------------------------------------

    def execute(self, signal: TradeSignal) -> bool:
        """Place an order on Kalshi for the given signal.

        Returns ``True`` if the order was accepted by the API.
        """
        if self._provider.read_only:
            logger.warning("[LiveGateway] BLOCKED: provider is read-only")
            return False

        if self._provider.anonymous:
            logger.warning("[LiveGateway] BLOCKED: provider is anonymous (no auth)")
            return False

        contract_side = getattr(signal, "contract_side", "YES").lower()
        order_type = "limit" if signal.limit_price else "market"

        order = self._build_order(
            ticker=signal.symbol,
            side=contract_side,
            action="buy" if signal.side == "buy" else "sell",
            order_type=order_type,
            count=signal.quantity,
            price=signal.limit_price,
        )

        return self._submit_order(order)

    # ------------------------------------------------------------------
    # Order management
    # ------------------------------------------------------------------

    def place_limit_order(
        self,
        ticker: str,
        side: str,
        action: str,
        count: int,
        price: float,
    ) -> Optional[dict]:
        """Place a limit order.

        Parameters
        ----------
        ticker : str
            Market ticker (e.g., ``KXBTC15M-26MAR19-1430-T84000``).
        side : str
            ``"yes"`` or ``"no"``.
        action : str
            ``"buy"`` or ``"sell"``.
        count : int
            Number of contracts.
        price : float
            Limit price in dollars (0-1).
        """
        order = self._build_order(ticker, side, action, "limit", count, price)
        if self._submit_order(order):
            return order
        return None

    def place_market_order(
        self,
        ticker: str,
        side: str,
        action: str,
        count: int,
    ) -> Optional[dict]:
        """Place a market order (taker)."""
        order = self._build_order(ticker, side, action, "market", count, None)
        if self._submit_order(order):
            return order
        return None

    def cancel_order(self, order_id: str) -> bool:
        """Cancel a pending order."""
        if self._provider.read_only or self._provider.anonymous:
            return False

        path = f"/portfolio/orders/{order_id}"
        url = f"{self._api_url}{path}"
        headers = self._provider._get_authenticated_headers("DELETE", path)

        try:
            resp = self._provider.session.delete(url, headers=headers, timeout=10)
            if resp.status_code in (200, 204):
                logger.info("[LiveGateway] Order %s cancelled", order_id)
                return True
            logger.warning(
                "[LiveGateway] Cancel failed: %s %s", resp.status_code, resp.text
            )
            return False
        except Exception as exc:
            logger.error("[LiveGateway] Cancel error: %s", exc)
            return False

    def get_positions(self) -> List[dict]:
        """Fetch current positions from Kalshi API."""
        if self._provider.anonymous:
            return []

        path = "/portfolio/positions"
        url = f"{self._api_url}{path}"
        headers = self._provider._get_authenticated_headers("GET", path)

        try:
            resp = self._provider.session.get(url, headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            return data.get("market_positions", [])
        except Exception as exc:
            logger.error("[LiveGateway] Positions fetch failed: %s", exc)
            return []

    def get_balance(self) -> float:
        """Fetch live balance from Kalshi."""
        return self._provider.get_balance()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _build_order(
        self,
        ticker: str,
        side: str,
        action: str,
        order_type: str,
        count: int,
        price: Optional[float],
    ) -> dict:
        """Build an order dict matching Kalshi V2 API schema."""
        self._order_counter += 1
        order = {
            "internal_id": self._order_counter,
            "ticker": ticker,
            "side": side,
            "action": action,
            "type": order_type,
            "count": count,
            "created_at": datetime.now().isoformat(),
            "status": "pending",
        }
        if price is not None:
            # Kalshi V2 API expects price in cents (integer)
            order["yes_price"] = int(round(price * 100))
            order["no_price"] = 100 - int(round(price * 100))
        return order

    def _submit_order(self, order: dict) -> bool:
        """Submit an order to the Kalshi API."""
        if self._provider.read_only:
            logger.warning("[LiveGateway] BLOCKED: read-only mode")
            order["status"] = "rejected_read_only"
            self._orders.append(order)
            return False

        path = "/portfolio/orders"
        url = f"{self._api_url}{path}"
        headers = self._provider._get_authenticated_headers("POST", path)

        body = {
            "ticker": order["ticker"],
            "side": order["side"],
            "action": order["action"],
            "type": order["type"],
            "count": order["count"],
        }
        if "yes_price" in order:
            body["yes_price"] = order["yes_price"]
            body["no_price"] = order["no_price"]

        try:
            resp = self._provider.session.post(
                url, headers=headers, json=body, timeout=15
            )
            resp_data = resp.json() if resp.text else {}

            if resp.status_code in (200, 201):
                api_order = resp_data.get("order", {})
                order["api_order_id"] = api_order.get("order_id")
                order["status"] = api_order.get("status", "accepted")
                order["response"] = api_order
                self._orders.append(order)
                logger.info(
                    "[LiveGateway] ORDER PLACED: %s %s %s %dx %s @ %s",
                    order["action"],
                    order["side"],
                    order["type"],
                    order["count"],
                    order["ticker"],
                    order.get("yes_price", "market"),
                )
                return True

            order["status"] = "rejected"
            order["error"] = resp_data
            self._orders.append(order)
            logger.warning(
                "[LiveGateway] ORDER REJECTED: %s — %s",
                resp.status_code,
                resp_data,
            )
            return False

        except Exception as exc:
            order["status"] = "error"
            order["error"] = str(exc)
            self._orders.append(order)
            logger.error("[LiveGateway] Order submission error: %s", exc)
            return False

    def get_order_history(self) -> List[dict]:
        """Return all orders placed during this session."""
        return list(self._orders)
