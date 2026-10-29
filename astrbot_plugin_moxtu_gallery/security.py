from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlparse


class SecurityError(ValueError):
    pass


_BEARER_RE = re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]+")


def redact_secret(value: object) -> str:
    text = str(value)
    text = _BEARER_RE.sub(r"\1<redacted>", text)
    text = re.sub(r"(?i)(api[_-]?key|token|password|secret)(\s*[:=]\s*)\S+", r"\1\2<redacted>", text)
    return text


def _is_private_host(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host.lower() in {"localhost", "localhost.localdomain"}
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def validate_image_url(
    value: str,
    *,
    allowed_hosts: set[str] | list[str] | tuple[str, ...],
    allow_local_http: bool = False,
) -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise SecurityError("图片地址必须使用 HTTPS")
    if parsed.username or parsed.password:
        raise SecurityError("图片地址不能包含用户凭据")
    host = parsed.hostname.rstrip(".").lower()
    local_http = False
    if parsed.scheme == "http":
        if not allow_local_http:
            raise SecurityError("图片地址必须使用 HTTPS")
        if host not in {"localhost", "localhost.localdomain", "127.0.0.1", "::1"}:
            raise SecurityError("图片地址必须使用 HTTPS")
        local_http = True
    if parsed.port not in (None, 80, 443) and not local_http:
        raise SecurityError("图片地址端口不允许")
    if _is_private_host(host) and not local_http:
        raise SecurityError("图片地址指向受限主机")
    normalized_hosts = {str(item).strip().rstrip(".").lower() for item in allowed_hosts if str(item).strip()}
    if normalized_hosts and host not in normalized_hosts:
        raise SecurityError("图片地址主机不在允许列表")
    return url


def ensure_https_api_url(value: str, *, allow_local_http: bool = False) -> str:
    parsed = urlparse(str(value or "").strip())
    if parsed.scheme == "https" and parsed.hostname:
        return str(value).strip().rstrip("/")
    if (
        allow_local_http
        and parsed.scheme == "http"
        and parsed.hostname
        and parsed.hostname.lower() in {"localhost", "127.0.0.1", "::1"}
    ):
        return str(value).strip().rstrip("/")
    raise SecurityError("图床 API 地址必须使用 HTTPS")


def bounded_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return min(maximum, max(minimum, number))
