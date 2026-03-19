"""Polymarket AI Trading Bot"""
import os
from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler

load_dotenv()

for key in ["PRIVATE_KEY", "FUNDER_ADDRESS", "ANTHROPIC_API_KEY"]:
    val = os.getenv(key, "")
    if not val or "sua_" in val or "seu_" in val:
        print(f"❌ Configure {key} no .env")
        exit(1)

from src.polymarket_client import PolymarketClient
from src.claude_analyst import ClaudeAnalyst
from src.trader import Trader
from src.dashboard import create_app

print("🔌 Conectando...")
pm = PolymarketClient()
print("✅ Polymarket OK")

claude = ClaudeAnalyst()
trader = Trader(pm, claude)

port = int(os.getenv("PORT", "3000"))
app = create_app(trader)

interval = int(os.getenv("TRADE_INTERVAL_SECONDS", "15"))
scheduler = BackgroundScheduler()
scheduler.add_job(trader.run_cycle, "interval", seconds=interval, id="loop", max_instances=1)

trader.start()
scheduler.start()

print(f"📊 Dashboard: http://localhost:{port}")
app.run(host="0.0.0.0", port=port, debug=False)
