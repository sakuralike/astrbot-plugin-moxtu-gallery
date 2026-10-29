from __future__ import annotations

import hashlib
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .caption_service import CaptionResult, CaptionService
from .image_pipeline import (
    ImageLimits,
    ImagePipelineError,
    PreparedImage,
    cleanup_temp_dir,
    create_temp_dir,
    download_image_url,
    find_image_components,
    prepare_image_component,
)
from .lsky_client import LskyClient, LskyError, LskyPhoto, LskySettings
from .metadata_store import MetadataStore
from .security import (
    SecurityError,
    bounded_int,
    ensure_https_api_url,
    validate_image_url,
)


PLUGIN_ID = "astrbot_plugin_moxtu_gallery"


def _as_bool(value: Any, default: bool = False) -> bool:
    return value if isinstance(value, bool) else default


def _as_text_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _message_id(event: AstrMessageEvent) -> str:
    message_obj = getattr(event, "message_obj", None)
    value = getattr(message_obj, "message_id", None)
    if value is None:
        value = getattr(message_obj, "id", None)
    if value is None:
        value = getattr(event, "unified_msg_origin", "")
    return str(value or "").strip()


def _display_name(event: AstrMessageEvent) -> str:
    sender = getattr(getattr(event, "message_obj", None), "sender", None)
    for obj in (sender, getattr(event, "sender", None)):
        if obj is None:
            continue
        for name in ("nickname", "card", "name", "display_name"):
            value = getattr(obj, name, None)
            if isinstance(value, str) and value.strip():
                return value.strip()[:120]
    return str(event.get_sender_id() or "")[:120]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_url_hash(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _photo_url(photo: LskyPhoto) -> str:
    return str(photo.public_url or photo.thumbnail_url or "").strip()


def _host(value: str) -> str:
    try:
        return str(urlparse(value).hostname or "").strip().lower().rstrip(".")
    except ValueError:
        return ""


class MoxtuGalleryPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config
        self.enabled = _as_bool(config.get("enabled", False))
        self.allowed_groups = set(_as_text_list(config.get("allowed_groups", [])))
        self.upload_allowed_users = set(
            _as_text_list(config.get("upload_allowed_users", []))
        )
        self._cooldowns: dict[str, float] = {}
        self._running: set[str] = set()
        self._terminated = False
        self._client: LskyClient | None = None
        self._caption = CaptionService(context, config)

        self._data_dir = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_ID
        self._temp_dir = self._data_dir / "tmp"
        self._store = MetadataStore(self._data_dir / "gallery.sqlite3")
        try:
            base_url = ensure_https_api_url(
                str(config.get("api_base_url", "https://moxtu.sbs") or ""),
                allow_local_http=_as_bool(config.get("allow_local_http", False)),
            )
            api_version = str(config.get("api_version", "auto") or "auto").lower()
            if api_version not in {"auto", "v1", "v2"}:
                raise ValueError("图床 API 版本必须是 auto、v1 或 v2")
            self._settings = LskySettings(
                base_url=base_url,
                api_version=api_version,
                api_prefix=str(config.get("api_prefix", "/api/v2") or "/api/v2"),
                token=str(config.get("api_token", "") or "").strip(),
                album_id=bounded_int(config.get("album_id", 0), 0, 0, 2_147_483_647),
                storage_id=bounded_int(
                    config.get("storage_id", 0), 0, 0, 2_147_483_647
                ),
                strategy_id=bounded_int(
                    config.get("strategy_id", 0), 0, 0, 2_147_483_647
                ),
                upload_is_public=_as_bool(config.get("upload_is_public", True), True),
                request_timeout=float(
                    bounded_int(config.get("request_timeout", 15), 15, 3, 30)
                ),
                request_retries=bounded_int(
                    config.get("api_retry_attempts", 2), 2, 0, 3
                ),
                api_concurrency=bounded_int(
                    config.get("api_concurrency", 4), 4, 1, 16
                ),
                allow_private_reply=_as_bool(
                    config.get("allow_private_reply", False)
                ),
                private_url_ttl=bounded_int(
                    config.get("private_url_ttl", 300), 300, 30, 3600
                ),
            )
            self._client = LskyClient(self._settings)
        except (SecurityError, TypeError, ValueError) as error:
            self._settings = None
            self.enabled = False
            logger.error("Moxtu 图床插件配置无效：%s", error)

    def _scope(self, event: AstrMessageEvent) -> str | None:
        if not self.enabled or self._terminated:
            return None
        try:
            group_id = str(event.get_group_id() or "").strip()
        except Exception:
            group_id = ""
        if not group_id:
            return None
        platform_id = str(
            getattr(event, "get_platform_id", lambda: "")() or ""
        ).strip()
        bot_id = str(getattr(event, "get_self_id", lambda: "")() or "").strip()
        candidates = {
            group_id,
            f"{platform_id}|{group_id}" if platform_id else "",
            f"{platform_id}|{bot_id}|{group_id}"
            if platform_id and bot_id
            else "",
        }
        candidates.discard("")
        if "*" not in self.allowed_groups and not candidates.intersection(
            self.allowed_groups
        ):
            return None
        return f"{platform_id}|{bot_id}|{group_id}"

    def _cooldown(self, key: str, seconds: int) -> bool:
        now = time.monotonic()
        deadline = self._cooldowns.get(key, 0.0)
        if deadline > now:
            return True
        self._cooldowns[key] = now + max(0, seconds)
        if len(self._cooldowns) > 2000:
            self._cooldowns = {
                item: value
                for item, value in self._cooldowns.items()
                if value > now
            }
        return False

    def _limits(self) -> ImageLimits:
        formats = tuple(
            str(item).upper().strip()
            for item in _as_text_list(
                self.config.get(
                    "allowed_formats", ["PNG", "JPEG", "WEBP", "GIF"]
                )
            )
        )
        return ImageLimits(
            max_image_bytes=bounded_int(
                self.config.get("max_image_bytes", 10 * 1024 * 1024),
                10 * 1024 * 1024,
                1024,
                50 * 1024 * 1024,
            ),
            max_pixels=bounded_int(
                self.config.get("max_pixels", 20_000_000),
                20_000_000,
                10_000,
                100_000_000,
            ),
            max_frames=bounded_int(
                self.config.get("max_frames", 10), 10, 1, 100
            ),
            allowed_formats=formats
            or ("PNG", "JPEG", "WEBP", "GIF"),
            allow_local_http=_as_bool(
                self.config.get("allow_local_http", False)
            ),
        )

    def _allowed_image_hosts(self) -> set[str]:
        hosts = {
            _host(str(self.config.get("api_base_url", "https://moxtu.sbs") or ""))
        }
        hosts.update(
            _host(item)
            for item in _as_text_list(self.config.get("allowed_image_hosts", []))
        )
        return {item for item in hosts if item}

    def _validated_photo_url(self, value: str) -> str:
        url = str(value or "").strip()
        if not url:
            return ""
        try:
            return validate_image_url(
                url,
                allowed_hosts=self._allowed_image_hosts(),
                allow_local_http=_as_bool(
                    self.config.get("allow_local_http", False)
                ),
            )
        except SecurityError:
            logger.warning("Moxtu 图床返回了不允许的图片地址")
            return ""

    async def _send_text(self, event: AstrMessageEvent, text: str) -> None:
        await event.send(MessageChain([Plain(str(text))]))

    async def _send_image(
        self,
        event: AstrMessageEvent,
        image: PreparedImage,
        *,
        caption: str = "",
    ) -> None:
        chain: list[Any] = [Image.fromFileSystem(str(image.path))]
        mode = str(self.config.get("caption_reply_mode", "silent") or "silent")
        if caption and mode == "append":
            chain.append(Plain(f"\n图片转述：{caption}"))
        await event.send(MessageChain(chain))
        if caption and mode == "separate":
            await self._send_text(event, f"图片转述：{caption}")

    def _finish_event(self, event: AstrMessageEvent) -> None:
        stopper = getattr(event, "stop_event", None)
        if callable(stopper):
            try:
                stopper()
            except Exception:
                pass

    def _caption_failed_text(self, result: CaptionResult) -> str:
        if result.error_code == "provider_missing":
            return "当前未配置可用的视觉模型，暂时不能处理图片。"
        if result.error_code == "image_unsupported":
            return "当前大模型不支持图片转述，暂时不能处理图片。"
        if result.error_code == "timeout":
            return "图片转述超时，请稍后再试。"
        return "图片转述失败，请稍后再试。"

    def _lsky_error_text(self, error: LskyError) -> str:
        return {
            "configuration": "图床配置不完整，请管理员检查 Token、相册和储存策略。",
            "unauthorized": "图床授权无效，请管理员检查 Token。",
            "forbidden": "图床账号或相册没有权限。",
            "too_large": "图片超过图床上传限制。",
            "validation": "图片或相册配置不正确。",
            "rate_limited": "图床请求过于频繁，请稍后再试。",
            "timeout": "图床请求超时，请稍后再试。",
            "network_error": "图床暂时无法连接，请稍后再试。",
            "unsupported": "当前图床接口不支持此操作。",
            "business_error": "图床拒绝了本次请求。",
        }.get(error.code, "图床服务暂时不可用，请稍后再试。")

    async def _caption_image(
        self,
        image: PreparedImage,
        event: AstrMessageEvent,
    ) -> CaptionResult:
        return await self._caption.describe(
            image.path,
            unified_message_origin=str(
                getattr(event, "unified_msg_origin", "") or ""
            ),
        )

    @filter.command("上传图片")
    async def upload_image(self, event: AstrMessageEvent) -> None:
        scope = self._scope(event)
        if scope is None or self._client is None:
            return
        self._finish_event(event)
        sender_id = str(event.get_sender_id() or "").strip()
        if self.upload_allowed_users and sender_id not in self.upload_allowed_users:
            await self._send_text(event, "你没有使用图片上传功能的权限。")
            return
        message_key = _message_id(event)
        if message_key in self._running:
            await self._send_text(event, "这条图片正在处理中，请稍候。")
            return
        group_id = str(event.get_group_id() or "").strip()
        user_cooldown = bounded_int(
            self.config.get("upload_cooldown", 10), 10, 0, 3600
        )
        group_cooldown = bounded_int(
            self.config.get("group_upload_cooldown", 3), 3, 0, 3600
        )
        if self._cooldown(f"upload:user:{sender_id}", user_cooldown) or self._cooldown(
            f"upload:group:{group_id}", group_cooldown
        ):
            await self._send_text(event, "上传操作过于频繁，请稍后再试。")
            return

        components = find_image_components(event.get_messages())
        if not components:
            await self._send_text(event, "请在“/上传图片”后附上一张图片。")
            return
        if len(components) > 1:
            await self._send_text(event, "一次只能上传一张图片，请分开发送。")
            return

        self._running.add(message_key)
        temp_dir = create_temp_dir(self._temp_dir)
        prepared: PreparedImage | None = None
        try:
            try:
                prepared = await prepare_image_component(
                    components[0], temp_dir, limits=self._limits()
                )
            except ImagePipelineError as error:
                await self._send_text(event, str(error))
                return

            event_id = message_key
            existing = self._store.get_upload_by_event(event_id, prepared.sha256)
            if existing and existing.get("upload_status") == "success":
                await self._send_text(event, "这张图片已经上传过了。")
                return

            caption = await self._caption_image(prepared, event)
            failure_policy = str(
                self.config.get("caption_failure_policy", "reject") or "reject"
            )
            if (
                self.config.get("caption_enabled", True) is True
                and not caption.ok
                and failure_policy != "allow_without_caption"
            ):
                self._store.record_caption(
                    photo_id=None,
                    sha256=prepared.sha256,
                    provider_id=caption.provider_id,
                    model_name=caption.model_name,
                    caption="",
                    status=caption.status,
                    error_code=caption.error_code,
                )
                await self._send_text(event, self._caption_failed_text(caption))
                return
            intro = caption.caption if caption.ok else ""
            try:
                photo = await self._client.upload(
                    prepared.path,
                    album_id=self._settings.album_id if self._settings else 0,
                    is_public=self._settings.upload_is_public
                    if self._settings
                    else True,
                    intro=intro,
                    content_type=prepared.mime_type,
                )
            except LskyError as error:
                logger.warning("Moxtu 图床上传失败：%s", error.code)
                await self._send_text(event, self._lsky_error_text(error))
                return
            if photo.id is None and not photo.key and not photo.public_url:
                await self._send_text(event, "图床返回了无法识别的图片信息，请稍后再试。")
                return
            photo.public_url = self._validated_photo_url(photo.public_url)

            self._store.record_caption(
                photo_id=photo.id,
                sha256=prepared.sha256,
                provider_id=caption.provider_id,
                model_name=caption.model_name,
                caption=caption.caption,
                status=caption.status,
                error_code=caption.error_code,
            )
            self._store.record_upload(
                {
                    "event_id": event_id,
                    "message_id": message_key,
                    "platform_name": str(event.get_platform_name() or ""),
                    "platform_id": str(
                        getattr(event, "get_platform_id", lambda: "")() or ""
                    ),
                    "bot_self_id": str(event.get_self_id() or ""),
                    "group_id": group_id,
                    "sender_id": sender_id,
                    "sender_name": _display_name(event),
                    "lsky_version": str(
                        getattr(self._client, "_resolved_version", None)
                        or (self._settings.api_version if self._settings else "")
                    ),
                    "album_id": str(
                        self._settings.album_id if self._settings else ""
                    ),
                    "photo_id": str(photo.id) if photo.id is not None else None,
                    "photo_key": photo.key,
                    "filename": prepared.source_name or photo.filename,
                    "mime_type": prepared.mime_type,
                    "width": prepared.width,
                    "height": prepared.height,
                    "size_bytes": prepared.size_bytes,
                    "sha256": prepared.sha256,
                    "public_url": photo.public_url,
                    "visibility": "public"
                    if self._settings and self._settings.upload_is_public
                    else "private",
                    "caption": caption.caption,
                    "caption_status": caption.status,
                    "upload_status": "success",
                    "uploaded_at": _now_iso(),
                }
            )
            text = "图片已上传到专属相册。"
            if caption.caption and str(
                self.config.get("caption_reply_mode", "silent")
            ) in {"append", "separate"}:
                text += f"\n图片转述：{caption.caption}"
            await self._send_text(event, text)
        except Exception:
            logger.exception("Moxtu 图片上传流程异常")
            await self._send_text(event, "图片处理失败，请稍后再试。")
        finally:
            self._running.discard(message_key)
            cleanup_temp_dir(temp_dir)

    async def _load_candidates(self) -> list[LskyPhoto]:
        if self._client is None or self._settings is None:
            return []
        pages = bounded_int(
            self.config.get("candidate_pages", 3), 3, 1, 5
        )
        per_page = bounded_int(
            self.config.get("candidate_page_size", 40), 40, 1, 100
        )
        public_weight = bounded_int(
            self.config.get("public_source_weight", 50), 50, 0, 100
        )
        album_weight = bounded_int(
            self.config.get("album_source_weight", 50), 50, 0, 100
        )
        resolved_or_configured_version = str(
            getattr(self._client, "_resolved_version", None)
            or self._settings.api_version
        ).strip().lower()
        is_v1 = resolved_or_configured_version == "v1" or (
            resolved_or_configured_version == "auto"
            and not self._settings.storage_id
            and bool(self._settings.strategy_id)
        )
        if is_v1:
            public_weight = 0
        sources: list[str] = []
        if public_weight:
            sources.extend(["public"] * public_weight)
        if album_weight and self._settings.album_id:
            sources.extend(["album"] * album_weight)
        if not sources:
            return []
        source = random.choice(sources)
        result: list[LskyPhoto] = []
        for page in range(1, pages + 1):
            try:
                if source == "public":
                    photos, _ = await self._client.list_public_photos(
                        page=page, per_page=per_page
                    )
                elif self._settings.upload_is_public and not is_v1:
                    photos, _ = await self._client.list_public_album_photos(
                        self._settings.album_id, page=page, per_page=per_page
                    )
                else:
                    photos, _ = await self._client.list_user_photos(
                        album_id=self._settings.album_id,
                        page=page,
                        per_page=per_page,
                    )
            except LskyError as error:
                if (
                    str(self._settings.api_version or "").strip().lower() == "auto"
                    and not is_v1
                    and error.status in {404, 405}
                    and self._settings.album_id
                ):
                    self._client._resolved_version = "v1"
                    is_v1 = True
                    source = "album"
                    try:
                        photos, _ = await self._client.list_user_photos(
                            album_id=self._settings.album_id,
                            page=page,
                            per_page=per_page,
                        )
                    except LskyError:
                        if result:
                            break
                        raise
                elif result:
                    break
                else:
                    raise
            result.extend(photos)
            if not photos:
                break
        random.shuffle(result)
        return result

    async def _resolve_photo_url(self, photo: LskyPhoto) -> str:
        if self._settings is None or self._client is None:
            return ""
        visibility = photo.visibility.strip().lower()
        is_private = visibility in {"private", "0", "false"}
        if not visibility and photo.source == "album_private":
            is_private = True
        if is_private:
            if not self._settings.allow_private_reply or photo.id is None:
                return ""
            try:
                return await self._client.private_url(photo.id)
            except LskyError:
                return ""
        return _photo_url(photo)

    @filter.command("来点图康康")
    async def random_image(self, event: AstrMessageEvent) -> None:
        scope = self._scope(event)
        if scope is None or self._client is None:
            return
        self._finish_event(event)
        sender_id = str(event.get_sender_id() or "").strip()
        group_id = str(event.get_group_id() or "").strip()
        message_key = _message_id(event)
        if message_key in self._running:
            return
        user_cooldown = bounded_int(
            self.config.get("random_cooldown", 15), 15, 0, 3600
        )
        group_cooldown = bounded_int(
            self.config.get("group_random_cooldown", 5), 5, 0, 3600
        )
        if self._cooldown(f"random:user:{sender_id}", user_cooldown) or self._cooldown(
            f"random:group:{group_id}", group_cooldown
        ):
            await self._send_text(event, "来图操作过于频繁，请稍后再试。")
            return
        self._running.add(message_key)
        temp_dir = create_temp_dir(self._temp_dir)
        try:
            try:
                candidates = await self._load_candidates()
            except LskyError as error:
                logger.warning("Moxtu 随机图片查询失败：%s", error.code)
                await self._send_text(event, self._lsky_error_text(error))
                return
            duplicate_window = bounded_int(
                self.config.get("duplicate_reply_window", 1800),
                1800,
                0,
                7 * 24 * 3600,
            )
            recent = self._store.recent_reply_hashes(
                group_id,
                limit=50,
                window_seconds=duplicate_window,
            )
            max_attempts = bounded_int(
                self.config.get("max_candidate_attempts", 5), 5, 1, 20
            )
            tried = 0
            for photo in candidates:
                if tried >= max_attempts:
                    break
                url = await self._resolve_photo_url(photo)
                if not url:
                    continue
                photo_identity = str(photo.id or photo.key or url)
                url_hash = _safe_url_hash(photo_identity)
                if url_hash in recent:
                    continue
                tried += 1
                try:
                    image = await download_image_url(
                        url,
                        temp_dir,
                        allowed_hosts=self._allowed_image_hosts(),
                        limits=self._limits(),
                        timeout=float(
                            bounded_int(
                                self.config.get("download_timeout", 10),
                                10,
                                3,
                                30,
                            )
                        ),
                        max_redirects=bounded_int(
                            self.config.get("max_redirects", 3), 3, 0, 5
                        ),
                    )
                except ImagePipelineError:
                    continue
                caption = await self._caption_image(image, event)
                if (
                    self.config.get("caption_enabled", True) is True
                    and not caption.ok
                    and str(
                        self.config.get("caption_failure_policy", "reject")
                        or "reject"
                    )
                    != "allow_without_caption"
                ):
                    continue
                try:
                    await self._send_image(
                        event,
                        image,
                        caption=caption.caption if caption.ok else "",
                    )
                except Exception:
                    logger.warning("Moxtu 随机图片发送失败")
                    continue
                self._store.record_caption(
                    photo_id=photo.id,
                    sha256=image.sha256,
                    provider_id=caption.provider_id,
                    model_name=caption.model_name,
                    caption=caption.caption,
                    status=caption.status,
                    error_code=caption.error_code,
                )
                self._store.record_reply(
                    group_id=group_id,
                    photo_id=photo.id,
                    source_type=photo.source or "unknown",
                    source_url_hash=url_hash,
                    caption_status=caption.status,
                    replied_at=_now_iso(),
                )
                return
            await self._send_text(event, "暂时没有找到可发送的图片，请稍后再试。")
        except Exception:
            logger.exception("Moxtu 随机图片流程异常")
            await self._send_text(event, "图片获取失败，请稍后再试。")
        finally:
            self._running.discard(message_key)
            cleanup_temp_dir(temp_dir)

    async def terminate(self) -> None:
        self._terminated = True
        if self._client is not None:
            await self._client.close()
            self._client = None
        cleanup_temp_dir(self._temp_dir)
