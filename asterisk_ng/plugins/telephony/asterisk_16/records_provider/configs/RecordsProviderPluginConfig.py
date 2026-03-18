from pydantic import BaseModel
from typing import Optional

from .MySqlConfig import MySqlConfig


__all__ = ["RecordsProviderPluginConfig"]


class RecordsProviderPluginConfig(BaseModel):
    mysql: MySqlConfig
    media_root: str = "/var/spool/asterisk/monitor/%Y/%m/%d/"
    cdr_table: str = "cdr"
    calldate_column: str = "calldate"
    recordingfile_column: str = "recordingfile"
    linkedid_column: str = "linkedid"
    external_records_service_url: Optional[str] = None
    external_records_service_timeout: int = 20
    external_records_service_default_client: Optional[str] = None
