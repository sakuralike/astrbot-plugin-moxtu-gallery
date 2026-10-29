from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

import aiohttp
from PIL import Image as PillowImage
from PIL import UnidentifiedImageError

from .security import SecurityError, validate_image_url


class ImagePipelineError(ValueError):
    """A user-safe image validation failure."""

    def __init__(self, message: str, *, code: str = "invalid_image") -> None:
        super().__init__(message)
        self.code = code


@dataclass(slots=True)
class ImageLimits:
    max_image_bytes: int = 10 * 1024 * 1024
    max_pixels: int = 20_000_000
    max_frames: int = 10
    allowed_formats: tuple[str, ...] = ("PNG", "JPEG", "WEBP", "GIF")
    allow_local_http: bool = False


@dataclass(slots=True)
class PreparedImage:
    path: Path
    sha256: str
    size_bytes: int
    mime_type: str
    format: str
    width: int
    height: int
    frames: int
    source_name: str = ""


def _component_is_image(component: object) -> bool:
    try:
        from astrbot.api.message_components import Image

        if isinstance(component, Image):
            return True
    except (ImportError, TypeError):
        pass
    return type(component).__name__.lower() == "image"


def find_image_components(messages: Iterable[object]) -> list[object]:
    return [component for component in messages if _component_is_image(component)]


def _source_url(component: object) -> str:
    for name in ("url", "src", "image_url", "origin", "file"):
        value = getattr(component, name, None)
        if isinstance(value, str) and value.strip():
            value = value.strip()
            if value.lower().startswith(("http://", "https://")):
                return value
    return ""


def _source_name(component: object) -> str:
    for name in ("filename", "file", "path", "url"):
        value = getattr(component, name, None)
        if isinstance(value, (str, Path)) and str(value).strip():
            return Path(str(value)).name[:180]
    return ""


def _safe_format(value: str) -> str:
    value = value.upper().strip()
    return "JPEG" if value in {"JPG", "JPEG"} else value


def _mime_for_format(image_format: str) -> str:
    return {
        "PNG": "image/png",
        "JPEG": "image/jpeg",
        "WEBP": "image/webp",
        "GIF": "image/gif",
    }.get(image_format, mimetypes.types_map.get(f".{image_format.lower()}", "application/octet-stream"))


def _copy_limited(source: Path, target: Path, maximum: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    try:
        with source.open("rb") as input_file, target.open("wb") as output_file:
            while True:
                chunk = input_file.read(128 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > maximum:
                    raise ImagePipelineError("图片超过大小限制。", code="too_large")
                digest.update(chunk)
                output_file.write(chunk)
    except OSError as error:
        raise ImagePipelineError("图片文件无法读取。", code="unreadable") from error
    if size <= 0:
        raise ImagePipelineError("图片文件为空。", code="empty")
    return digest.hexdigest(), size


def _validate_image(path: Path, limits: ImageLimits) -> tuple[str, int, int, int]:
    try:
        with PillowImage.open(path) as image:
            image_format = _safe_format(str(image.format or ""))
            if image_format not in {_safe_format(value) for value in limits.allowed_formats}:
                raise ImagePipelineError("图片格式不受支持。", code="unsupported_format")
            width, height = int(image.width), int(image.height)
            if width <= 0 or height <= 0:
                raise ImagePipelineError("图片尺寸无效。", code="invalid_dimensions")
            if width * height > limits.max_pixels:
                raise ImagePipelineError("图片像素超过限制。", code="too_many_pixels")
            try:
                frames = int(getattr(image, "n_frames", 1) or 1)
            except (TypeError, ValueError):
                frames = 1
            if frames > limits.max_frames:
                raise ImagePipelineError("图片帧数超过限制。", code="too_many_frames")
            image.verify()
        with PillowImage.open(path) as image:
            image.load()
    except ImagePipelineError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as error:
        raise ImagePipelineError("图片内容无法验证。", code="invalid_image") from error
    return image_format, width, height, frames


async def prepare_image_component(
    component: object,
    temp_dir: str | Path,
    *,
    limits: ImageLimits | None = None,
) -> PreparedImage:
    """Materialize one AstrBot Image component and validate its real contents."""

    limits = limits or ImageLimits()
    source_url = _source_url(component)
    if source_url:
        try:
            validate_image_url(
                source_url,
                allowed_hosts=(),
                allow_local_http=limits.allow_local_http,
            )
        except SecurityError as error:
            raise ImagePipelineError(str(error), code="unsafe_source") from error
    converter = getattr(component, "convert_to_file_path", None)
    if not callable(converter):
        raise ImagePipelineError("图片消息无法落地处理。", code="unreadable")
    try:
        source_value = await converter()
    except Exception as error:
        raise ImagePipelineError("图片下载或读取失败。", code="download_failed") from error
    source = Path(str(source_value))
    if not source.is_file():
        raise ImagePipelineError("图片文件无法读取。", code="unreadable")

    destination_dir = Path(temp_dir)
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / f"{uuid.uuid4().hex}.image"
    try:
        digest, size = _copy_limited(source, destination, limits.max_image_bytes)
        image_format, width, height, frames = _validate_image(destination, limits)
    except Exception:
        try:
            destination.unlink()
        except OSError:
            pass
        raise
    suffix = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "GIF": ".gif"}.get(
        image_format,
        ".image",
    )
    final_path = destination.with_suffix(suffix)
    try:
        destination.replace(final_path)
    except OSError:
        final_path = destination
    return PreparedImage(
        path=final_path,
        sha256=digest,
        size_bytes=size,
        mime_type=_mime_for_format(image_format),
        format=image_format,
        width=width,
        height=height,
        frames=frames,
        source_name=_source_name(component),
    )


def cleanup_prepared_image(image: PreparedImage | None) -> None:
    if image is None:
        return
    try:
        image.path.unlink(missing_ok=True)
    except OSError:
        pass


def cleanup_temp_dir(path: str | Path) -> None:
    try:
        shutil.rmtree(Path(path), ignore_errors=True)
    except OSError:
        pass


def create_temp_dir(base_dir: str | Path) -> Path:
    directory = Path(base_dir)
    directory.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="image-", dir=directory))


