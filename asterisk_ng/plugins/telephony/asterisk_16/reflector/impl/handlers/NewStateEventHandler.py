from typing import Callable
from datetime import datetime

from asterisk_ng.interfaces import CallCreatedTelephonyEvent, RingingTelephonyEvent

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

def is_external_phone(phone: str) -> bool:
    digits = "".join(ch for ch in phone if ch.isdigit())
    return len(digits) >= 10


def is_agent_endpoint(endpoint: str) -> bool:
    return endpoint.startswith("vipma_")


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

        agent_channel, _ = split_channels(channel, root_channel)
        channel_endpoint = extract_endpoint(channel.name)
        root_endpoint = extract_endpoint(root_channel.name)
        is_inbound_agent_leg = is_agent_endpoint(channel_endpoint) and not is_agent_endpoint(root_endpoint)

        if new_state == "Ringing":
            await self.__reflector.update_channel_state(channel_name, new_state)
            agent_endpoint = extract_endpoint(agent_channel.name)

            caller_phone_number = agent_endpoint
            called_phone_number = channel.phone

            if is_inbound_agent_leg:
                caller_phone_number = root_channel.phone or channel.phone
                called_phone_number = agent_endpoint

            ringing_telephony_event = RingingTelephonyEvent(
                caller_phone_number=caller_phone_number,
                called_phone_number=called_phone_number,
                created_at=datetime.now(),
            )

            await self.__event_bus.publish(ringing_telephony_event)
            return

        if new_state == "Up":

            await self.__reflector.update_channel_state(channel_name, new_state)

            agent_endpoint = extract_endpoint(agent_channel.name)

            # ищем номер клиента среди каналов звонка
            call = await self.__reflector.get_call(linked_id)

            client_phone = None

            for channel_unique_id in call.channels_unique_ids:
                try:
                    ch = await self.__reflector.get_channel_by_unique_id(channel_unique_id)

                    if ch.phone and is_external_phone(ch.phone):
                        client_phone = ch.phone
                        break

                except KeyError:
                    pass

            if client_phone is None and root_channel.phone and is_external_phone(root_channel.phone):
                client_phone = root_channel.phone

            if client_phone is None and channel.phone and is_external_phone(channel.phone):
                client_phone = channel.phone

            if client_phone is None:
                await self.__logger.debug(
                    f"Skip CallCreatedTelephonyEvent: client phone not found"
                )
                return
        
            caller_phone_number = agent_endpoint
            called_phone_number = client_phone

            if is_inbound_agent_leg:
                caller_phone_number = client_phone
                called_phone_number = agent_endpoint

            call_created_telephony_event = CallCreatedTelephonyEvent(
                unique_id=linked_id,
                caller_phone_number=caller_phone_number,
                called_phone_number=called_phone_number,
                created_at=datetime.now()
            )
        
            await self.__event_bus.publish(call_created_telephony_event)
        
            return        
