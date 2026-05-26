from __future__ import annotations

import asyncio
from typing import Any

from broker import BrokerClient, create_broker_client

from .schemas import AccountConfig, CopyBotConfig


class AccountRegistry:
    def __init__(self):
        self.config = CopyBotConfig()
        self.clients: dict[str, BrokerClient] = {}

    def load(self, config: CopyBotConfig) -> None:
        self.config = config
        self.clients = {account.account_id: create_broker_client(account) for account in config.accounts}

    async def connect_all(self) -> None:
        if not self.clients:
            return
        clients = list(self.clients.values())
        results = await asyncio.gather(*(client.connect() for client in clients), return_exceptions=True)
        for client, result in zip(clients, results):
            if isinstance(result, Exception):
                client.connected = False
                client.last_message = str(result)

    def get_client(self, account_id: str) -> BrokerClient:
        try:
            return self.clients[account_id]
        except KeyError as error:
            raise ValueError(f"unknown account_id: {account_id}") from error

    def get_account(self, account_id: str) -> AccountConfig:
        return self.get_client(account_id).account

    def status(self) -> dict[str, Any]:
        return {
            account_id: client.status()
            for account_id, client in sorted(self.clients.items(), key=lambda item: item[0])
        }
