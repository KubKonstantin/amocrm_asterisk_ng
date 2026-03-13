from typing import Dict, Mapping

from asterisk_ng.interfaces import CallDomainModel
from asterisk_ng.interfaces import IRedirectTelephonyCommand
from asterisk_ng.interfaces import CrmUserId
from asterisk_ng.interfaces import IRedirectDomainCommand

__all__ = ["RedirectDomainCommandImpl"]


class RedirectDomainCommandImpl(IRedirectDomainCommand):

    __slots__ = (
        "__active_calls",
        "__redirect_telephony_command",
    )

    def __init__(
        self,
        active_calls: Dict[CrmUserId, CallDomainModel],
        redirect_telephony_command: IRedirectTelephonyCommand,
    ) -> None:
        self.__active_calls = active_calls
        self.__redirect_telephony_command = redirect_telephony_command

    async def __call__(self, user_id: CrmUserId, phone_number: str) -> None:
        #call_model = self.__active_calls[user_id]
        call_model = self.__active_calls.get(user_id)

        if call_model is None:
            return

         # выполняем redirect в Asterisk
        await self.__redirect_telephony_command(
            phone_number=call_model.client_phone_number,
            redirect_phone_number=phone_number,
        )

# переносим активный звонок на нового агента
        for uid in list(self.__active_calls.keys()):
            if uid == user_id:
                continue

            other_call = self.__active_calls[uid]

            if other_call.client_phone_number == call_model.client_phone_number:
                self.__active_calls.pop(user_id, None)
                self.__active_calls[uid] = call_model
                break
