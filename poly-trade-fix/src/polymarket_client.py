"""
Polymarket client - FIXED: Uses limit orders to avoid spread losses.
"""
import os
import json
import requests
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"
CHAIN_ID = 137


class PolymarketClient:
    def __init__(self):
        self.private_key = os.getenv("PRIVATE_KEY")
        self.funder = os.getenv("FUNDER_ADDRESS")
        self.sig_type = int(os.getenv("SIGNATURE_TYPE", "1"))

        if self.funder:
            self.clob = ClobClient(
                CLOB_URL, key=self.private_key, chain_id=CHAIN_ID,
                signature_type=self.sig_type, funder=self.funder,
            )
        else:
            self.clob = ClobClient(
                CLOB_URL, key=self.private_key, chain_id=CHAIN_ID,
            )

        self.clob.set_api_creds(self.clob.create_or_derive_api_creds())
        self._price_cache = {}

    def get_usdc_balance(self):
        try:
            bal = self.clob.get_balance_allowance(asset_type=0)
            if bal and hasattr(bal, 'balance'):
                return float(bal.balance) / 1e6
            if isinstance(bal, dict) and 'balance' in bal:
                return float(bal['balance']) / 1e6
        except Exception:
            pass
        return None

    def get_active_markets(self, limit=50):
        try:
            resp = requests.get(
                f"{GAMMA_URL}/markets",
                params={
                    "active": "true", "closed": "false",
                    "limit": limit, "order": "volume24hr", "ascending": "false",
                },
                timeout=10,
            )
            resp.raise_for_status()
        except Exception as e:
            print(f"   ❌ Erro mercados: {e}")
            return []

        filtered = []
        for m in resp.json():
            parsed = self._parse_market(m)
            if parsed:
                filtered.append(parsed)
                for idx, tok in enumerate(parsed["tokens"]):
                    if idx < len(parsed["prices"]):
                        self._price_cache[tok] = parsed["prices"][idx]
        return filtered

    def get_market_by_id(self, market_id):
        try:
            resp = requests.get(f"{GAMMA_URL}/markets/{market_id}", timeout=10)
            resp.raise_for_status()
            return self._parse_market(resp.json())
        except Exception:
            return None

    def get_orderbook(self, token_id):
        """Get full orderbook with bid/ask and spread."""
        try:
            book = self.clob.get_order_book(token_id)
            best_bid = 0
            best_ask = 1

            if hasattr(book, 'bids') and book.bids:
                best_bid = float(book.bids[0].price)
            elif isinstance(book, dict) and book.get('bids'):
                best_bid = float(book['bids'][0]['price'])

            if hasattr(book, 'asks') and book.asks:
                best_ask = float(book.asks[0].price)
            elif isinstance(book, dict) and book.get('asks'):
                best_ask = float(book['asks'][0]['price'])

            mid = (best_bid + best_ask) / 2 if best_bid > 0 else best_ask
            spread_pct = ((best_ask - best_bid) / mid * 100) if mid > 0 else 100

            self._price_cache[token_id] = mid
            return {
                "mid": mid,
                "bid": best_bid,
                "ask": best_ask,
                "spread_pct": spread_pct,
            }
        except Exception:
            cached = self._price_cache.get(token_id)
            if cached:
                return {"mid": cached, "bid": cached, "ask": cached, "spread_pct": 0}
            return None

    def _parse_market(self, m):
        try:
            volume = float(m.get("volume24hr", 0) or 0)
            if volume < 500:
                return None

            outcomes = m.get("outcomes", "[]")
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)
            tokens = m.get("clobTokenIds", "[]")
            if isinstance(tokens, str):
                tokens = json.loads(tokens)
            prices = m.get("outcomePrices", "[]")
            if isinstance(prices, str):
                prices = json.loads(prices)

            if len(outcomes) < 2 or len(tokens) < 2:
                return None

            float_prices = [float(p) for p in prices if p]

            return {
                "id": m.get("id"),
                "question": m.get("question", ""),
                "description": m.get("description", ""),
                "outcomes": outcomes,
                "tokens": tokens,
                "prices": float_prices,
                "volume_24h": volume,
                "liquidity": float(m.get("liquidity", 0) or 0),
                "end_date": m.get("endDate", ""),
                "category": m.get("category", ""),
            }
        except Exception:
            return None

    # KEY FIX: Buy with limit at BID price (we become the maker, not taker)
    def buy_at_bid(self, token_id, amount_usdc, bid_price):
        """Place limit buy at the bid price. We wait to be filled = no spread loss."""
        size = round(amount_usdc / bid_price, 2)
        order_args = OrderArgs(
            price=round(bid_price, 2), size=size, side=BUY, token_id=token_id,
        )
        signed = self.clob.create_order(order_args)
        return self.clob.post_order(signed, OrderType.GTC)

    # KEY FIX: Sell with limit at ASK price (we wait to be filled)
    def sell_at_ask(self, token_id, size, ask_price):
        """Place limit sell at the ask price. We wait to be filled = no spread loss."""
        order_args = OrderArgs(
            price=round(ask_price, 2), size=round(size, 2), side=SELL, token_id=token_id,
        )
        signed = self.clob.create_order(order_args)
        return self.clob.post_order(signed, OrderType.GTC)

    def cancel_all(self):
        try:
            return self.clob.cancel_all()
        except Exception as e:
            return {"error": str(e)}
