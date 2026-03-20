import json
import os
import re
from typing import Any
from typing import Coroutine
from typing import Callable
from typing import Optional
from typing import Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import aiofiles
from aiomysql.connection import Connection
from pymysql.err import MySQLError

from asterisk_ng.interfaces import File
from asterisk_ng.interfaces import Filetype
from asterisk_ng.interfaces import IGetRecordFileByUniqueIdQuery
from asterisk_ng.system.logger import ILogger

from .configs import RecordsProviderPluginConfig
from .utils import is_valid_unique_id


__all__ = ["GetRecordFileByUniqueIdQuery"]


class GetRecordFileByUniqueIdQuery(IGetRecordFileByUniqueIdQuery):

    __EXTENSIONS = {
        "mp3": Filetype.MP3,
        "mp4": Filetype.MP3,
        "wav": Filetype.WAV,
        "wave": Filetype.WAVE,
    }

    __slots__ = (
        "__config",
        "__get_connection",
        "__connection",
        "__logger",
    )

    def __init__(
        self,
        config: RecordsProviderPluginConfig,
        get_connection: Callable[[], Coroutine[Any, Any, Connection]],
        logger: ILogger,
    ) -> None:
        self.__config = config
        self.__get_connection = get_connection
        self.__logger = logger
        self.__connection: Optional[Connection] = None

    @classmethod
    async def __get_content_from_file(cls, path: str) -> bytes:
        async with aiofiles.open(path, mode='rb') as f:
            content = await f.read()
        return content

    @classmethod
    def __get_filetype(cls, filename: str) -> Filetype:
        extension = filename.split(".")[-1].lower()
        return cls.__EXTENSIONS[extension]

    @classmethod
    def __extract_client_from_record_filename(cls, filename: str) -> Optional[str]:
        match = re.match(r"^25_([^|]+)\|", filename)
        if match is None:
            return None
        return match.group(1)

    async def __search_filename_in_external_service(self, unique_id: str) -> str:
        """
        Backward-compatible helper for legacy deployed call paths.
        Some running instances still call this method directly.
        """
        service_url = self.__config.external_records_service_url
        client_id = self.__config.external_records_service_default_client
        if service_url is None or client_id is None:
            raise FileNotFoundError(f"File with unique_id: `{unique_id}` not found in external service.")

        request = Request(
            f"{service_url.rstrip('/')}/search-file",
            data=json.dumps({
                "X-Client": client_id,
                "term": unique_id,
                "type": "contains",
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=self.__config.external_records_service_timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        files = payload.get("files") or []
        if len(files) == 0 or files[0].get("original_filename") is None:
            raise FileNotFoundError(f"File with unique_id: `{unique_id}` not found in external service.")
        return files[0]["original_filename"]



    async def __search_record_in_external_service(self, filename: str, client_id: str) -> str:
        service_url = self.__config.external_records_service_url
        if service_url is None:
            raise RuntimeError("external records service url is not configured")

        timeout = self.__config.external_records_service_timeout

        await self.__logger.info(
            f"[records_provider] external /search-file start filename={filename} client={client_id}"
        )

        search_request = Request(
            f"{service_url.rstrip('/')}/search-file",
            data=json.dumps({
                "X-Client": client_id,
                "term": filename,
                "type": "exact_name",
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urlopen(search_request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError) as exc:
            raise RuntimeError(f"Failed to call search-file endpoint: {exc}")

        files = payload.get("files") or []
        if len(files) == 0:
            raise FileNotFoundError(f"File `{filename}` not found in external service.")

        resolved_filename = files[0].get("original_filename")
        if resolved_filename is None:
            raise FileNotFoundError(f"File `{filename}` not found in external service.")

        return resolved_filename

    async def __fetch_file_from_external_service(self, filename: str) -> bytes:
        service_url = self.__config.external_records_service_url
        if service_url is None:
            raise RuntimeError("external records service url is not configured")

        service_url = service_url.rstrip("/")

        client_id = self.__extract_client_from_record_filename(filename)
        if client_id is None:
            client_id = self.__config.external_records_service_default_client

        if client_id is None:
            raise ValueError(
                f"Unable to resolve X-Client for filename `{filename}`. "
                "Set external_records_service_default_client in config."
            )

        timeout = self.__config.external_records_service_timeout
        resolved_filename = await self.__search_record_in_external_service(filename=filename, client_id=client_id)

        await self.__logger.info(f"[records_provider] external /decrypt start filename={resolved_filename} client={client_id}")

        decrypt_request = Request(
            f"{service_url}/decrypt",
            data=json.dumps({
                "record_file": resolved_filename,
                "X-Client": client_id,
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urlopen(decrypt_request, timeout=timeout) as response:
                decrypt_payload = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError) as exc:
            raise RuntimeError(f"Failed to call decrypt endpoint: {exc}")

        download_url = decrypt_payload.get("download_url")
        if download_url is None:
            raise RuntimeError(f"External service response has no download_url: {decrypt_payload}")

        download_request = Request(f"{service_url}{download_url}", method="GET")
        try:
            with urlopen(download_request, timeout=timeout) as response:
                return response.read()
        except (HTTPError, URLError) as exc:
            raise RuntimeError(f"Failed to download decrypted file: {exc}")

    async def __get_fileinfo_by_uniqueid(self, unique_id: str) -> Tuple[str, str]:
        cleaned_unique_id = unique_id.strip()
        async with self.__connection.cursor() as cur:
            await cur.execute(
                f"SELECT {self.__config.calldate_column}, "
                f"{self.__config.recordingfile_column} "
                f"FROM {self.__config.cdr_table} "
                f"WHERE uniqueid=%s "
                f"AND {self.__config.recordingfile_column} IS NOT NULL "
                f"AND {self.__config.recordingfile_column} <> '' "
                f"ORDER BY {self.__config.calldate_column} DESC LIMIT 1",
                (cleaned_unique_id,),
            )
            row = await cur.fetchone()
            if row is None:
                await self.__logger.info(
                    f"[records_provider] uniqueid lookup miss for {cleaned_unique_id} in table {self.__config.cdr_table}"
                )
                raise FileNotFoundError(f"File with unique_id: `{cleaned_unique_id}` not found by uniqueid.")

            await self.__logger.info(
                f"[records_provider] uniqueid match type=exact key={cleaned_unique_id} table={self.__config.cdr_table}"
            )
            return row

    async def __get_fileinfo(self, unique_id: str) -> Tuple[str, str]:
        return await self.__get_fileinfo_by_uniqueid(unique_id)

    async def __call__(self, unique_id: str) -> File:
        await self.__logger.info(
            f"[records_provider] resolve record unique_id={unique_id} table={self.__config.cdr_table} "
            f"external_url={self.__config.external_records_service_url}"
        )

        if not is_valid_unique_id(unique_id):
            raise ValueError(f"Invalid unique_id: `{unique_id}`.")

        if self.__connection is None:
            self.__connection = await self.__get_connection()

        filename = None
        date = None

        try:
            date, filename = await self.__get_fileinfo(unique_id=unique_id)
        except (RuntimeError, MySQLError):
            self.__connection = await self.__get_connection()
            date, filename = await self.__get_fileinfo(unique_id=unique_id)
        except FileNotFoundError:
            if self.__config.external_records_service_url is None:
                raise

        if self.__config.external_records_service_url is not None:
            if filename is None:
                await self.__logger.info(f"[records_provider] DB lookup miss for {unique_id}; fallback to external /search-file")
                filename = await self.__search_filename_in_external_service(unique_id=unique_id)

            content = await self.__fetch_file_from_external_service(filename=filename)
            filetype = self.__get_filetype(filename)
            return File(
                name=filename,
                type=filetype,
                content=content,
            )

        if self.__config.external_records_service_url is not None:
            content = await self.__fetch_file_from_external_service(filename=filename)
            filetype = self.__get_filetype(filename)
            return File(
                name=filename,
                type=filetype,
                content=content,
            )

        if self.__config.external_records_service_url is not None:
            content = await self.__fetch_file_from_external_service(filename=filename)
            filetype = self.__get_filetype(filename)
            return File(
                name=filename,
                type=filetype,
                content=content,
            )

        if self.__config.external_records_service_url is not None:
            content = await self.__fetch_file_from_external_service(filename=filename)
            filetype = self.__get_filetype(filename)
            return File(
                name=filename,
                type=filetype,
                content=content,
            )

        if self.__config.external_records_service_url is not None:
            content = await self.__fetch_file_from_external_service(filename=filename)
            filetype = self.__get_filetype(filename)
            return File(
                name=filename,
                type=filetype,
                content=content,
            )

        if self.__config.external_records_service_url is not None:
            content = await self.__fetch_file_from_external_service(filename=filename)
            filetype = self.__get_filetype(filename)
            return File(
                name=filename,
                type=filetype,
                content=content,
            )

        if self.__config.external_records_service_url is not None:
            content = await self.__fetch_file_from_external_service(filename=filename)
            filetype = self.__get_filetype(filename)
            return File(
                name=filename,
                type=filetype,
                content=content,
            )

        if self.__config.external_records_service_url is not None:
            content = await self.__fetch_file_from_external_service(filename=filename)
            filetype = self.__get_filetype(filename)
            return File(
                name=filename,
                type=filetype,
                content=content,
            )

        if self.__config.external_records_service_url is not None:
            content = await self.__fetch_file_from_external_service(filename=filename)
            filetype = self.__get_filetype(filename)
            return File(
                name=filename,
                type=filetype,
                content=content,
            )

        if self.__config.external_records_service_url is not None:
            content = await self.__fetch_file_from_external_service(filename=filename)
            filetype = self.__get_filetype(filename)
            return File(
                name=filename,
                type=filetype,
                content=content,
            )

        directory_path = date.strftime(self.__config.media_root).rstrip('/')
        file_path = os.path.join(directory_path, filename)

        if not os.path.exists(file_path):
            raise FileNotFoundError(
                f"File with unique_id: `{unique_id}` not found,  file_path: `{file_path}`."
            )

        content = await self.__get_content_from_file(file_path)

        filetype = self.__get_filetype(filename)

        return File(
            name=filename,
            type=filetype,
            content=content,
        )
