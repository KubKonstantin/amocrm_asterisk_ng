from asterisk_ng.system.dispatcher import IQuery

from typing import Optional

from ..models import CallDomainModel
from ...crm_system import CrmUserId


__all__ = ["IGetAgentCallQuery"]


class IGetAgentCallQuery(IQuery[Optional[CallDomainModel]]):

    __slots__ = ()

    async def __call__(self, agent_id: CrmUserId) -> Optional[CallDomainModel]:
        raise NotImplementedError()
