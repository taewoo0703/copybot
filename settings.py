from utility.BaseSettings import BaseSettings
from dataclasses import dataclass
from functools import lru_cache

@dataclass
class DiscordSettings(BaseSettings):
    DISCORD_WEBHOOK_URL: str | None = None

@dataclass
class WebSettings(BaseSettings):
    PORT: int | None = None
    WHITELIST: list[str] | None = None
    USE_WHITELIST: bool | None = None
    PASSWORD: str | None = None

@dataclass
class ExchangeSettings(BaseSettings):
    COPYBOT_CONFIG_PATH: str | None = None

@dataclass
class TotalSettings(ExchangeSettings, DiscordSettings, WebSettings):
    pass

@lru_cache()
def get_settings():
    return TotalSettings()

# global instance
settings = get_settings()


# test
if __name__ == "__main__":
    print(settings)
