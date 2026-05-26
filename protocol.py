from pydantic import BaseModel, validator, root_validator
from settings import settings
from utility import LOGGER_LEVEL_LITERAL, LOGGER_LEVELS

############################################
# base request
############################################
def check_password(password: str) -> bool:
    if password == settings.PASSWORD:
        return True
    else:
        return False
    
class BaseRequest(BaseModel):
    password: str

    @validator("password")
    def validate_password(cls, v: str):
        if not check_password(v):
            raise ValueError("invalid password")
        return v
    
    @root_validator(pre=True)
    def root_validate(cls, values):
        # "NaN" to None
        for key, value in values.items():
            if value in ("NaN", ""):
                values[key] = None
        return values
    

############################################
# copybot requests
############################################
class ConfigReloadRequest(BaseRequest):
    pass


class SyncTriggerRequest(BaseRequest):
    group_id: str | None = None


############################################
# log level request
############################################
class LogLevelRequest(BaseRequest):
    log_level: LOGGER_LEVEL_LITERAL

    @validator("log_level", pre=True)
    def validate_log_level(cls, v: str):
        if v.upper() not in LOGGER_LEVELS:
            raise ValueError(f"invalid log level: {v.upper()}")
        return v.upper()
