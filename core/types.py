from enum import Enum

SPACE = " "


class BrokerName(str, Enum):
    DB = "db"
    KIWOOM = "kiwoom"
    MIRAE_ASSET = "miraeasset"
    FAKE = "fake"


class MarketScope(str, Enum):
    DOMESTIC = "domestic"
    GLOBAL = "global"


class AccountMode(str, Enum):
    DRY_RUN = "dry_run"
    LIVE = "live"


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class SyncRunMode(str, Enum):
    DRY_RUN = "dry_run"
    LIVE = "live"
