from utility.BaseLogManager import BaseLogManager, log_level_under
from settings import settings


class LogManager(BaseLogManager):
    def __init__(self, discord_webhook_url: str | None = None):
        super().__init__(discord_webhook_url)

    @log_level_under("INFO")
    async def log_sync_message_async(self, group_id: str, message: str):
        await self.log_message_async(f"[sync:{group_id}] {message}")

    @log_level_under("DEBUG")
    async def log_rebalance_plan_async(self, group_id: str, orders: list[dict]):
        await self.log_message_async(f"[rebalance:{group_id}] planned orders: {orders}")

    @log_level_under("INFO")
    async def log_position_change_message_async(self, positions: list[dict]):
        await self.log_message_async(f"positions changed: {positions}")

    @log_level_under("DEBUG")
    async def log_order_message_async(self, order_info: dict, type):
        await self.log_message_async(f"[{type}] order: {order_info}")

    @log_level_under("DEBUG")
    async def log_cancel_order_message_async(self, order_info: dict, type):
        await self.log_message_async(f"[{type}] cancel order: {order_info}")

    @log_level_under("ERROR")
    async def log_order_error_message_async(self, error: str | Exception, order_info: dict, type):
        await self.log_error_message_async(error, f"{type} Order", str(order_info))

    @log_level_under("ERROR")
    async def log_cancel_order_error_message_async(self, error: str | Exception, order_info: dict, type):
        await self.log_error_message_async(error, f"{type} Cancel Order", str(order_info))

    @log_level_under("ERROR")
    async def log_fetch_positions_error_message_async(self, error: str | Exception, type):
        await self.log_error_message_async(error, f"{type} Fetch Positions")


logManager = LogManager(settings.DISCORD_WEBHOOK_URL)
