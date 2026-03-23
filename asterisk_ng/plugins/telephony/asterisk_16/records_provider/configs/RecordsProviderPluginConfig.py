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
    external_records_service_url: Optional[str] = "http://127.0.0.1:5000"
    external_records_service_timeout: int = 20
    external_records_service_default_client: Optional[str] = "vipma"
