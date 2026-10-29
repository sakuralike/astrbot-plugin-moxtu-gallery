from __future__ import annotations

import asyncio
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Iterable
from urllib.parse import urljoin, urlparse

import aiohttp


class LskyError(RuntimeError):
    """A normalized Lsky API failure."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "request_failed",
        status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.retry_after = retry_after


@dataclass(slots=True)
class LskySettings:
    base_url: str = "https://moxtu.sbs"
    api_version: str = "auto"
    api_prefix: str = "/api/v2"
    token: str = ""
    album_id: int = 0
    storage_id: int = 0
    strategy_id: int = 0
    upload_is_public: bool = True
    request_timeout: float = 15.0
    request_retries: int = 2
    api_concurrency: int = 4
    allow_private_reply: bool = False
    private_url_ttl: int = 300


@dataclass(slots=True)
class LskyPhoto:
    id: int | str | None = None
    key: str = ""
    filename: str = ""
    mimetype: str = ""
    extension: str = ""
    md5: str = ""
    sha1: str = ""
    width: int | None = None
    height: int | None = None
    public_url: str = ""
    thumbnail_url: str = ""
    visibility: str = ""
    source: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _status_is_success(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "ok", "success", "true", "successful"}


def _message_from_payload(payload: Any, fallback: str) -> str:
    if isinstance(payload, dict):
        for key in ("message", "msg", "error", "detail"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        data = payload.get("data")
        if isinstance(data, dict):
            for key in ("message", "msg", "error", "detail"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return fallback


def _data_payload(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    data = payload.get("data")
    if isinstance(data, dict) and "data" in data:
        return data
    return data


def _photo(value: Any, *, source: str = "") -> LskyPhoto:
    if not isinstance(value, dict):
        return LskyPhoto(source=source)
    links = value.get("links")
    url = (
        value.get("public_url")
        or value.get("url")
        or links.get("url")
        if isinstance(links, dict)
        else value.get("public_url") or value.get("url") or ""
    )
    thumbnail = (
        value.get("thumbnail_url")
        or value.get("thumb_url")
        or value.get("thumbnail")
        or (links.get("thumbnail_url") if isinstance(links, dict) else "")
        or ""
    )
    visibility = value.get("visibility")
    if not visibility:
        is_public = value.get("is_public")
        permission = value.get("permission")
        if is_public in (True, 1, "1", "true", "public"):
            visibility = "public"
        elif is_public in (False, 0, "0", "false", "private"):
            visibility = "private"
        elif str(permission or "").strip().lower() in {
            "public",
            "1",
            "true",
        }:
            visibility = "public"
        elif str(permission or "").strip().lower() in {
            "private",
            "0",
            "false",
        }:
            visibility = "private"
    return LskyPhoto(
        id=value.get("id") or value.get("photo_id") or value.get("key"),
        key=str(value.get("key") or value.get("pathname") or value.get("path") or ""),
        filename=str(value.get("filename") or value.get("name") or ""),
        mimetype=str(value.get("mimetype") or value.get("mime_type") or ""),
        extension=str(value.get("extension") or ""),
        md5=str(value.get("md5") or ""),
        sha1=str(value.get("sha1") or ""),
        width=_as_int(value.get("width")),
        height=_as_int(value.get("height")),
        public_url=str(url or ""),
        thumbnail_url=str(thumbnail or ""),
        visibility=str(visibility or ""),
        source=source,
        raw=dict(value),
    )


class LskyClient:
    """Small async client for the Lsky v1 and v2 JSON APIs."""

    def __init__(
        self,
        settings: LskySettings,
        *,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self.settings = settings
        self._session = session
        self._owns_session = session is None
        self._resolved_version: str | None = None
        self._request_semaphore = asyncio.Semaphore(
            max(1, min(16, int(settings.api_concurrency)))
        )

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(
                total=max(3.0, float(self.settings.request_timeout)),
                connect=min(3.0, max(1.0, float(self.settings.request_timeout))),
            )
            self._session = aiohttp.ClientSession(timeout=timeout)
            self._owns_session = True
        return self._session

    def _version(self) -> str:
        version = str(self.settings.api_version or "auto").strip().lower()
        if version in {"v1", "v2"}:
            return version
        return self._resolved_version or "v2"

    def _path(self, path: str, version: str | None = None) -> str:
        path = "/" + path.lstrip("/")
        version = version or self._version()
        if version == "v1":
            prefix = "/api/v1"
        else:
            prefix = str(self.settings.api_prefix or "/api/v2").strip()
            if not prefix.startswith("/"):
                prefix = "/" + prefix
            prefix = prefix.rstrip("/")
        return f"{prefix}{path}"

    def _url(self, path: str, version: str | None = None) -> str:
        base = str(self.settings.base_url or "").strip()
        if not base:
            raise LskyError("图床地址未配置", code="configuration")
        if not base.endswith("/"):
            base += "/"
        parsed = urlparse(base)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise LskyError("图床地址无效", code="configuration")
        return urljoin(base, self._path(path, version).lstrip("/"))

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.settings.token:
            headers["Authorization"] = f"Bearer {self.settings.token}"
        return headers

    async def _request_once(
        self,
        method: str,
        path: str,
        *,
        version: str | None = None,
        data: Any = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        session = await self._get_session()
        request_headers = self._headers()
        if headers:
            request_headers.update(headers)
        try:
            async with self._request_semaphore:
                async with session.request(
                    method,
                    self._url(path, version),
                    params=params,
                    data=data,
                    headers=request_headers,
                    allow_redirects=False,
                ) as response:
                    text = await response.text()
                    try:
                        payload = json.loads(text) if text else {}
                    except json.JSONDecodeError:
                        payload = {}
                    if 300 <= response.status < 400:
                        raise LskyError(
                            "图床接口不接受重定向",
                            code="redirect",
                            status=response.status,
                        )
                    if response.status >= 400:
                        code = {
                            401: "unauthorized",
                            403: "forbidden",
                            413: "too_large",
                            422: "validation",
                            429: "rate_limited",
                        }.get(response.status, "upstream_error")
                        retry_after = None
                        try:
                            retry_after = float(
                                response.headers.get("Retry-After", "")
                            )
                        except (TypeError, ValueError):
                            pass
                        raise LskyError(
                            _message_from_payload(
                                payload, f"图床请求失败（HTTP {response.status}）"
                            ),
                            code=code,
                            status=response.status,
                            retry_after=retry_after,
                        )
                    if isinstance(
                        payload, dict
                    ) and "status" in payload and not _status_is_success(
                        payload.get("status")
                    ):
                        raise LskyError(
                            _message_from_payload(payload, "图床拒绝了请求"),
                            code="business_error",
                            status=response.status,
                        )
                    return payload
        except LskyError:
            raise
        except asyncio.TimeoutError as error:
            raise LskyError("图床请求超时", code="timeout") from error
        except aiohttp.ClientError as error:
            raise LskyError("图床网络请求失败", code="network_error") from error

    async def _request(
        self,
        method: str,
        path: str,
        *,
        version: str | None = None,
        data: Any = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        retryable = method.upper() in {"GET", "HEAD"}
        retries = max(0, min(3, int(self.settings.request_retries))) if retryable else 0
        for attempt in range(retries + 1):
            try:
                return await self._request_once(
                    method,
                    path,
                    version=version,
                    data=data,
                    params=params,
                    headers=headers,
                )
            except LskyError as error:
                can_retry = (
                    retryable
                    and attempt < retries
                    and (
                        (error.status is not None and error.status >= 500)
                        or error.code in {"network_error", "timeout"}
                    )
                )
                if not can_retry:
                    raise
                await asyncio.sleep(min(1.5, 0.25 * (2**attempt)))

    async def _request_with_version_fallback(
        self,
        method: str,
        path: str,
        *,
        data: Any = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        configured = str(self.settings.api_version or "auto").strip().lower()
        if configured in {"v1", "v2"}:
            return await self._request(
                method, path, version=configured, data=data, params=params, headers=headers
            )
        try:
            payload = await self._request(
                method, path, version="v2", data=data, params=params, headers=headers
            )
            self._resolved_version = "v2"
            return payload
        except LskyError as error:
            if error.status not in {404, 405}:
                raise
            payload = await self._request(
                method, path, version="v1", data=data, params=params, headers=headers
            )
            self._resolved_version = "v1"
            return payload

    async def upload(
        self,
        file_path: str | Path,
        *,
        album_id: int | None = None,
        is_public: bool | None = None,
        intro: str = "",
        content_type: str | None = None,
    ) -> LskyPhoto:
        path = Path(file_path)
        if not path.is_file():
            raise LskyError("待上传图片不存在", code="local_file")
        chosen_album = self.settings.album_id if album_id is None else album_id
        configured = str(self.settings.api_version or "auto").strip().lower()
        if configured in {"v1", "v2"}:
            versions = [configured]
        else:
            versions = []
            if self.settings.storage_id:
                versions.append("v2")
            if self.settings.strategy_id:
                versions.append("v1")
            if not versions:
                raise LskyError(
                    "未配置 v2 储存策略 ID 或 v1 储存策略 ID",
                    code="configuration",
                )
        last_error: LskyError | None = None
        for version in versions:
            try:
                fields: dict[str, str] = {}
                public = self.settings.upload_is_public if is_public is None else is_public
                if version == "v1":
                    if not self.settings.strategy_id:
                        raise LskyError("未配置 v1 储存策略 ID", code="configuration")
                    fields["strategy_id"] = str(self.settings.strategy_id)
                    fields["permission"] = "1" if public else "0"
                else:
                    if not self.settings.storage_id:
                        raise LskyError("未配置 v2 储存策略 ID", code="configuration")
                    fields["storage_id"] = str(self.settings.storage_id)
                    fields["is_public"] = "1" if public else "0"
                if chosen_album:
                    fields["album_id"] = str(chosen_album)
                if intro and version == "v2":
                    fields["intro"] = intro[:500]
                form = aiohttp.FormData()
                for key, value in fields.items():
                    form.add_field(key, value)
                with path.open("rb") as handle:
                    form.add_field(
                        "file",
                        handle,
                        filename=path.name,
                        content_type=content_type or "application/octet-stream",
                    )
                    payload = await self._request(
                        "POST",
                        "/upload",
                        version=version,
                        data=form,
                    )
                self._resolved_version = version
                return _photo(_data_payload(payload), source="upload")
            except LskyError as error:
                last_error = error
                if configured in {"v1", "v2"} or error.status not in {404, 405}:
                    raise
        raise last_error or LskyError("图床上传失败")

    async def list_public_photos(
        self,
        *,
        page: int = 1,
        per_page: int = 40,
        order: str = "newest",
    ) -> tuple[list[LskyPhoto], dict[str, Any]]:
        if str(self.settings.api_version or "auto").strip().lower() == "v1":
            raise LskyError("v1 接口不支持公共广场", code="unsupported")
        payload = await self._request(
            "GET",
            "/explore/photos",
            version="v2",
            params={
                "page": max(1, page),
                "per_page": min(100, max(1, per_page)),
                "order": order,
            },
        )
        self._resolved_version = "v2"
        data = _data_payload(payload)
        items, meta = self._items_and_meta(data)
        return [_photo(item, source="public") for item in items], meta

    async def list_public_album_photos(
        self,
        album_id: int,
        *,
        page: int = 1,
        per_page: int = 40,
    ) -> tuple[list[LskyPhoto], dict[str, Any]]:
        if str(self.settings.api_version or "auto").strip().lower() == "v1":
            raise LskyError("v1 接口不支持公开相册查询", code="unsupported")
        payload = await self._request(
            "GET",
            f"/explore/albums/{int(album_id)}/photos",
            version="v2",
            params={"page": max(1, page), "per_page": min(100, max(1, per_page))},
        )
        self._resolved_version = "v2"
        data = _data_payload(payload)
        items, meta = self._items_and_meta(data)
        return [_photo(item, source="album_public") for item in items], meta

    async def list_user_photos(
        self,
        *,
        album_id: int | None = None,
        page: int = 1,
        per_page: int = 40,
    ) -> tuple[list[LskyPhoto], dict[str, Any]]:
        chosen_album = self.settings.album_id if album_id is None else album_id
        params: dict[str, Any] = {
            "page": max(1, page),
            "album_id": chosen_album or "",
        }
        configured = str(self.settings.api_version or "auto").strip().lower()
        if configured == "v1" or self._version() == "v1":
            payload = await self._request("GET", "/images", version="v1", params=params)
        else:
            params["per_page"] = min(100, max(1, per_page))
            try:
                payload = await self._request("GET", "/user/photos", version="v2", params=params)
                self._resolved_version = "v2"
            except LskyError as error:
                if configured != "auto" or error.status not in {404, 405}:
                    raise
                payload = await self._request("GET", "/images", version="v1", params=params)
                self._resolved_version = "v1"
        data = _data_payload(payload)
        items, meta = self._items_and_meta(data)
        return [_photo(item, source="album_private") for item in items], meta

    async def private_url(self, photo_id: int | str) -> str:
        if str(self.settings.api_version or "auto").strip().lower() == "v1":
            raise LskyError("v1 接口不支持私有图片签名链接", code="unsupported")
        minutes = max(1, int(math.ceil(max(1, int(self.settings.private_url_ttl)) / 60)))
        payload = await self._request(
            "POST",
            f"/user/photos/{photo_id}/private-url",
            version="v2",
            data=json.dumps({"minutes": minutes}),
            headers={"Content-Type": "application/json"},
        )
        self._resolved_version = "v2"
        data = _data_payload(payload)
        if isinstance(data, dict):
            url = data.get("url") or data.get("private_url") or data.get("signed_url")
            if isinstance(url, str) and url:
                return url
        raise LskyError("图床未返回私有图片链接", code="invalid_response")

    async def list_albums(self, *, page: int = 1) -> list[dict[str, Any]]:
        configured = str(self.settings.api_version or "auto").strip().lower()
        params = {"page": max(1, page)}
        if configured == "v1" or self._version() == "v1":
            payload = await self._request("GET", "/albums", version="v1", params=params)
        else:
            try:
                payload = await self._request("GET", "/user/albums", version="v2", params=params)
                self._resolved_version = "v2"
            except LskyError as error:
                if configured != "auto" or error.status not in {404, 405}:
                    raise
                payload = await self._request("GET", "/albums", version="v1", params=params)
                self._resolved_version = "v1"
        data = _data_payload(payload)
        items, _ = self._items_and_meta(data)
        return [dict(item) for item in items if isinstance(item, dict)]

    @staticmethod
    def _items_and_meta(data: Any) -> tuple[list[Any], dict[str, Any]]:
        if isinstance(data, list):
            return data, {}
        if isinstance(data, dict):
            items = data.get("data")
            if isinstance(items, list):
                return items, data
            for key in ("items", "photos", "albums"):
                value = data.get(key)
                if isinstance(value, list):
                    return value, data
        return [], {}

    async def close(self) -> None:
        if self._owns_session and self._session is not None and not self._session.closed:
            await self._session.close()
