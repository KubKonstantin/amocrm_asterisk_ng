from datetime import datetime
from asyncio import sleep, create_task

from asterisk_ng.interfaces import CallReportReadyTelephonyEvent
from asterisk_ng.interfaces import CallStatus

from asterisk_ng.plugins.telephony.ami_manager import Event
from asterisk_ng.plugins.telephony.ami_manager import IAmiEventHandler

from asterisk_ng.system.event_bus import IEventBus
from asterisk_ng.system.logger import ILogger

from ...core import IReflector


__all__ = ["CdrEventHandler"]


class CdrEventHandler(IAmiEventHandler):

    __DISPOSITION_MAPPING = {
        "FAILED": CallStatus.FAILED,
        "ANSWERED": CallStatus.ANSWERED,
        "NO ANSWER": CallStatus.NO_ANSWER,
        "BUSY": CallStatus.BUSY,
        "CONGESTION": CallStatus.FAILED,
    }

    __slots__ = (
        "__reflector",
        "__event_bus",
        "__logger",
    )

    def __init__(
        self,
        reflector: IReflector,
        event_bus: IEventBus,
        logger: ILogger,
    ) -> None:
        self.__reflector = reflector
        self.__event_bus = event_bus
        self.__logger = logger

    def __convert_datetime(self, str_datetime) -> datetime:
        return datetime.strptime(str_datetime, "%Y-%m-%d %H:%M:%S")

    def __get_disposition(
        self,
        str_disposition: str
    ) -> CallStatus:
        try:
            return self.__DISPOSITION_MAPPING[str_disposition]
        except KeyError:
            raise ValueError(f"Unknown disposition {str_disposition}.")

    async def __call__(self, event: Event) -> None:
        task = create_task(self.call2(event))
        task.add_done_callback(self.__log_task_exception)

    def __log_task_exception(self, task) -> None:
        try:
            task.result()
        except Exception as exc:
            create_task(self.__logger.error(f"CdrEventHandler task failed: {exc}"))

    async def call2(self, event: Event) -> None:

        await sleep(3.0)

        cdr_linkedid = event.get("Linkedid")
        cdr_uniqueid = event.get("Uniqueid") or event.get("UniqueID")

        if not cdr_linkedid and not cdr_uniqueid:
            await self.__logger.error(f"Cdr event without linked id: {event}")
            return

        key_candidates = [
            key
            for key in (cdr_linkedid, cdr_uniqueid)
            if key is not None
        ]

        unique_id = event.get("Uniqueid") or event.get("UniqueID")
        if unique_id is None:
            await self.__logger.error(f"Cdr event without unique id: {event}")
            return

        cdr_event_key = f"{unique_id}:{event['StartTime']}:{event['EndTime']}:{event.get('Destination', '')}"
        if await self.__reflector.get_ignore_cdr_flag(cdr_event_key):
            return

        if await self.__reflector.get_ignore_cdr_flag(unique_id):
            return

        destination = event.get("Destination")
        destination_endpoint = destination if destination and destination.startswith("vipma_") else None

        lookup_candidates = []
        if destination_endpoint is not None:
            for key in key_candidates:
                lookup_candidates.append(f"{key}-agent-{destination_endpoint}")
        else:
            for key in key_candidates:
                lookup_candidates.append(key)

        call_completed_event = None
        call_completed_event_key = None

        for key in lookup_candidates:
            try:
                call_completed_event = await self.__reflector.get_call_completed_event(key)
                call_completed_event_key = key
                break
            except KeyError:
                continue

        if call_completed_event is None and destination_endpoint is not None:
            for key in key_candidates:
                try:
                    call_completed_event = await self.__reflector.get_call_completed_event(key)
                    call_completed_event_key = key
                    break
                except KeyError:
                    continue

        if call_completed_event is None:
            await self.__logger.info(
                f"Saved CallCompletedEvent not found. linkedid={cdr_linkedid} uniqueid={cdr_uniqueid}"
            )
            return

        caller_phone_number = call_completed_event.caller_phone_number
        called_phone_number = call_completed_event.called_phone_number

        duration = int(event["Duration"])
        str_disposition = event["Disposition"]
        str_start_time = event["StartTime"]
        str_end_time = event["EndTime"]

        start_datetime = self.__convert_datetime(str_start_time)
        end_datetime = self.__convert_datetime(str_end_time)

        if str_answer_time := event.get("AnswerTime", None):
            answer_datetime = self.__convert_datetime(str_answer_time)
        else:
            answer_datetime = None

        disposition = self.__get_disposition(str_disposition)

        if call_completed_event.disposition == CallStatus.ANSWERED and disposition != CallStatus.ANSWERED:
            return

        if call_completed_event.disposition != CallStatus.ANSWERED and disposition == CallStatus.ANSWERED:
            return

        call_report_ready_telephony_event = CallReportReadyTelephonyEvent(
            unique_id=unique_id,
            created_at=datetime.now(),
            caller_phone_number=caller_phone_number,
            called_phone_number=called_phone_number,
            duration=duration,
            disposition=disposition,
            call_start_at=start_datetime,
            call_end_at=end_datetime,
            answer_at=answer_datetime,
        )

        await self.__event_bus.publish(call_report_ready_telephony_event)
        await self.__reflector.set_ignore_cdr_flag(cdr_event_key)
        await self.__reflector.set_ignore_cdr_flag(unique_id)

        if called_phone_number is not None:
            delete_keys = set(lookup_candidates)
            delete_keys.add(call_completed_event_key)

            for key in delete_keys:
                await self.__reflector.delete_call_completed_event(key)
