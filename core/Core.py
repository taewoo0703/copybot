import asyncio
import time

from .CopyEngine import CopyEngine
from .EmergencyControl import emergencyControl
from .LogManager import logManager


class Core:
    def __init__(self):
        self.emergency_control = emergencyControl
        self.copy_engine = CopyEngine()
        # loop
        self.active = True  # loop active
        # tasks
        self.timer_task = None
        self.emergency_task = None

    async def initialize(self):
        # Init Copy Engine (reload config)
        await self.copy_engine.initialize()

        # set timer
        self.timer_task = asyncio.create_task(self.timer_update_1s())
        self.timer_task.add_done_callback(self.timer_update_done_callback)

        # start emergency control loop
        self.emergency_task = asyncio.create_task(self.emergency_control_loop())

    async def on_shutdown(self):
        # set active false to stop loops
        self.active = False

        # cancel tasks
        if self.timer_task and not self.timer_task.done():
            self.timer_task.cancel()
            try:
                await self.timer_task
            except asyncio.CancelledError:
                pass

        if self.emergency_task and not self.emergency_task.done():
            self.emergency_task.cancel()
            try:
                await self.emergency_task
            except asyncio.CancelledError:
                pass

        # shutdown complete
        await logManager.log_message_async("shutdown complete!")

    ##############################
    # Emergency Control loop
    ##############################
    async def emergency_control_loop(self):
        while self.active:
            await self.emergency_control.update()
            await asyncio.sleep(1)


    ##############################
    # Timer event
    ##############################
    # timer - update
    async def timer_update_1s(self):
        """
        매 1초마다 on_timer_update 메서드 호출
        """
        while self.active:
            await asyncio.sleep(1)
            await self.on_timer_update(time.time(), "1s")

    # on_timer - update
    async def on_timer_update(self, update_timestamp: float, timeframe: str):
        """
        update timer에 의해 호출되는 메서드
        """
        await self.copy_engine.on_timer_update()
        logManager.trace(f"on_timer_update - {timeframe} complete")

    # done call back - update
    def timer_update_done_callback(self, task):
        self.active = False
        try:
            if task.cancelled():
                logManager.log_message("update task was cancelled")
            elif task.exception():
                logManager.log_error_message(task.exception(), "update error")
            else:
                logManager.log_message("update done")
        except Exception as error:
            logManager.log_error_message(error, "update error")


    ################################
    # API methods
    ################################
    async def reload_config(self):
        """
        설정 재로드 메서드. API에서 호출됨.
        - 설정을 재로드하고, 동기화까지 수행. 동기화는 설정 로드 후에 수행하여, 새 설정이 즉시 반영되도록 함.
        """
        return await self.copy_engine.reload_config(sync_after_load=True)

    async def trigger_sync(self, group_id: str | None = None):
        """
        그룹 동기화 트리거 메서드. API에서 호출됨.
        - group_id: 특정 그룹 ID를 지정하면 해당 그룹만 동기화, None이면 모든 그룹 동기화
        """
        if group_id:
            return await self.copy_engine.sync_group(group_id, force=True)
        return await self.copy_engine.sync_all(force=True)

    def set_pause(self, pause: bool) -> None:
        """
        시스템 일시정지 설정 메서드. API에서 호출됨.
        - pause: True로 설정하면 시스템이 일시정지되고, False로 설정하면 재개됨.
        """
        self.copy_engine.set_pause(pause)

    def view_status(self) -> dict:
        """
        시스템 상태를 반환하는 메서드. API에서 호출됨.
         - paused: 시스템 일시정지 여부
         - accounts: 계정별 상태 정보
         - groups: 그룹별 상태 정보
         - next_poll_at: 그룹별 다음 동기화 예정 시간
        """
        return self.copy_engine.get_status()

    def view_config(self) -> dict:
        """
        현재 로드된 설정을 반환하는 메서드. API에서 호출됨.
        """
        return self.copy_engine.get_config()


core: Core = Core()
