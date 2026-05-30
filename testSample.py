"""
Quick interactive sample for the new copy trading engine.

Run with:
    python testSample.py
"""

import asyncio

from core.CopyEngine import CopyEngine
from core.config_loader import load_copybot_config


async def main():
    engine = CopyEngine()
    config = load_copybot_config("config/copybot.yaml")
    await engine.apply_config(config, sync_after_load=True)
    print(engine.get_status())


if __name__ == "__main__":
    asyncio.run(main())
