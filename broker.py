"""Thin wrapper around the Alpaca API: market data + order execution."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from alpaca.common.enums import Sort
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

import config


class Broker:
    def __init__(self):
        self.trading = TradingClient(config.API_KEY, config.SECRET_KEY, paper=config.PAPER)
        self.data = StockHistoricalDataClient(config.API_KEY, config.SECRET_KEY)

    def account_summary(self):
        acct = self.trading.get_account()
        return {
            "equity": float(acct.equity),
            "cash": float(acct.cash),
            "buying_power": float(acct.buying_power),
            "paper": config.PAPER,
        }

    def is_market_open(self):
        clock = self.trading.get_clock()
        return clock.is_open

    def get_position_qty(self, symbol):
        try:
            pos = self.trading.get_open_position(symbol)
            return float(pos.qty)
        except Exception:
            return 0.0

    def minutes_to_close(self):
        clock = self.trading.get_clock()
        return (clock.next_close - clock.timestamp).total_seconds() / 60

    def minutes_since_open(self):
        clock = self.trading.get_clock()
        if not clock.is_open:
            return None
        ny = ZoneInfo("America/New_York")
        now_ny = clock.timestamp.astimezone(ny)
        today_open = now_ny.replace(hour=9, minute=30, second=0, microsecond=0)
        return (now_ny - today_open).total_seconds() / 60

    def recent_bars(self, symbol, lookback=60):
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Minute,
            start=datetime.now(timezone.utc) - timedelta(days=3),
            limit=lookback,
            sort=Sort.DESC,
        )
        rows = self.data.get_stock_bars(req).data.get(symbol, [])
        return [
            {"open": float(b.open), "high": float(b.high), "low": float(b.low), "close": float(b.close), "t": b.timestamp}
            for b in reversed(rows)
        ]

    def daily_closes(self, symbol, days=120):
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Day,
            start=datetime.now(timezone.utc) - timedelta(days=days),
        )
        rows = self.data.get_stock_bars(req).data.get(symbol, [])
        return [float(b.close) for b in rows]

    def daily_bars(self, symbol, days=120):
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Day,
            start=datetime.now(timezone.utc) - timedelta(days=days),
        )
        rows = self.data.get_stock_bars(req).data.get(symbol, [])
        return [(float(b.open), float(b.close)) for b in rows]

    def get_position(self, symbol):
        try:
            p = self.trading.get_open_position(symbol)
            return {
                "qty": float(p.qty),
                "entry": float(p.avg_entry_price),
                "cost": float(p.cost_basis),
                "value": float(p.market_value),
                "pnl": float(p.unrealized_pl),
            }
        except Exception:
            return None

    def get_entry_price(self, symbol):
        try:
            return float(self.trading.get_open_position(symbol).avg_entry_price)
        except Exception:
            return None

    def recent_closes(self, symbol, lookback=30):
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Minute,
            start=datetime.now(timezone.utc) - timedelta(days=7),
            limit=lookback,
            sort=Sort.DESC,
        )
        bars = self.data.get_stock_bars(req)
        rows = bars.data.get(symbol, [])
        return [float(b.close) for b in reversed(rows)]

    def buy_notional(self, symbol, dollars):
        order = MarketOrderRequest(
            symbol=symbol,
            notional=round(dollars, 2),
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
        )
        return self.trading.submit_order(order)

    def sell_all(self, symbol):
        qty = self.get_position_qty(symbol)
        if qty <= 0:
            return None
        order = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        )
        return self.trading.submit_order(order)
