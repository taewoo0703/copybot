import asyncio
import functools
import os
import sys
import traceback
from datetime import datetime, timedelta, timezone
from typing import Literal

try:
    from loguru import logger
except ImportError:
    class _FallbackLogger:
        def remove(self, *args, **kwargs):
            return None

        def add(self, *args, **kwargs):
            return 0

        def trace(self, message, *args, **kwargs):
            print(message)

        def debug(self, message, *args, **kwargs):
            print(message)

        def info(self, message, *args, **kwargs):
            print(message)

        def success(self, message, *args, **kwargs):
            print(message)

        def warning(self, message, *args, **kwargs):
            print(message)

        def error(self, message, *args, **kwargs):
            print(message)

        def critical(self, message, *args, **kwargs):
            print(message)

    logger = _FallbackLogger()

try:
    from dhooks import Webhook, Embed
except ImportError:
    class Embed:
        def __init__(self, title: str = "", description: str = "", color: int = 0):
            self.title = title
            self.description = description
            self.color = color
            self.fields = []

        def add_field(self, name: str, value: str, inline: bool = False):
            self.fields.append({"name": name, "value": value, "inline": inline})

    class Webhook:
        def __init__(self, url: str):
            self.url = url

        def send(self, message: str = None, embed: Embed = None):
            if embed:
                logger.info(f"{embed.title} {embed.description}")
            elif message:
                logger.info(message)

        class Async:
            def __init__(self, url: str):
                self.url = url

            async def send(self, message: str = None, embed: Embed = None):
                if embed:
                    logger.info(f"{embed.title} {embed.description}")
                elif message:
                    logger.info(message)


LOGGER_LEVEL_LITERAL = Literal["TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"]
LOGGER_LEVELS = ("TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL")


def log_level_under(level: LOGGER_LEVEL_LITERAL):
    def decorator(func):
        if asyncio.iscoroutinefunction(func):
            @functools.wraps(func)
            async def async_wrapper(self, *args, **kwargs):
                if LOGGER_LEVELS.index(self.log_level) <= LOGGER_LEVELS.index(level):
                    return await func(self, *args, **kwargs)
                return None

            return async_wrapper

        @functools.wraps(func)
        def sync_wrapper(self, *args, **kwargs):
            if LOGGER_LEVELS.index(self.log_level) <= LOGGER_LEVELS.index(level):
                return func(self, *args, **kwargs)
            return None

        return sync_wrapper

    return decorator


