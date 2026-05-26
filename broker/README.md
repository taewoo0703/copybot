# Broker Adapter Notes

Broker credentials are resolved from `credentials_ref`.

For `credentials_ref: DOMESTIC_MASTER`, the loader reads:

- `DOMESTIC_MASTER_APP_KEY`
- `DOMESTIC_MASTER_APP_SECRET`
- `DOMESTIC_MASTER_IS_MOCK`

DB and Kiwoom adapters implement `BrokerClient` directly in their own files.
Each adapter owns its token, header, balance, quote, order payload, and response
parsing rules. Sell orders are sent as market orders; buy orders are sent as
limit orders at `Quote.ask_price_1`. Mirae Asset is intentionally a non-live
shell until a supported official trading API is available.

Official REST paths, TR IDs, domestic exchange defaults, and DB overseas market
division defaults are code constants in `broker/db.py` and `broker/kiwoom.py`.
They are not loaded from `.env`. Access tokens are issued at runtime from the
app key and secret key, then kept in process memory. The default broker API
base URLs are code constants.
