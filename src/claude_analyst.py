"""
Claude Analyst - FIXED: Filters bad markets, prevents repeat trades.
"""
import os
import json
import re
import anthropic


class ClaudeAnalyst:
    def __init__(self):
        self.client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    def find_opportunities(self, markets, portfolio, trade_history, blacklist):
        markets_text = self._format_markets(markets, blacklist)
        portfolio_text = self._format_portfolio(portfolio)

        if not markets_text:
            return {"buys": []}

        prompt = f"""MERCADOS DISPONÍVEIS:

{markets_text}

PORTFÓLIO: {portfolio_text}

Encontre mercados onde o preço está ERRADO.

ESTRATÉGIA (IMPORTANTE):
- Compre posições que você acredita que vão VALORIZAR nos próximos dias
- NÃO é scalping rápido. É comprar barato e esperar o mercado corrigir
- Exemplo: jogo de basquete onde um time é favorito mas o preço não reflete isso
- Exemplo: evento político extremamente improvável mas com YES acima de 20%

REGRAS:
- SÓ preços entre $0.15 e $0.85
- Spread do mercado deve ser MENOR que 3%
- Volume mínimo $10k
- Máximo $1 por trade
- Confiança mínima 80%
- Máximo 2 compras por ciclo
- NUNCA recomende mercados que já estão na blacklist

BLACKLIST (não tocar): {list(blacklist)}

Responda APENAS JSON:
{{
  "buys": [
    {{
      "market_id": "id",
      "question": "pergunta",
      "action": "BUY_YES" | "BUY_NO",
      "amount_usdc": 1,
      "confidence": 0-100,
      "reasoning": "por que vai valorizar"
    }}
  ]
}}

Se nada parece bom, retorne buys vazio. Melhor não operar."""

        try:
            response = self.client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=1500,
                system="""Analista de prediction markets. Foco: encontrar valor.
Compra onde preço não reflete realidade. Segura até corrigir.
Conservador: só opera com alta convicção. Na dúvida, não opera.
APENAS JSON. Sem markdown.""",
                messages=[{"role": "user", "content": prompt}],
            )

            text = response.content[0].text.strip()
            result = self._extract_json(text)

            if result is None:
                return {"buys": []}

            valid = []
            for b in result.get("buys", []):
                if b.get("action") in ("BUY_YES", "BUY_NO"):
                    mid = b.get("market_id", "")
                    if mid in blacklist:
                        continue
                    b["amount_usdc"] = min(float(b.get("amount_usdc", 0)), 1)
                    b["confidence"] = min(int(b.get("confidence", 0)), 100)
                    valid.append(b)
            result["buys"] = valid[:2]
            return result

        except Exception as e:
            print(f"   ❌ Claude erro: {e}")
            return {"buys": [], "error": str(e)}

    def _extract_json(self, text):
        try:
            return json.loads(text)
        except Exception:
            pass
        patterns = [
            r'```json\s*\n?(.*?)\n?```',
            r'```\s*\n?(.*?)\n?```',
            r'(\{[\s\S]*"buys"[\s\S]*\})',
        ]
        for pat in patterns:
            match = re.search(pat, text, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group(1).strip())
                except Exception:
                    continue
        start = text.rfind('{')
        if start >= 0:
            depth = 0
            for i in range(start, len(text)):
                if text[i] == '{':
                    depth += 1
                elif text[i] == '}':
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[start:i+1])
                        except Exception:
                            break
        return None

    def _format_markets(self, markets, blacklist):
        lines = []
        for i, m in enumerate(markets[:25], 1):
            if m["id"] in blacklist:
                continue
            prices = m.get("prices", [0, 0])
            yes = prices[0] if len(prices) > 0 else 0
            no = prices[1] if len(prices) > 1 else 0
            if yes > 0.85 or yes < 0.15:
                continue
            spread_info = m.get("spread_pct", "?")
            lines.append(
                f"{i}. [{m['id']}] {m['question']}\n"
                f"   YES=${yes:.2f} NO=${no:.2f} Spread={spread_info}% "
                f"Vol=${m['volume_24h']:,.0f} Liq=${m['liquidity']:,.0f} "
                f"Cat:{m.get('category','?')}"
            )
        return "\n".join(lines) if lines else ""

    def _format_portfolio(self, portfolio):
        return (
            f"USDC=${portfolio.get('usdc',0):.2f} | "
            f"P&L=${portfolio.get('realized_pnl',0):.3f} | "
            f"Posições={len(portfolio.get('positions',[]))}"
        )
