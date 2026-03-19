"""
Trader FIXED - Limit orders, blacklist, spread filter, no repeat trades.
"""
import os
import traceback
from datetime import datetime, timedelta


class Trader:
    def __init__(self, pm_client, claude):
        self.pm = pm_client
        self.claude = claude

        self.max_bet = float(os.getenv("MAX_BET_USDC", "1"))
        self.min_confidence = int(os.getenv("MIN_CONFIDENCE", "80"))
        self.max_positions = int(os.getenv("MAX_OPEN_POSITIONS", "5"))
        self.claude_every = int(os.getenv("CLAUDE_EVERY_N_CYCLES", "3"))
        self.max_spread = float(os.getenv("MAX_SPREAD_PCT", "3"))

        self.tp_pct = float(os.getenv("TAKE_PROFIT_PCT", "5"))
        self.sl_pct = float(os.getenv("STOP_LOSS_PCT", "10"))
        self.trail_pct = float(os.getenv("TRAIL_PROFIT_PCT", "2"))

        self.is_running = False
        self.positions = []
        self.trade_log = []
        self.decision_log = []
        self.blacklist = set()          # Market IDs that lost money - never touch again
        self.cooldown = {}              # Market ID -> timestamp when we can trade again
        self.total_invested = 0
        self.total_returned = 0
        self.realized_pnl = 0
        self.cycle_count = 0
        self.auto_sells = 0
        self.claude_calls = 0
        self.start_time = None
        self.last_error = None
        self.wins = 0
        self.losses = 0

    def run_cycle(self):
        if not self.is_running:
            return

        self.cycle_count += 1
        now = datetime.now().strftime('%H:%M:%S')
        print(f"\n{'='*50}")
        print(f"⏰ [{now}] Ciclo #{self.cycle_count}")

        try:
            markets = self.pm.get_active_markets(limit=30)
            if not markets:
                return

            # Add spread info to markets
            self._enrich_markets_with_spread(markets)

            # Update prices
            self._update_prices(markets)

            # Auto-sell check
            sold = self._auto_sell(markets)
            if sold:
                print(f"   ⚡ {sold} venda(s) automática(s)")

            # Claude for new buys
            if self.cycle_count % self.claude_every == 0 or len(self.positions) < 2:
                self._ask_claude(markets)
            else:
                print(f"   ⏩ Monitorando preços...")

            self._summary()
            self.last_error = None

        except Exception as e:
            self.last_error = str(e)
            print(f"   ❌ {e}")
            traceback.print_exc()

    # ========== SPREAD CHECK ==========

    def _enrich_markets_with_spread(self, markets):
        """Add spread info to top markets for filtering."""
        for m in markets[:15]:
            tokens = m.get("tokens", [])
            if len(tokens) > 0:
                book = self.pm.get_orderbook(tokens[0])
                if book:
                    m["spread_pct"] = round(book.get("spread_pct", 100), 1)
                    m["bid"] = book.get("bid", 0)
                    m["ask"] = book.get("ask", 1)

    # ========== PRICE TRACKING ==========

    def _update_prices(self, markets):
        markets_by_id = {m["id"]: m for m in markets}

        for pos in self.positions:
            old_price = pos.get("current_price", pos["avg_price"])

            # Get from orderbook (most accurate)
            token_id = pos.get("token_id")
            if token_id:
                book = self.pm.get_orderbook(token_id)
                if book and book["mid"] > 0.01:
                    pos["current_price"] = book["mid"]
                    pos["bid"] = book["bid"]
                    pos["ask"] = book["ask"]

                    if book["mid"] > pos.get("peak_price", 0):
                        pos["peak_price"] = book["mid"]

                    entry = pos["avg_price"]
                    if entry > 0:
                        pos["pnl_pct"] = ((book["mid"] - entry) / entry) * 100
                        pos["pnl_usd"] = (book["mid"] - entry) * pos["shares"]

                    new_price = book["mid"]
                    if abs(new_price - old_price) > 0.002:
                        print(f"   📊 {pos['question'][:40]}... ${old_price:.3f}→${new_price:.3f} ({pos.get('pnl_pct',0):+.1f}%)")

    # ========== AUTO-SELL ==========

    def _auto_sell(self, markets):
        to_sell = []

        for i, pos in enumerate(self.positions):
            entry = pos["avg_price"]
            current = pos.get("current_price", entry)
            peak = pos.get("peak_price", current)

            if entry <= 0 or current <= 0 or current < entry * 0.3:
                continue

            pnl_pct = ((current - entry) / entry) * 100
            drop_from_peak = ((peak - current) / peak) * 100 if peak > 0 else 0

            reason = None

            if pnl_pct >= self.tp_pct:
                reason = f"✅ TP +{pnl_pct:.1f}%"
            elif pnl_pct <= -self.sl_pct:
                reason = f"🛑 SL {pnl_pct:.1f}%"
            elif peak > entry * 1.02 and pnl_pct > 0 and drop_from_peak >= self.trail_pct:
                reason = f"📉 TRAIL -{drop_from_peak:.1f}%"

            if reason:
                to_sell.append((i, pos, current, reason))

        sold = 0
        for i, pos, price, reason in sorted(to_sell, key=lambda x: x[0], reverse=True):
            if self._execute_sell(pos, reason, markets):
                self.positions.pop(i)
                sold += 1
                self.auto_sells += 1
        return sold

    def _execute_sell(self, pos, reason, markets):
        token_id = pos.get("token_id")
        if not token_id:
            return False

        ask_price = pos.get("ask", 0)
        if ask_price <= 0.01:
            bid_price = pos.get("bid", 0)
            if bid_price <= 0.01:
                print(f"      ❌ Sem preço válido pra vender")
                return False
            ask_price = bid_price

        try:
            shares = pos["shares"]

            # SELL AT ASK (limit order - we wait to be filled)
            result = self.pm.sell_at_ask(token_id, shares, ask_price)

            sell_value = shares * ask_price
            cost = shares * pos["avg_price"]
            pnl = sell_value - cost

            self.realized_pnl += pnl
            self.total_returned += sell_value

            if pnl >= 0:
                self.wins += 1
            else:
                self.losses += 1
                # BLACKLIST: lost money = never trade this market again
                self.blacklist.add(pos["market_id"])
                print(f"      🚫 Mercado adicionado à blacklist")

            # COOLDOWN: don't re-enter for 2 hours
            self.cooldown[pos["market_id"]] = datetime.now() + timedelta(hours=2)

            emoji = "💰" if pnl >= 0 else "🔻"
            print(f"   {emoji} SOLD '{pos['question'][:35]}' | {reason} | P&L: ${pnl:+.3f}")

            self.trade_log.append({
                "action": f"SELL_{pos['side']}",
                "market_id": pos["market_id"],
                "question": pos["question"],
                "side": pos["side"],
                "shares": round(shares, 2),
                "entry_price": pos["avg_price"],
                "exit_price": ask_price,
                "amount": round(sell_value, 2),
                "pnl": round(pnl, 4),
                "reason": reason,
                "timestamp": datetime.now().isoformat(),
                "cycle": self.cycle_count,
            })
            return True
        except Exception as e:
            print(f"   ❌ Sell erro: {e}")
            return False

    # ========== CLAUDE BUYS ==========

    def _ask_claude(self, markets):
        self.claude_calls += 1
        print(f"   🧠 Claude (#{self.claude_calls})...")

        portfolio = self._portfolio_state()

        if portfolio["usdc"] is not None and portfolio["usdc"] < 0.5:
            print(f"   ⚠️ Saldo insuficiente")
            return

        analysis = self.claude.find_opportunities(markets, portfolio, self.trade_log, self.blacklist)
        buys = analysis.get("buys", [])

        for b in buys:
            b["timestamp"] = datetime.now().isoformat()
            b["cycle"] = self.cycle_count
            self.decision_log.append(b)

        bought = 0
        for b in buys:
            if b.get("confidence", 0) < self.min_confidence:
                print(f"   ⏩ Confiança baixa: {b.get('question','')[:35]} ({b.get('confidence')}%)")
                continue
            if len(self.positions) >= self.max_positions:
                break

            amount = min(float(b.get("amount_usdc", 0)), self.max_bet)
            if amount < 0.5:
                continue

            if self._execute_buy(b, amount, markets):
                bought += 1

        print(f"   Resultado: {len(buys)} analisadas, {bought} compradas")

    def _execute_buy(self, decision, amount_usdc, markets):
        action = decision.get("action", "")
        market_id = decision.get("market_id", "")

        # BLACKLIST CHECK
        if market_id in self.blacklist:
            print(f"   🚫 Mercado na blacklist")
            return False

        # COOLDOWN CHECK
        cooldown_until = self.cooldown.get(market_id)
        if cooldown_until and datetime.now() < cooldown_until:
            mins_left = int((cooldown_until - datetime.now()).total_seconds() / 60)
            print(f"   ⏳ Cooldown: {mins_left}min restantes")
            return False

        # DUPLICATE CHECK
        for p in self.positions:
            if p["market_id"] == market_id:
                print(f"   ⏩ Já temos posição")
                return False

        market = next((m for m in markets if m["id"] == market_id), None)
        if not market:
            print(f"   ⚠️ Mercado não encontrado")
            return False

        # SPREAD CHECK
        spread = market.get("spread_pct", 100)
        if spread > self.max_spread:
            print(f"   ⏩ Spread alto: {spread:.1f}% > {self.max_spread}%")
            return False

        try:
            tokens = market["tokens"]
            prices = market["prices"]

            if action == "BUY_YES":
                token_id, price, side = tokens[0], prices[0], "YES"
            elif action == "BUY_NO":
                token_id, price, side = tokens[1], prices[1], "NO"
            else:
                return False

            if price <= 0.10 or price >= 0.90:
                print(f"   ⏩ Preço fora do range: ${price:.3f}")
                return False

            # GET BID PRICE (we buy at the bid = we're the maker)
            book = self.pm.get_orderbook(token_id)
            if not book or book["bid"] <= 0.01:
                print(f"   ⏩ Sem bid válido")
                return False

            buy_price = book["bid"]
            shares = round(amount_usdc / buy_price, 2)

            if shares < 2:
                print(f"   ⏩ Poucas shares")
                return False

            print(f"   🟢 BUY {side} '{market['question'][:40]}' | ${amount_usdc:.2f} @ ${buy_price:.3f} (bid) | spread:{spread:.1f}%")

            # BUY AT BID (limit order)
            result = self.pm.buy_at_bid(token_id, amount_usdc, buy_price)

            self.positions.append({
                "market_id": market_id,
                "question": market["question"],
                "side": side,
                "token_id": token_id,
                "shares": shares,
                "avg_price": buy_price,
                "current_price": buy_price,
                "peak_price": buy_price,
                "bid": book["bid"],
                "ask": book["ask"],
                "pnl_pct": 0,
                "pnl_usd": 0,
                "entry_time": datetime.now().isoformat(),
            })

            self.total_invested += amount_usdc

            self.trade_log.append({
                "action": action,
                "market_id": market_id,
                "question": market["question"],
                "side": side,
                "token_id": token_id,
                "shares": shares,
                "entry_price": buy_price,
                "amount": amount_usdc,
                "spread": spread,
                "confidence": decision.get("confidence", 0),
                "reasoning": decision.get("reasoning", ""),
                "timestamp": datetime.now().isoformat(),
                "cycle": self.cycle_count,
            })
            return True

        except Exception as e:
            print(f"   ❌ Buy erro: {e}")
            return False

    # ========== PORTFOLIO ==========

    def _portfolio_state(self):
        real = self.pm.get_usdc_balance()
        return {
            "usdc": real if real is not None else max(0, 17 - self.total_invested + self.total_returned),
            "positions": self.positions,
            "total_invested": self.total_invested,
            "total_returned": self.total_returned,
            "realized_pnl": self.realized_pnl,
            "total_trades": len(self.trade_log),
        }

    def _summary(self):
        unrealized = sum(p.get("pnl_usd", 0) for p in self.positions)
        real = self.pm.get_usdc_balance()
        usdc = real if real is not None else max(0, 17 - self.total_invested + self.total_returned)
        total_value = usdc + sum(
            p.get("current_price", p["avg_price"]) * p["shares"] for p in self.positions
        )
        wr = (self.wins / (self.wins + self.losses) * 100) if (self.wins + self.losses) > 0 else 0

        print(f"   💰 USDC: ${usdc:.2f} | Total: ~${total_value:.2f}")
        print(f"   📈 P&L: ${self.realized_pnl:+.3f} (real) ${unrealized:+.3f} (não-real)")
        print(f"   📊 W/L: {self.wins}/{self.losses} ({wr:.0f}%) | Pos: {len(self.positions)} | Blacklist: {len(self.blacklist)}")

    def start(self):
        self.is_running = True
        self.start_time = datetime.now().isoformat()
        balance = self.pm.get_usdc_balance()
        print(f"""
🚀 BOT ATIVO (FIXED)
   Saldo: ${balance or 0:.2f}
   TP: +{self.tp_pct}% | SL: -{self.sl_pct}% | Trail: -{self.trail_pct}%
   Max spread: {self.max_spread}% | Ordens: LIMIT (bid/ask)
   Claude a cada {self.claude_every} ciclos
""")

    def stop(self):
        self.is_running = False
        print("🛑 Bot parado.")

    def get_status(self):
        uptime = 0
        if self.start_time:
            delta = datetime.now() - datetime.fromisoformat(self.start_time)
            uptime = int(delta.total_seconds() / 60)

        unrealized = sum(p.get("pnl_usd", 0) for p in self.positions)
        real = self.pm.get_usdc_balance()
        usdc = real if real is not None else 0
        total_value = usdc + sum(
            p.get("current_price", p["avg_price"]) * p["shares"] for p in self.positions
        )
        wr = (self.wins / (self.wins + self.losses) * 100) if (self.wins + self.losses) > 0 else 0

        return {
            "is_running": self.is_running,
            "cycle_count": self.cycle_count,
            "uptime_minutes": uptime,
            "total_invested": round(self.total_invested, 2),
            "total_returned": round(self.total_returned, 2),
            "realized_pnl": round(self.realized_pnl, 4),
            "unrealized_pnl": round(unrealized, 4),
            "total_value": round(total_value, 2),
            "usdc_available": round(usdc, 2),
            "total_trades": len(self.trade_log),
            "auto_sells": self.auto_sells,
            "claude_calls": self.claude_calls,
            "wins": self.wins,
            "losses": self.losses,
            "winrate": round(wr, 1),
            "open_positions": len(self.positions),
            "max_positions": self.max_positions,
            "blacklisted": len(self.blacklist),
            "config": {
                "max_bet": self.max_bet,
                "min_confidence": self.min_confidence,
                "interval": int(os.getenv("TRADE_INTERVAL_SECONDS", "15")),
                "take_profit": self.tp_pct,
                "stop_loss": self.sl_pct,
                "trailing": self.trail_pct,
                "max_spread": self.max_spread,
                "claude_every": self.claude_every,
            },
            "recent_decisions": self.decision_log[-20:][::-1],
            "recent_trades": self.trade_log[-25:][::-1],
            "positions": self.positions[::-1],
            "last_error": self.last_error,
        }
