from datetime import datetime
from typing import Callable

from asterisk_ng.interfaces import CallCompletedTelephonyEvent, CallStatus

from asterisk_ng.plugins.telephony.ami_manager import Event
from asterisk_ng.plugins.telephony.ami_manager import IAmiEventHandler

from asterisk_ng.system.event_bus import IEventBus
from asterisk_ng.system.logger import ILogger

from ...core import IReflector


__all__ = [
    "HangupEventHandler",
]


def extract_endpoint(channel_name: str) -> str:
    """
    PJSIP/vipma_kkubeev-00000008 -> vipma_kkubeev
    """
    try:
        return channel_name.split("/")[1].split("-")[0]
    except Exception:
        return channel_name



class HangupEventHandler(IAmiEventHandler):

    __slots__ = (
        "__is_physical_channel",
        "__event_bus",
        "__reflector",
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
        linked_id = event["Linkedid"]

        if "sbc" in channel_name:
            return

        if not self.__is_physical_channel(channel_name):
            return

        try:
            root_channel = await self.__reflector.get_channel_by_name(channel_name)
        except KeyError:
            return

        try:
            call = await self.__reflector.get_call(linked_id)
        except KeyError:
            return

        agent_endpoint = extract_endpoint(root_channel.name)

        # ---- ищем другие agent каналы ----
        other_agent_exists = False
        client_phone = None
        disposition = CallStatus.NO_ANSWER

        for channel_unique_id in call.channels_unique_ids:

            if channel_unique_id == root_channel.unique_id:
                continue

            try:
                ch = await self.__reflector.get_channel_by_unique_id(channel_unique_id)
            except KeyError:
                continue

            endpoint = extract_endpoint(ch.name)

            if endpoint.startswith("vipma_"):
                other_agent_exists = True

            if endpoint != agent_endpoint and "vipma_" not in endpoint:
                client_phone = ch.phone

            if ch.state.lower() == "up":
                disposition = CallStatus.ANSWERED

        # если есть другой агент — это transfer
        if other_agent_exists:
            await self.__logger.debug(
                f"Skip hangup completion (transfer). linkedid={linked_id}"
            )

            await self.__reflector.delete_channel_from_call(
                linked_id,
                root_channel.unique_id
            )

            return

        # fallback
        if client_phone is None:
            client_phone = root_channel.phone

        call_completed_event = CallCompletedTelephonyEvent(
            unique_id=linked_id,
            disposition=disposition,
            caller_phone_number=agent_endpoint,
            called_phone_number=client_phone,
            created_at=datetime.now(),
        )

        # удаляем канал
        await self.__reflector.delete_channel_from_call(
            linked_id,
            root_channel.unique_id
        )

        # сохраняем под linked_id и всеми известными unique_id звонка,
        # чтобы CDR мог найти событие даже если приходит без Linkedid
        completion_keys = set(call.channels_unique_ids)
        completion_keys.add(linked_id)
        completion_keys.add(root_channel.unique_id)

        completion_event_keys = set()
        for completion_key in completion_keys:
            completion_event_keys.add(completion_key)
            completion_event_keys.add(f"{completion_key}-agent-{agent_endpoint}")

        for completion_event_key in sorted(completion_event_keys):
            await self.__reflector.save_call_completed_event(
                completion_event_key,
                call_completed_event,
            )

        await self.__event_bus.publish(call_completed_event)
