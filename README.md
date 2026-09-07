# Polymarket V3 — Real-Data Paper Bot

This version runs on a cloud host and reads Polymarket's public market-data APIs. It does not place orders.

Polymarket documents that market data is public and requires no API key, authentication, or wallet.

The strategy included here is only a demonstration signal so we can validate the data pipeline and paper execution. It is not a claim of profitability.

## Render
- Build: `pip install -r requirements.txt`
- Start: `uvicorn bot:app --host 0.0.0.0 --port $PORT`

## Environment
Optional:
- STARTING_BALANCE=1000
- STAKE_USD=10
- MAX_POSITIONS=5
- SCAN_SECONDS=20
- MIN_LIQUIDITY=5000
- MIN_PRICE=0.08
- MAX_PRICE=0.92
- TAKE_PROFIT=0.08
- STOP_LOSS=0.06

No secrets are required.
