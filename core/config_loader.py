from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from settings import settings

from .schemas import CopyBotConfig


DEFAULT_CONFIG_PATH = Path("config") / "copybot.yaml"


def load_copybot_config(path: str | os.PathLike | None = None) -> CopyBotConfig:
    config_path = Path(path or settings.COPYBOT_CONFIG_PATH or os.getenv("COPYBOT_CONFIG_PATH") or DEFAULT_CONFIG_PATH)
    if not config_path.exists():
        return CopyBotConfig()

    raw_text = config_path.read_text(encoding="utf-8")
    if config_path.suffix.lower() in {".yaml", ".yml"}:
        values = _load_yaml(raw_text)
    else:
        raise ValueError(f"copybot config must be YAML: {config_path.suffix}")

    if not isinstance(values, dict):
        raise ValueError("copybot config must be an object")
    return CopyBotConfig.from_dict(values)


def _load_yaml(raw_text: str) -> dict[str, Any]:
    values = yaml.safe_load(raw_text) or {}
    if not isinstance(values, dict):
        raise ValueError("copybot YAML config must be an object")
    return values