class BaseLogManager:
    def __init__(self, discord_webhook_url: str | None = None):
        self.discord_webhook_url = discord_webhook_url
        self.log_level: LOGGER_LEVEL_LITERAL = "DEBUG"

        current_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(current_dir)
        log_path = os.path.join(project_root, "log", "copybot.log")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)

        try:
            logger.remove(0)
        except Exception:
            pass
        logger.add(
            log_path,
            rotation="1 days",
            retention="7 days",
            format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}",
            level="INFO",
        )
        self.console_handler_id = logger.add(
            sys.stderr,
            colorize=True,
            format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level}</level> | <level>{message}</level>",
            level=self.log_level,
        )
        self.logger = logger
        self.hook = None
        self.hook_async = None
        self.test_mode = False
        self._configure_webhook()

    def _configure_webhook(self):
        if not self.discord_webhook_url:
            return
        try:
            url = self.discord_webhook_url.replace("discordapp", "discord")
            self.hook = Webhook(url)
        except Exception:
            logger.error("Discord webhook URL is invalid: {}", self.discord_webhook_url)
            self.hook = None

    def initialize(self):
        if not self.discord_webhook_url:
            self.hook_async = None
            return
        try:
            url = self.discord_webhook_url.replace("discordapp", "discord")
            self.hook_async = Webhook.Async(url)
        except Exception as error:
            self.log_error_message(error, "BaseLogManager")
            logger.error("Discord webhook URL is invalid: {}", self.discord_webhook_url)
            self.hook_async = None

    def set_console_log_level(self, level: LOGGER_LEVEL_LITERAL):
        if level not in LOGGER_LEVELS:
            raise ValueError(f"log level must be one of {LOGGER_LEVELS}")
        try:
            logger.remove(self.console_handler_id)
        except Exception:
            pass
        self.console_handler_id = logger.add(
            sys.stderr,
            colorize=True,
            format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level}</level> | <level>{message}</level>",
            level=level,
        )
        self.log_level = level

    def set_test_mode(self, test_mode: bool):
        self.test_mode = test_mode

    def get_error(self, error: Exception) -> str:
        tb = traceback.extract_tb(error.__traceback__)
        error_msg = []
        for tb_info in tb:
            error_msg.append(f"File {tb_info.filename}, line {tb_info.lineno}, in {tb_info.name}")
            if tb_info.line:
                error_msg.append(f"  {tb_info.line}")
        error_msg.append(str(error))
        return "\n".join(error_msg)

    def parse_time(self, utc_timestamp):
        utc_datetime = datetime.fromtimestamp(utc_timestamp, timezone.utc)
        kst_datetime = utc_datetime.astimezone(timezone(timedelta(hours=9)))
        return kst_datetime.strftime("%y-%m-%d %H:%M:%S")

    def log_message(self, message: str = "None", embed: Embed = None):
        try:
            if self.hook:
                if embed:
                    self.hook.send(embed=embed)
                else:
                    self.hook.send(message)
            elif embed:
                logger.info(f"{embed.title} {embed.description}")
            else:
                logger.info(message)
        except Exception as error:
            logger.error(f"Discord send error: {error}")

    def log_error_message(self, error: str | Exception, name: str, description: str = None):
        if isinstance(error, Exception):
            error = self.get_error(error)
        embed = Embed(title=f"{name} error", description=f"[{name} error]\n{error}", color=0xFF0000)
        if description:
            embed.add_field(name="Description", value=description, inline=False)
        logger.error(f"{name} error\n{error}")
        self.log_message(embed=embed)

    def log_warning_message(self, error: str | Exception, name: str, description: str = None):
        if isinstance(error, Exception):
            error = self.get_error(error)
        embed = Embed(title=f"{name} warning", description=f"[{name} warning]\n{error}", color=0xFFAA00)
        if description:
            embed.add_field(name="Description", value=description, inline=False)
        logger.warning(f"{name} warning\n{error}")
        self.log_debug_message(embed=embed)

    @log_level_under("DEBUG")
    def log_debug_message(self, message: str = "None", embed: Embed = None):
        self.log_message(message, embed)

    @log_level_under("DEBUG")
    def log_error_debug_message(self, error: str | Exception, name: str, description: str = None):
        self.log_error_message(error, name, description)

    @log_level_under("TRACE")
    def log_trace_message(self, message: str = "None", embed: Embed = None):
        self.log_message(message, embed)

    @log_level_under("TRACE")
    def log_error_trace_message(self, error: str | Exception, name: str, description: str = None):
        self.log_error_message(error, name, description)

    ################################
    # async discord webhook
    ################################
    async def log_message_async(self, message: str = "None", embed: Embed = None):
        try:
            if self.hook_async:
                if embed:
                    await self.hook_async.send(embed=embed)
                else:
                    await self.hook_async.send(message)
            elif embed:
                logger.info(f"{embed.title} {embed.description}")
            else:
                logger.info(message)
        except Exception as error:
            logger.error(f"Discord send error: {error}")

    async def log_error_message_async(self, error: str | Exception, name: str, description: str = None):
        if isinstance(error, Exception):
            error = self.get_error(error)
        embed = Embed(title=f"{name} error", description=f"[{name} error]\n{error}", color=0xFF0000)
        if description:
            embed.add_field(name="Description", value=description, inline=False)
        logger.error(f"{name} error\n{error}")
        await self.log_message_async(embed=embed)

    async def log_warning_message_async(self, error: str | Exception, name: str, description: str = None):
        if isinstance(error, Exception):
            error = self.get_error(error)
        embed = Embed(title=f"{name} warning", description=f"[{name} warning]\n{error}", color=0xFFAA00)
        if description:
            embed.add_field(name="Description", value=description, inline=False)
        logger.warning(f"{name} warning\n{error}")
        await self.log_debug_message_async(embed=embed)

    @log_level_under("DEBUG")
    async def log_debug_message_async(self, message: str = "None", embed: Embed = None):
        await self.log_message_async(message, embed)

    @log_level_under("DEBUG")
    async def log_error_debug_message_async(self, error: str | Exception, name: str, description: str = None):
        await self.log_error_message_async(error, name, description)

    @log_level_under("TRACE")
    async def log_trace_message_async(self, message: str = "None", embed: Embed = None):
        await self.log_message_async(message, embed)

    @log_level_under("TRACE")
    async def log_error_trace_message_async(self, error: str | Exception, name: str, description: str = None):
        await self.log_error_message_async(error, name, description)

    ################################
    # logger wrapper
    ################################
    def trace(self, message: str, for_test_mode: bool = False):
        if not self.test_mode or for_test_mode:
            logger.trace(message)

    def debug(self, message: str, for_test_mode: bool = False):
        if not self.test_mode or for_test_mode:
            logger.debug(message)

    def info(self, message: str, for_test_mode: bool = False):
        if not self.test_mode or for_test_mode:
            logger.info(message)

    def success(self, message: str, for_test_mode: bool = False):
        if not self.test_mode or for_test_mode:
            logger.success(message)

    def warning(self, message: str, for_test_mode: bool = False):
        if not self.test_mode or for_test_mode:
            logger.warning(message)

    def error(self, message: str, for_test_mode: bool = False):
        if not self.test_mode or for_test_mode:
            logger.error(message)

    def critical(self, message: str, for_test_mode: bool = False):
        if not self.test_mode or for_test_mode:
            logger.critical(message)
