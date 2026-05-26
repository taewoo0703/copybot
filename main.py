from contextlib import asynccontextmanager
import ipaddress
import os
import traceback

from fastapi import FastAPI, Request, status
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import ORJSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from core.Core import core
from core.LogManager import logManager
from protocol import BaseRequest, ConfigReloadRequest, LogLevelRequest, SyncTriggerRequest
from settings import settings


VERSION = "Copy Trading Bot ver 2.0"


@asynccontextmanager
async def lifespan(app: FastAPI):
    logManager.initialize()
    await core.initialize()
    await logManager.log_message_async(f"{VERSION} started")

    yield

    try:
        await core.on_shutdown()
    except Exception as error:
        logManager.log_error_message(error, "Shutdown Error")


current_dir = os.path.dirname(os.path.abspath(__file__))
static_dir = os.path.join(current_dir, "static")
app = FastAPI(default_response_class=ORJSONResponse, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=static_dir), name="static")

##############################
# routing
##############################
@app.get("/")
async def get_home():
    return RedirectResponse(url="/static/home.html")

@app.get("/admin")
async def get_admin():
    return RedirectResponse(url="/static/admin.html")

##############################
# fundamental functinos
##############################

whitelist = ["127.0.0.1"] + (settings.WHITELIST or [])


@app.middleware("http")
async def whitelist_middleware(request: Request, call_next):
    try:
        if (
            settings.USE_WHITELIST
            and request.client
            and request.client.host not in whitelist
            and not ipaddress.ip_address(request.client.host).is_private
        ):
            msg = f"{request.client.host}는 안됩니다"
            print(msg)
            return ORJSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content=f"{request.client.host} is not allowed",
            )
    except Exception:
        logManager.log_error_message(traceback.format_exc(), "Middleware Error")

    return await call_next(request)


@app.middleware("http")
async def no_cache_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc):
    message = "[Error]\n"
    for index, error in enumerate(exc.errors()):
        message += f"[error{index + 1}] {error.get('msg')}\n{error.get('loc')}\n"

    logManager.log_error_message(f"{message}\n {exc.body}", "validation_exception_handler")
    return await request_validation_exception_handler(request, exc)


@app.post("/config/reload")
async def reload_config(request: ConfigReloadRequest):
    config = await core.reload_config()
    return {"message": "config reloaded", "config": config.to_dict()}


##########################################
# monitoring
##########################################
@app.post("/view_status")
async def view_status(request: BaseRequest):
    return core.view_status()


@app.post("/view_config")
async def view_config(request: BaseRequest):
    return core.view_config()


@app.post("/sync/trigger")
async def trigger_sync(request: SyncTriggerRequest):
    return await core.trigger_sync(request.group_id)


##########################################
# utility
##########################################
@app.get("/use_whitelist/{use}")
async def use_whitelist(use: str):
    use_bool = use.lower() in ("true", "1", "yes", "on")
    settings.USE_WHITELIST = use_bool
    return f"use_whitelist: {use_bool}"


##########################################
# external interrupt
##########################################
@app.post("/pause")
async def pause(request: BaseRequest):
    core.set_pause(True)
    return "Paused"


@app.post("/resume")
async def resume(request: BaseRequest):
    core.set_pause(False)
    return "Resumed"


##########################################
# debug
##########################################
@app.post("/log_level")
async def set_console_log_level(request: LogLevelRequest):
    level = request.log_level
    logManager.set_console_log_level(level)
    logManager.trace("trace")
    logManager.debug("debug")
    logManager.info("info")
    logManager.success("success")
    logManager.warning("warning")
    logManager.error("error")
    logManager.critical("critical")
    return f"console log level: {level}"
