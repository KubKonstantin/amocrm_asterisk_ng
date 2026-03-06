from typing import Callable
from datetime import datetime

from asterisk_ng.interfaces import CallCreatedTelephonyEvent, RingingTelephonyEvent, CallCompletedTelephonyEvent

from asterisk_ng.plugins.telephony.ami_manager import Event
from asterisk_ng.plugins.telephony.ami_manager import IAmiEventHandler

from asterisk_ng.system.event_bus import IEventBus
from asterisk_ng.system.logger import ILogger

from ...core import IReflector


__all__ = [
    "NewStateEventHandler",
]

def extract_endpoint(channel_name: str) -> str:
    """
    PJSIP/vipma_kkubeev-00000008 -> vipma_kkubeev
    """
    try:
        return channel_name.split("/")[1].split("-")[0]
    except Exception:
        return channel_name

class NewStateEventHandler(IAmiEventHandler):

    __slots__ = (
        "__is_physical_channel",
        "__reflector",
        "__event_bus",
        "__logger",
    )

    def __init__(
        self,
        is_physical_channel: Callable[[str], bool],
        reflector: IReflector,
        event_bus: IEventBus,
        logger: ILogger,
    ) -> None:
        self.__is_physical_channel = is_physical_channel
        self.__reflector = reflector
        self.__event_bus = event_bus
        self.__logger = logger

    async def __call__(self, event: Event) -> None:

        channel_name = event["Channel"]
        new_state = event["ChannelStateDesc"]
        linked_id = event["Linkedid"]

        if not self.__is_physical_channel(channel_name):
            return

        channel = await self.__reflector.get_channel_by_name(channel_name)
        root_channel = await self.__reflector.get_channel_by_unique_id(linked_id)

        if channel.unique_id == root_channel.unique_id:
            return


        # Определяем agent и trunk канал
        def split_channels(ch1, ch2):
            if "sbc" in ch1.name:
                return ch2, ch1
            if "sbc" in ch2.name:
                return ch1, ch2
            return ch1, ch2

        agent_channel, trunk_channel = split_channels(channel, root_channel)
        if new_state == "Ringing":
            await self.__reflector.update_channel_state(channel_name, new_state)
            agent_endpoint = extract_endpoint(agent_channel.name)
            ringing_telephony_event = RingingTelephonyEvent(
                caller_phone_number=agent_endpoint,
                called_phone_number=channel.phone,
                created_at=datetime.now(),
            )

            await self.__event_bus.publish(ringing_telephony_event)
            return

        if new_state == "Up":
            await self.__reflector.update_channel_state(channel_name, new_state)
            agent_endpoint = extract_endpoint(agent_channel.name)
            call_created_telephony_event = CallCreatedTelephonyEvent(
                unique_id=linked_id,
                caller_phone_number=agent_endpoint,
                called_phone_number=trunk_channel.phone,
                created_at=datetime.now()
            )

            await self.__event_bus.publish(call_created_telephony_event)
            return
