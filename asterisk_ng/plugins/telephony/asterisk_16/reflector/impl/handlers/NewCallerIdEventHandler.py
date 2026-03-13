from typing import Callable

from asterisk_ng.plugins.telephony.ami_manager import Event
from asterisk_ng.plugins.telephony.ami_manager import IAmiEventHandler

from asterisk_ng.system.logger import ILogger

from ...core import IReflector


__all__ = [
    "NewCallerIdEventHandler",
]


class NewCallerIdEventHandler(IAmiEventHandler):

    __slots__ = (
        "__is_physical_channel",
        "__reflector",
        "__logger",
    )

    def __init__(
        self,
        is_physical_channel: Callable[[str], bool],
        reflector: IReflector,
        logger: ILogger,
    ) -> None:
        self.__is_physical_channel = is_physical_channel
        self.__reflector = reflector
        self.__logger = logger

    async def __call__(self, event: Event) -> None:
    
        channel_name = event["Channel"]
    
        if not self.__is_physical_channel(channel_name):
            return
    
        caller = event.get("CallerIDNum")
        connected = event.get("ConnectedLineNum")
    
        phone_number = caller
    
        if "vipma_" in channel_name and connected:
            phone_number = connected
    
        if phone_number is None:
            return
    
        # 🔴 игнорируем внутренние номера типа 053100
        if phone_number.isdigit() and len(phone_number) < 7:
            return
    
        try:
            channel = await self.__reflector.get_channel_by_name(channel_name)
        except KeyError:
            return
    
        if channel.phone is None:
            await self.__reflector.update_channel_phone(
                channel_name,
                phone=phone_number
            )
