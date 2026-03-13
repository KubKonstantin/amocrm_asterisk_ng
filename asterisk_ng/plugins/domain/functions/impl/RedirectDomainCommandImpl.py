from typing import Dict

from asterisk_ng.interfaces import CallDomainModel
from asterisk_ng.interfaces import IGetCrmUserIdByPhoneQuery
from asterisk_ng.interfaces import IRedirectTelephonyCommand
from asterisk_ng.interfaces import CrmUserId
from asterisk_ng.interfaces import IRedirectDomainCommand

__all__ = ["RedirectDomainCommandImpl"]


class RedirectDomainCommandImpl(IRedirectDomainCommand):

    __slots__ = (
        "__active_calls",
        "__get_crm_user_id_by_phone_query",
        "__redirect_telephony_command",
    )

    def __init__(
        self,
        active_calls: Dict[CrmUserId, CallDomainModel],
        get_crm_user_id_by_phone_query: IGetCrmUserIdByPhoneQuery,
        redirect_telephony_command: IRedirectTelephonyCommand,
    ) -> None:
        self.__active_calls = active_calls
        self.__get_crm_user_id_by_phone_query = get_crm_user_id_by_phone_query
        self.__redirect_telephony_command = redirect_telephony_command

    async def __call__(self, user_id: CrmUserId, phone_number: str) -> None:
        call_model = self.__active_calls.get(user_id)

        if call_model is None:
            return

        redirect_phone_number = phone_number.strip()
        if not redirect_phone_number:
            return

        # выполняем redirect в Asterisk
        await self.__redirect_telephony_command(
            phone_number=call_model.client_phone_number,
            redirect_phone_number=redirect_phone_number,
        )

        # текущий агент должен завершить разговор в интерфейсе после трансфера
        self.__active_calls.pop(user_id, None)

        # переносим активный звонок на нового агента (если это известный внутренний номер)
        try:
            redirect_agent_id = await self.__get_crm_user_id_by_phone_query(redirect_phone_number)
        except KeyError:
            return


        # переносим активный звонок на нового агента (если это известный внутренний номер)
        try:
            redirect_agent_id = await self.__get_crm_user_id_by_phone_query(redirect_phone_number)
        except KeyError:
            return



        # переносим активный звонок на нового агента
        try:
            redirect_agent_id = await self.__get_crm_user_id_by_phone_query(phone_number)
        except KeyError:
            return

        self.__active_calls.pop(user_id, None)
        self.__active_calls[redirect_agent_id] = call_model.copy(update={"agent_id": redirect_agent_id})