async def download_image_url(
    url: str,
    temp_dir: str | Path,
    *,
    allowed_hosts: set[str] | list[str] | tuple[str, ...],
    limits: ImageLimits | None = None,
    timeout: float = 10.0,
    max_redirects: int = 3,
) -> PreparedImage:
    """Download one trusted image URL and apply the same local validation."""

    limits = limits or ImageLimits()
    current = validate_image_url(
        url,
        allowed_hosts=allowed_hosts,
        allow_local_http=limits.allow_local_http,
    )
    destination_dir = Path(temp_dir)
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / f"{uuid.uuid4().hex}.download"
    client_timeout = aiohttp.ClientTimeout(total=max(3.0, float(timeout)))
    try:
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            for _ in range(max(0, int(max_redirects)) + 1):
                async with session.get(
                    current,
                    allow_redirects=False,
                    headers={
                        "Accept": "image/*",
                        "User-Agent": "astrbot-plugin-moxtu-gallery/1.0",
                    },
                ) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        if not location:
                            raise ImagePipelineError("图床图片跳转地址为空。", code="redirect")
                        current = validate_image_url(
                            urljoin(current, location),
                            allowed_hosts=allowed_hosts,
                            allow_local_http=limits.allow_local_http,
                        )
                        continue
                    if response.status < 200 or response.status >= 300:
                        raise ImagePipelineError(
                            "图床图片暂时无法读取。",
                            code=f"http_{response.status}",
                        )
                    declared = int(response.headers.get("Content-Length") or 0)
                    if declared > limits.max_image_bytes:
                        raise ImagePipelineError("图片超过大小限制。", code="too_large")
                    digest = hashlib.sha256()
                    size = 0
                    with destination.open("wb") as output:
                        async for chunk in response.content.iter_chunked(128 * 1024):
                            size += len(chunk)
                            if size > limits.max_image_bytes:
                                raise ImagePipelineError("图片超过大小限制。", code="too_large")
                            digest.update(chunk)
                            output.write(chunk)
                    if size <= 0:
                        raise ImagePipelineError("图片文件为空。", code="empty")
                    image_format, width, height, frames = _validate_image(destination, limits)
                    suffix = {
                        "JPEG": ".jpg",
                        "PNG": ".png",
                        "WEBP": ".webp",
                        "GIF": ".gif",
                    }.get(image_format, ".image")
                    final_path = destination.with_suffix(suffix)
                    destination.replace(final_path)
                    return PreparedImage(
                        path=final_path,
                        sha256=digest.hexdigest(),
                        size_bytes=size,
                        mime_type=_mime_for_format(image_format),
                        format=image_format,
                        width=width,
                        height=height,
                        frames=frames,
                        source_name=Path(urlparse(current).path).name[:180],
                    )
            raise ImagePipelineError("图床图片跳转次数过多。", code="redirect")
    except ImagePipelineError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError) as error:
        raise ImagePipelineError("图床图片下载失败。", code="download_failed") from error
    finally:
        if destination.exists():
            try:
                destination.unlink()
            except OSError:
                pass
