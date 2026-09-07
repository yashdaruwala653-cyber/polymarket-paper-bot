# Polymarket V4 — Adaptive Real-Data Paper Bot

Paper trading only. Uses public Polymarket market data. No wallet, API key, or real orders.

Features:
- real active-market data from Gamma API
- probability/price analysis using implied probability, recent momentum, liquidity and volume
- confidence-ranked entries
- risk-based paper position sizing with stake cap
- configurable stop loss / take profit (default 1:2, 6% / 12%)
- maximum simultaneous positions
- maximum trades per hour and cooldown
- rolling adaptive parameter learning from closed paper trades
- SQLite trade log for the running session
- live dashboard and /health endpoint

Default paper settings:
- starting balance: $1,000
- max positions: 8
- max trades/hour: 12
- risk budget: 1% of balance per trade
- max stake: $50
- stop loss: 6%
- take profit: 12%
- scan: every 15 seconds

Important: adaptive learning is an online rules-based optimizer, not a claim of guaranteed profitability. Render's free service has ephemeral local storage, so the SQLite history is not durable across service replacement/redeploys.
