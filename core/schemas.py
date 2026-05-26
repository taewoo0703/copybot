from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from .types import AccountMode, BrokerName, MarketScope, OrderSide, SyncRunMode


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Holding:
    symbol: str
    quantity: int
    current_price: float
    market_value: float
    exchange: str = ""
    currency: str = "KRW"

    @property
    def key(self) -> str:
        return f"{self.exchange}:{self.symbol}" if self.exchange else self.symbol

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PortfolioSnapshot:
    account_id: str
    total_equity: float
    cash: float
    holdings: list[Holding] = field(default_factory=list)
    currency: str = "KRW"
    captured_at: str = field(default_factory=utc_now_iso)

    def holding_map(self) -> dict[str, Holding]:
        return {holding.key: holding for holding in self.holdings}

    def weights(self) -> dict[str, float]:
        if self.total_equity <= 0:
            return {}
        return {
            holding.key: holding.market_value / self.total_equity
            for holding in self.holdings
            if holding.market_value > 0
        }

    def fingerprint(self) -> tuple[tuple[str, int], ...]:
        return tuple(sorted((holding.key, holding.quantity) for holding in self.holdings))

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["weights"] = self.weights()
        return payload


@dataclass
class TargetOrder:
    group_id: str
    account_id: str
    broker: BrokerName
    market_scope: MarketScope
    symbol: str
    side: OrderSide
    quantity: int
    estimated_price: float
    estimated_value: float
    mode: SyncRunMode
    exchange: str = ""
    reason: str = "rebalance"
    created_at: str = field(default_factory=utc_now_iso)

    @property
    def instrument_key(self) -> str:
        return f"{self.exchange}:{self.symbol}" if self.exchange else self.symbol

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["broker"] = self.broker.value
        payload["market_scope"] = self.market_scope.value
        payload["side"] = self.side.value
        payload["mode"] = self.mode.value
        payload["instrument_key"] = self.instrument_key
        return payload


@dataclass
class OrderResult:
    order: TargetOrder
    accepted: bool
    order_id: str | None = None
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "order": self.order.to_dict(),
            "accepted": self.accepted,
            "order_id": self.order_id,
            "message": self.message,
        }


@dataclass
class AccountConfig:
    account_id: str
    broker: BrokerName
    market_scope: MarketScope
    credentials_ref: str
    mode: AccountMode = AccountMode.DRY_RUN
    base_url: str | None = None

    @classmethod
    def from_dict(cls, values: dict[str, Any]) -> "AccountConfig":
        return cls(
            account_id=str(values["account_id"]),
            broker=BrokerName(values["broker"]),
            market_scope=MarketScope(values["market_scope"]),
            credentials_ref=str(values.get("credentials_ref") or values["account_id"]),
            mode=AccountMode(values.get("mode", AccountMode.DRY_RUN.value)),
            base_url=values.get("base_url"),
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["broker"] = self.broker.value
        payload["market_scope"] = self.market_scope.value
        payload["mode"] = self.mode.value
        return payload


@dataclass
class CopyGroupConfig:
    group_id: str
    master_account_id: str
    slave_account_ids: list[str]
    enabled: bool = True
    poll_interval_seconds: int = 60
    min_trade_value: float = 0.0
    mode: AccountMode = AccountMode.DRY_RUN

    @classmethod
    def from_dict(cls, values: dict[str, Any]) -> "CopyGroupConfig":
        return cls(
            group_id=str(values["group_id"]),
            master_account_id=str(values["master_account_id"]),
            slave_account_ids=[str(account_id) for account_id in values.get("slave_account_ids", [])],
            enabled=bool(values.get("enabled", True)),
            poll_interval_seconds=int(values.get("poll_interval_seconds", 60)),
            min_trade_value=float(values.get("min_trade_value", 0.0)),
            mode=AccountMode(values.get("mode", AccountMode.DRY_RUN.value)),
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["mode"] = self.mode.value
        return payload


@dataclass
class CopyBotConfig:
    accounts: list[AccountConfig] = field(default_factory=list)
    copy_groups: list[CopyGroupConfig] = field(default_factory=list)

    @classmethod
    def from_dict(cls, values: dict[str, Any]) -> "CopyBotConfig":
        accounts = [AccountConfig.from_dict(item) for item in values.get("accounts", [])]
        copy_groups = [CopyGroupConfig.from_dict(item) for item in values.get("copy_groups", [])]
        config = cls(accounts=accounts, copy_groups=copy_groups)
        config.validate()
        return config

    def account_map(self) -> dict[str, AccountConfig]:
        return {account.account_id: account for account in self.accounts}

    def validate(self) -> None:
        account_ids = [account.account_id for account in self.accounts]
        if len(account_ids) != len(set(account_ids)):
            raise ValueError("duplicate account_id in copybot config")

        group_ids = [group.group_id for group in self.copy_groups]
        if len(group_ids) != len(set(group_ids)):
            raise ValueError("duplicate group_id in copybot config")

        accounts = self.account_map()
        slave_to_group: dict[str, str] = {}
        for group in self.copy_groups:
            if group.master_account_id not in accounts:
                raise ValueError(f"unknown master_account_id: {group.master_account_id}")
            if not group.slave_account_ids:
                raise ValueError(f"copy group {group.group_id} has no slave accounts")

            master_scope = accounts[group.master_account_id].market_scope
            for slave_id in group.slave_account_ids:
                if slave_id not in accounts:
                    raise ValueError(f"unknown slave_account_id: {slave_id}")
                if slave_id == group.master_account_id:
                    raise ValueError(f"copy group {group.group_id} uses master as slave")
                if accounts[slave_id].market_scope != master_scope:
                    raise ValueError(f"copy group {group.group_id} mixes market scopes")
                if slave_id in slave_to_group:
                    raise ValueError(
                        f"slave account {slave_id} is already assigned to group {slave_to_group[slave_id]}"
                    )
                slave_to_group[slave_id] = group.group_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "accounts": [account.to_dict() for account in self.accounts],
            "copy_groups": [group.to_dict() for group in self.copy_groups],
        }
