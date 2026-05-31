import unittest

from core.LogManager import LogManager, TableImage


class LogManagerTests(unittest.TestCase):
    def test_render_table_png_without_browser(self):
        manager = LogManager()
        png = manager._render_table_png(
            TableImage(
                name="Snapshot",
                headers=["symbol", "current price", "weight"],
                rows=[
                    ["AAPL", "123.45", "50.00%"],
                    ["cash", "-", "50.00%"],
                ],
            )
        )

        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertGreater(len(png), 1000)

