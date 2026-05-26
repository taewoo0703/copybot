from __future__ import annotations

from core.schemas import AccountConfig
from core.types import BrokerName

from .base import BrokerClient, BrokerCredentials


def create_broker_client(account: AccountConfig) -> BrokerClient:
    credentials = BrokerCredentials.from_env(account.credentials_ref)
    if account.broker == BrokerName.DB:
        from .db import DBBrokerClient

        return DBBrokerClient(account, credentials)
    if account.broker == BrokerName.KIWOOM:
        from .kiwoom import KiwoomBrokerClient

        return KiwoomBrokerClient(account, credentials)
    if account.broker == BrokerName.MIRAE_ASSET:
        from .miraeasset import MiraeAssetBrokerClient

        return MiraeAssetBrokerClient(account, credentials)
    if account.broker == BrokerName.FAKE:
        from .fake import FakeBrokerClient

        return FakeBrokerClient(account, credentials)
    raise ValueError(f"unsupported broker: {account.broker}")
