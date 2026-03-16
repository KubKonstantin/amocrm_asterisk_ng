from asterisk_ng.interfaces import IRedirectTelephonyCommand

from asterisk_ng.plugins.telephony.ami_manager import Action
from asterisk_ng.plugins.telephony.ami_manager import IAmiManager

from ...reflector import IReflector

from asterisk_ng.system.event_bus import IEventBus


__all__ = ["RedirectTelephonyCommandImpl"]


class RedirectTelephonyCommandImpl(IRedirectTelephonyCommand):

    __slots__ = (
        "__ami_manager",
        "__reflector",
        "__event_bus",
        "__context",
    )

    def __init__(
        self,
        ami_manager: IAmiManager,
        reflector: IReflector,
        event_bus: IEventBus,
        context: str
    ) -> None:
        self.__ami_manager = ami_manager
        self.__reflector = reflector
        self.__event_bus = event_bus
        self.__context = context

    def __is_agent_channel(self, channel_name: str) -> bool:
        try:
            endpoint = channel_name.split("/")[1].split("-")[0]
        except Exception:
            return False
        return endpoint.startswith("vipma_")

    def __is_preferred_client_channel(self, channel_name: str) -> bool:
        return "sbc" in channel_name.lower()

    async def __resolve_redirect_channel(self, phone_number: str):
        channel = await self.__reflector.get_channel_by_phone(phone=phone_number)

        if not self.__is_agent_channel(channel.name):
            return channel

        try:
            call = await self.__reflector.get_call(channel.linked_id)
        except KeyError:
            return channel

        best_candidate = None

        for channel_unique_id in call.channels_unique_ids:
            try:
                candidate = await self.__reflector.get_channel_by_unique_id(channel_unique_id)
            except KeyError:
                continue

            if candidate.phone != phone_number:
                continue

            if self.__is_agent_channel(candidate.name):
                continue

            if self.__is_preferred_client_channel(candidate.name):
                return candidate

            best_candidate = candidate

        return best_candidate or channel

    async def __call__(
        self,
        phone_number: str,
        redirect_phone_number: str,
    ) -> None:
        channel = await self.__resolve_redirect_channel(phone_number)

        action = Action(
            name="Redirect",
            parameters={
                "Channel": channel.name,
                "Exten": redirect_phone_number,
                "Context": self.__context,
                "Priority": 1,
            }
        )
        await self.__ami_manager.send_action(action)
