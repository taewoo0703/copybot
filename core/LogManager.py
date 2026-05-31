from dataclasses import dataclass
from io import BytesIO
import re

from dhooks import Embed
from dhooks.file import File
import plotly.graph_objects as go
import plotly.io as pio

from settings import settings
from utility.BaseLogManager import BaseLogManager, log_level_under

from .schemas import CopyGroupConfig, OrderResult, PortfolioSnapshot, TargetOrder
from .types import OrderSide


class LogManager(BaseLogManager):
    def __init__(self, discord_webhook_url: str | None = None):
        super().__init__(discord_webhook_url)

    @log_level_under("INFO")
    async def log_master_position_changed_async(
        self,
        group: CopyGroupConfig,
        before: PortfolioSnapshot,
        after: PortfolioSnapshot,
    ) -> None:
        symbols = self._snapshot_symbols(before, after)
        description = f"group_id: `{group.group_id}`\nmaster_account_id: `{group.master_account_id}`"
        quantity_table = TableImage(
            name="Quantity",
            headers=["account", *symbols, "cash"],
            rows=[
                ["before", *self._quantity_values(before, symbols), self._money(before.cash)],
                ["after", *self._quantity_values(after, symbols), self._money(after.cash)],
            ],
        )
        weight_table = TableImage(
            name="Weight",
            headers=["account", *symbols, "cash"],
            rows=[
                ["before", *self._weight_values(before, symbols), self._percent(self._cash_weight(before))],
                ["after", *self._weight_values(after, symbols), self._percent(self._cash_weight(after))],
            ],
        )
        await self._send_table_images_async(
            title=f"Master changed: {after.account_id}",
            description=description,
            color=0x2ECC71,
            filename_prefix=f"master_changed_{after.account_id}",
            tables=[quantity_table, weight_table],
        )

    @log_level_under("TRACE")
    async def log_portfolio_snapshot_async(self, snapshot: PortfolioSnapshot) -> None:
        symbols = self._snapshot_symbols(snapshot)
        table = TableImage(
            name="Snapshot",
            headers=["metric", *symbols, "cash"],
            rows=[
                ["current price", *self._price_values(snapshot, symbols), "-"],
                ["quantity", *self._quantity_values(snapshot, symbols), "-"],
                ["current value", *self._market_value_values(snapshot, symbols), self._money(snapshot.cash)],
                ["weight", *self._weight_values(snapshot, symbols), self._percent(self._cash_weight(snapshot))],
            ],
        )
        await self._send_table_images_async(
            title=f"Portfolio snapshot: {snapshot.account_id}",
            description=(
                f"account_id: `{snapshot.account_id}`\n"
                f"total_equity: `{self._money(snapshot.total_equity)}`\n"
                f"captured_at: `{snapshot.captured_at}`"
            ),
            color=0x95A5A6,
            filename_prefix=f"portfolio_snapshot_{snapshot.account_id}",
            tables=[table],
        )

    @log_level_under("DEBUG")
    async def log_rebalance_orders_async(
        self,
        group: CopyGroupConfig,
        master: PortfolioSnapshot,
        slave: PortfolioSnapshot,
        orders: list[TargetOrder],
    ) -> None:
        if not orders:
            return

        symbols = self._rebalance_symbols(master, slave, orders)
        table = TableImage(
            name="Plan",
            headers=["metric", *symbols, "cash"],
            rows=[
                ["master weight", *self._weight_values(master, symbols), self._percent(self._cash_weight(master))],
                ["slave weight", *self._weight_values(slave, symbols), self._percent(self._cash_weight(slave))],
                ["current price", *self._rebalance_price_values(master, slave, symbols), "-"],
                ["slave current qty", *self._quantity_values(slave, symbols), "-"],
                ["target_qty", *self._target_quantity_values(slave, orders, symbols), "-"],
                ["diff_qty", *self._diff_quantity_values(orders, symbols), "-"],
            ],
        )
        await self._send_table_images_async(
            title=f"Rebalance orders: {slave.account_id}",
            description=(
                f"group_id: `{group.group_id}`\n"
                f"master_account_id: `{master.account_id}`\n"
                f"slave_account_id: `{slave.account_id}`\n"
                f"orders: `{len(orders)}`"
            ),
            color=0x3498DB,
            filename_prefix=f"rebalance_orders_{group.group_id}_{slave.account_id}",
            tables=[table],
        )

    @log_level_under("INFO")
    async def log_slave_sync_errors_async(
        self,
        group: CopyGroupConfig,
        slave_account_id: str,
        errors: list[str],
    ) -> None:
        if not errors:
            return

        embed = Embed(
            title=f"Slave sync errors: {slave_account_id}",
            description=(
                f"group_id: `{group.group_id}`\n"
                f"master_account_id: `{group.master_account_id}`\n"
                f"slave_account_id: `{slave_account_id}`"
            ),
            color=0xF1C40F,
        )
        self._add_text_fields(embed, "Errors", "\n".join(f"- {error}" for error in errors))
        await self.log_message_async(embed=embed)

    @log_level_under("INFO")
    async def log_order_results_async(
        self,
        group: CopyGroupConfig,
        slave_account_id: str,
        results: list[OrderResult],
    ) -> None:
        if not results:
            return

        embed = Embed(
            title=f"Order results: {slave_account_id}",
            description=(
                f"group_id: `{group.group_id}`\n"
                f"master_account_id: `{group.master_account_id}`\n"
                f"slave_account_id: `{slave_account_id}`\n"
                f"results: `{len(results)}`"
            ),
            color=0x1ABC9C,
        )
        for index, result in enumerate(results, start=1):
            embed.add_field(
                name=f"{index}. {result.order.instrument_key} {result.order.side.value} x{result.order.quantity}",
                value=self._lines(
                    f"accepted: `{result.accepted}`",
                    f"order_id: `{result.order_id or '-'}`",
                    f"message: {self._inline_code(self._short_text(result.message or '-', 850))}",
                ),
                inline=False,
            )
        await self.log_message_async(embed=embed)

    @log_level_under("INFO")
    async def log_group_weight_comparison_async(
        self,
        group: CopyGroupConfig,
        master: PortfolioSnapshot,
        slaves: list[PortfolioSnapshot],
    ) -> None:
        symbols = self._snapshot_symbols(master, *slaves)
        table = TableImage(
            name="Weights",
            headers=["account", *symbols, "cash"],
            rows=[
                ["master", *self._weight_values(master, symbols), self._percent(self._cash_weight(master))],
                *[
                    [slave.account_id, *self._weight_values(slave, symbols), self._percent(self._cash_weight(slave))]
                    for slave in slaves
                ],
            ],
        )
        await self._send_table_images_async(
            title=f"Sync weight comparison: {group.group_id}",
            description=(
                f"group_id: `{group.group_id}`\n"
                f"master_account_id: `{master.account_id}`\n"
                f"slave_account_ids: `{', '.join(slave.account_id for slave in slaves)}`"
            ),
            color=0x9B59B6,
            filename_prefix=f"sync_weight_comparison_{group.group_id}",
            tables=[table],
        )

    async def _send_table_images_async(
        self,
        title: str,
        description: str,
        color: int,
        filename_prefix: str,
        tables: list["TableImage"],
    ) -> None:
        if not self.hook_async:
            await self.log_message_async(embed=Embed(title=title, description=description, color=color))
            return

        try:
            for table in tables:
                png = self._render_table_png(table)
                filename = self._image_filename(filename_prefix, table.name)
                buffer = BytesIO(png)
                file = File(buffer, name=filename)
                embed = Embed(
                    title=f"{title} - {table.name}",
                    description=description,
                    color=color,
                    image_url=f"attachment://{filename}",
                )
                try:
                    await self.hook_async.send(embed=embed, file=file)
                finally:
                    file.close(force=True)
        except Exception as error:
            self.logger.error(f"Discord table image send error: {error}")

    def _render_table_png(self, table: "TableImage") -> bytes:
        columns = self._transpose_rows(table.rows)
        col_widths = self._column_widths(table.headers, table.rows)
        width = max(620, min(1500, 40 + sum(col_widths) * 11))
        header_height = 34
        cell_height = 32
        vertical_margin = 8
        height = header_height + len(table.rows) * cell_height + vertical_margin

        fig = go.Figure(
            data=[
                go.Table(
                    columnwidth=col_widths,
                    header=dict(
                        values=table.headers,
                        align=["left", *["right"] * (len(table.headers) - 1)],
                        fill_color="#E8EEF6",
                        font=dict(color="#111827", size=15),
                        height=header_height,
                        line_color="#CBD5E1",
                    ),
                    cells=dict(
                        values=columns,
                        align=["left", *["right"] * (len(table.headers) - 1)],
                        fill_color="#FFFFFF",
                        font=dict(color="#111827", size=14),
                        height=cell_height,
                        line_color="#E2E8F0",
                    ),
                )
            ]
        )
        fig.update_layout(
            width=width,
            height=height,
            margin=dict(l=2, r=2, t=2, b=2),
            paper_bgcolor="#FFFFFF",
        )
        return pio.to_image(fig, format="png", width=width, height=height, scale=2)

    def _transpose_rows(self, rows: list[list[str]]) -> list[list[str]]:
        if not rows:
            return []
        return [[row[index] for row in rows] for index in range(len(rows[0]))]

    def _column_widths(self, headers: list[str], rows: list[list[str]]) -> list[int]:
        widths = []
        for index, header in enumerate(headers):
            max_chars = max(len(str(header)), *(len(str(row[index])) for row in rows))
            widths.append(max(7, min(18, max_chars + 2)))
        return widths

    def _image_filename(self, prefix: str, table_name: str) -> str:
        safe_prefix = re.sub(r"[^A-Za-z0-9_.-]+", "_", prefix).strip("_")
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", table_name).strip("_")
        return f"{safe_prefix}_{safe_name}.png".lower()

    def _snapshot_symbols(self, *snapshots: PortfolioSnapshot) -> list[str]:
        symbols = {
            holding.key
            for snapshot in snapshots
            for holding in snapshot.holdings
        }
        return sorted(symbols)

    def _rebalance_symbols(
        self,
        master: PortfolioSnapshot,
        slave: PortfolioSnapshot,
        orders: list[TargetOrder],
    ) -> list[str]:
        return sorted(set(self._snapshot_symbols(master, slave)) | {order.instrument_key for order in orders})

    def _quantity_values(self, snapshot: PortfolioSnapshot, symbols: list[str]) -> list[str]:
        holdings = snapshot.holding_map()
        return [str(holdings[symbol].quantity) if symbol in holdings else "0" for symbol in symbols]

    def _price_values(self, snapshot: PortfolioSnapshot, symbols: list[str]) -> list[str]:
        holdings = snapshot.holding_map()
        return [self._money(holdings[symbol].current_price) if symbol in holdings else "-" for symbol in symbols]

    def _market_value_values(self, snapshot: PortfolioSnapshot, symbols: list[str]) -> list[str]:
        holdings = snapshot.holding_map()
        return [self._money(holdings[symbol].market_value) if symbol in holdings else "0" for symbol in symbols]

    def _weight_values(self, snapshot: PortfolioSnapshot, symbols: list[str]) -> list[str]:
        weights = snapshot.weights()
        return [self._percent(weights.get(symbol, 0.0)) for symbol in symbols]

    def _cash_weight(self, snapshot: PortfolioSnapshot) -> float:
        if snapshot.total_equity <= 0:
            return 0.0
        return snapshot.cash / snapshot.total_equity

    def _rebalance_price_values(
        self,
        master: PortfolioSnapshot,
        slave: PortfolioSnapshot,
        symbols: list[str],
    ) -> list[str]:
        master_holdings = master.holding_map()
        slave_holdings = slave.holding_map()
        values = []
        for symbol in symbols:
            holding = slave_holdings.get(symbol) or master_holdings.get(symbol)
            values.append(self._money(holding.current_price) if holding else "-")
        return values

    def _diff_quantity_values(self, orders: list[TargetOrder], symbols: list[str]) -> list[str]:
        diffs = {
            order.instrument_key: order.quantity if order.side == OrderSide.BUY else -order.quantity
            for order in orders
        }
        return [str(diffs.get(symbol, 0)) for symbol in symbols]

    def _target_quantity_values(
        self,
        slave: PortfolioSnapshot,
        orders: list[TargetOrder],
        symbols: list[str],
    ) -> list[str]:
        holdings = slave.holding_map()
        diffs = {
            order.instrument_key: order.quantity if order.side == OrderSide.BUY else -order.quantity
            for order in orders
        }
        values = []
        for symbol in symbols:
            current_qty = holdings[symbol].quantity if symbol in holdings else 0
            values.append(str(current_qty + diffs.get(symbol, 0)))
        return values

    def _add_text_fields(self, embed: Embed, name: str, value: str) -> None:
        blocks = self._text_blocks(value)
        for index, block in enumerate(blocks):
            field_name = name if len(blocks) == 1 else f"{name} ({index + 1}/{len(blocks)})"
            embed.add_field(name=field_name, value=block, inline=False)

    def _text_blocks(self, value: str) -> list[str]:
        chunks = []
        current = []
        current_length = 0
        for line in value.splitlines() or [""]:
            if len(line) > 1024:
                line = f"{line[:1021]}..."
            projected_length = current_length + len(line) + 1
            if current and projected_length > 1024:
                chunks.append("\n".join(current))
                current = [line]
                current_length = len(line) + 1
            else:
                current.append(line)
                current_length = projected_length
        if current:
            chunks.append("\n".join(current))
        return chunks

    def _short_text(self, value: str, limit: int = 120) -> str:
        normalized = " ".join(str(value).split())
        if len(normalized) <= limit:
            return normalized
        return f"{normalized[: limit - 3]}..."

    def _lines(self, *lines: str) -> str:
        return "\n".join(lines)

    def _inline_code(self, value: str) -> str:
        escaped = str(value).replace("`", "'")
        return f"`{escaped}`"

    def _money(self, value: float) -> str:
        if float(value).is_integer():
            return f"{value:,.0f}"
        return f"{value:,.2f}"

    def _percent(self, value: float) -> str:
        return f"{value * 100:.2f}%"


@dataclass(frozen=True)
class TableImage:
    name: str
    headers: list[str]
    rows: list[list[str]]


logManager = LogManager(settings.DISCORD_WEBHOOK_URL)
