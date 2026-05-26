# Broker Adapter Notes

Broker credentials are resolved from `credentials_ref`.

For `credentials_ref: DOMESTIC_MASTER`, the loader reads:

- `DOMESTIC_MASTER_APP_KEY`
- `DOMESTIC_MASTER_APP_SECRET` or `DOMESTIC_MASTER_SECRET_KEY`
- `DOMESTIC_MASTER_ACCOUNT_NO`
- `DOMESTIC_MASTER_ACCOUNT_PASSWORD`
- `DOMESTIC_MASTER_ACCESS_TOKEN`
- `DOMESTIC_MASTER_BASE_URL`
- `DOMESTIC_MASTER_IS_MOCK`

REST endpoint paths can be provided per credential ref:

- `DOMESTIC_MASTER_DOMESTIC_BALANCE_PATH`
- `DOMESTIC_MASTER_DOMESTIC_ORDER_PATH`
- `DOMESTIC_MASTER_DOMESTIC_QUOTE_PATH`
- `DOMESTIC_MASTER_GLOBAL_BALANCE_PATH`
- `DOMESTIC_MASTER_GLOBAL_ORDER_PATH`
- `DOMESTIC_MASTER_GLOBAL_QUOTE_PATH`
- `DOMESTIC_MASTER_BALANCE_TR_ID`
- `DOMESTIC_MASTER_ORDER_TR_ID`
- `DOMESTIC_MASTER_QUOTE_TR_ID`

DB and Kiwoom adapters implement `BrokerClient` directly in their own files.
Each adapter owns its token, header, balance, quote, order payload, and response
parsing rules. Sell orders are sent as market orders; buy orders are sent as
limit orders at `Quote.ask_price_1`. Mirae Asset is intentionally a non-live
shell until a supported official trading API is available.
