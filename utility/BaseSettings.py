import json
import os

from dataclasses import dataclass, fields
from pathlib import Path
from typing import get_type_hints

from dotenv import load_dotenv


@dataclass
class BaseSettings:
    """
    환경 변수를 로드하고, 필드의 타입 힌트에 따라 타입 변환을 수행.
    타입 힌트가 없으면 기본적으로 문자열(str)로 처리.
    """
    def __post_init__(self):
        # .env 파일 로드
        project_root = Path(__file__).resolve().parents[1]  # project_root = C:/Home/lscopybot
        load_dotenv(dotenv_path=project_root.parent / "copybot_env" / ".env")   # C:/Home/copybot_env/.env
        type_hints = get_type_hints(self.__class__) # 타입 힌트를 가져옴
        for field in fields(self):
            env_value = os.getenv(field.name)
            if env_value is None:
                continue
            target_type = type_hints.get(field.name, str)   # 타입 힌트 없으면 str 기본값
            setattr(self, field.name, self._convert_value(env_value, target_type))

    def _convert_value(self, value: str, target_type):
        """
        타입 힌트에 따라 값을 변환. 힌트가 없으면 문자열로 반환.
        """
        if target_type in {float, float | None}:
            return float(value)
        if target_type in {int, int | None}:
            return int(value)
        if target_type in {bool, bool | None}:
            return self._str_to_bool(value)
        if target_type in {str, str | None}:
            return value
        if target_type in {list, list[str], list[str] | None}:
            return json.loads(value)
        if target_type is None or target_type == type(None):
            return None
        raise ValueError(f"Unsupported target type: {target_type}")

    @staticmethod
    def _str_to_bool(value: str) -> bool | None:
        """
        문자열을 boolean으로 변환. True/False로 인식할 값들을 처리.
        """
        true_values = {"1", "true", "True", "TRUE", "yes", "YES", "on", "ON"}
        false_values = {"0", "false", "False", "FALSE", "None", "none", "", "no", "NO", "off", "OFF"}
        if value in true_values:
            return True
        if value in false_values:
            return False
        return None
