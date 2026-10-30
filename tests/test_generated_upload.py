from __future__ import annotations

import asyncio
import hashlib
import io
import json
import tempfile
import types
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from PIL import Image as PillowImage

from astrbot_plugin_moxtu_gallery.caption_service import CaptionResult
from astrbot_plugin_moxtu_gallery.lsky_client import LskyError, LskyPhoto, LskySettings
from astrbot_plugin_moxtu_gallery.metadata_store import MetadataStore
from test_main_candidates import _load_main_module


class Image:
    def __init__(self, path):
        self.path = str(path)

    @classmethod
    def fromFileSystem(cls, path):
        return cls(path)

    async def convert_to_file_path(self):
        return self.path


class Event:
    def __init__(self, message_id="message-1", images=None):
        self.message_obj = types.SimpleNamespace(
            message_id=message_id,
            sender=types.SimpleNamespace(nickname="member-name"),
        )
        self.message_str = "/生图 private prompt"
        self.images = list(images or [])
        self.unified_msg_origin = "platform-1:GroupMessage:group-1"
        self.stop_event = AsyncMock()
        self.send = AsyncMock()

    def get_group_id(self):
        return "group-1"

    def get_sender_id(self):
        return "member-1"

    def get_platform_id(self):
        return "platform-1"

    def get_platform_name(self):
        return "qq_official"

    def get_self_id(self):
        return "bot-1"

    def get_messages(self):
        return self.images


class GeneratedUploadTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.module = _load_main_module()
        self.image_patch = patch.object(self.module, "Image", Image)
        self.image_patch.start()
        self.addCleanup(self.image_patch.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.plugin = object.__new__(self.module.MoxtuGalleryPlugin)
        self.plugin.enabled = True
        self.plugin._terminated = False
        self.plugin.allowed_groups = {"group-1"}
        self.plugin.upload_allowed_users = set()
        self.plugin.config = {
            "caption_enabled": True,
            "caption_failure_policy": "reject",
            "api_base_url": "https://example.test",
            "upload_cooldown": 10,
            "group_upload_cooldown": 3,
        }
        self.plugin._settings = LskySettings(
            base_url="https://example.test",
            api_version="v2",
            token="test-token",
            album_id=12,
            storage_id=4,
            upload_is_public=False,
        )
        self.plugin._cooldowns = {}
        self.plugin._running = set()
        self.plugin._temp_dir = self.root / "tmp"
        self.plugin._store = MetadataStore(self.root / "metadata.sqlite3")
        self.plugin._caption = types.SimpleNamespace(
            describe=AsyncMock(
                return_value=CaptionResult(
                    status="ok", caption="一只猫", provider_id="vision", model_name="vision"
                )
            )
        )
        self.uploaded = []

        async def upload(path, **kwargs):
            self.uploaded.append((Path(path).read_bytes(), kwargs))
            return LskyPhoto(
                id=7, key="photo-key", public_url="https://example.test/image.png"
            )

        self.plugin._client = types.SimpleNamespace(
            upload=AsyncMock(side_effect=upload), _resolved_version="v2"
        )
        self.plugin._send_text = AsyncMock()
        self.event = Event()
        data = io.BytesIO()
        PillowImage.new("RGB", (3, 2), "red").save(data, format="PNG")
        self.image_bytes = data.getvalue()

    def assert_clean(self):
        self.assertFalse(self.plugin._running)
        self.assertFalse(
            self.plugin._temp_dir.exists() and list(self.plugin._temp_dir.iterdir())
        )
        self.event.send.assert_not_called()
        self.event.stop_event.assert_not_called()
        self.plugin._send_text.assert_not_called()

    async def test_success_preserves_bytes_attribution_and_visibility(self):
        result = await self.plugin.upload_generated_image(self.event, self.image_bytes)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["photo_id"], "7")
        self.assertEqual(self.uploaded[0][0], self.image_bytes)
        self.assertEqual(
            self.uploaded[0][1],
            {"album_id": 12, "is_public": False, "intro": "一只猫", "content_type": "image/png"},
        )
        record = self.plugin._store.list_uploads()[0]
        for key, value in {
            "message_id": "message-1",
            "platform_id": "platform-1",
            "bot_self_id": "bot-1",
            "group_id": "group-1",
            "sender_id": "member-1",
            "sender_name": "member-name",
            "visibility": "private",
            "album_id": "12",
            "sha256": hashlib.sha256(self.image_bytes).hexdigest(),
        }.items():
            self.assertEqual(record[key], value)
        self.assertEqual(
            json.loads(record["event_id"].removeprefix("astrbot_img:")),
            ["platform-1", "bot-1", "group-1", "message-1"],
        )
        self.assertEqual(self.event.message_str, "/生图 private prompt")
        self.assertNotIn("private prompt", json.dumps(self.uploaded[0][1]))
        self.assert_clean()

    async def test_successful_duplicate_precedes_cooldown(self):
        await self.plugin.upload_generated_image(self.event, self.image_bytes)
        result = await self.plugin.upload_generated_image(self.event, self.image_bytes)
        self.assertEqual(result["status"], "existing")
        self.plugin._client.upload.assert_awaited_once()
        self.plugin._caption.describe.assert_awaited_once()
        self.assert_clean()

    async def test_same_upload_in_progress_is_not_sent_twice(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def upload(path, **kwargs):
            entered.set()
            await release.wait()
            return LskyPhoto(id=8)

        self.plugin._client.upload.side_effect = upload
        task = asyncio.create_task(
            self.plugin.upload_generated_image(self.event, self.image_bytes)
        )
        try:
            await entered.wait()
            result = await self.plugin.upload_generated_image(self.event, self.image_bytes)
            self.assertEqual(result["error_code"], "in_progress")
        finally:
            release.set()
            self.assertEqual((await task)["status"], "success")
        self.plugin._client.upload.assert_awaited_once()
        self.assert_clean()

    async def test_disabled_group_and_user_denied(self):
        self.plugin.enabled = False
        result = await self.plugin.upload_generated_image(self.event, self.image_bytes)
        self.assertEqual(result["error_code"], "disabled")
        self.plugin.enabled = True
        self.plugin.allowed_groups = set()
        result = await self.plugin.upload_generated_image(self.event, self.image_bytes)
        self.assertEqual(result["error_code"], "group_denied")
        self.plugin.allowed_groups = {"group-1"}
        self.plugin.upload_allowed_users = {"another-member"}
        result = await self.plugin.upload_generated_image(self.event, self.image_bytes)
        self.assertEqual(result["error_code"], "user_denied")
        self.plugin._client.upload.assert_not_called()
        self.assert_clean()

    async def test_incomplete_target_is_rejected_without_upload(self):
        for field, value in (("token", ""), ("album_id", 0), ("storage_id", 0)):
            original = getattr(self.plugin._settings, field)
            with self.subTest(field=field):
                setattr(self.plugin._settings, field, value)
                result = await self.plugin.upload_generated_image(self.event, self.image_bytes)
                self.assertEqual(result["error_code"], "configuration")
                setattr(self.plugin._settings, field, original)
        self.plugin._client.upload.assert_not_called()
        self.assert_clean()

    async def test_too_large_and_invalid_image_are_rejected(self):
        self.plugin.config["max_image_bytes"] = 1024
        result = await self.plugin.upload_generated_image(self.event, b"x" * 1025)
        self.assertEqual(result["error_code"], "too_large")
        result = await self.plugin.upload_generated_image(self.event, b"not an image")
        self.assertEqual(result["error_code"], "invalid_image")
        self.plugin._client.upload.assert_not_called()
        self.assert_clean()

    async def test_cooldown_is_shared_with_manual_uploads(self):
        self.plugin._cooldown("upload:user:member-1", 60)
        result = await self.plugin.upload_generated_image(self.event, self.image_bytes)
        self.assertEqual(result["error_code"], "cooldown")
        self.plugin._client.upload.assert_not_called()
        self.assert_clean()

    async def test_caption_failure_rejects_upload(self):
        self.plugin._caption.describe.return_value = CaptionResult(
            status="error", error_code="provider_missing"
        )
        result = await self.plugin.upload_generated_image(self.event, self.image_bytes)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "provider_missing")
        self.plugin._client.upload.assert_not_called()
        self.assert_clean()

    async def test_timeout_and_network_failure_are_unknown_without_retry(self):
        for code in ("timeout", "network_error"):
            with self.subTest(code=code):
                self.plugin._cooldowns.clear()
                self.plugin._client.upload.reset_mock()
                self.plugin._client.upload.side_effect = LskyError(
                    "secret supplier details", code=code
                )
                result = await self.plugin.upload_generated_image(self.event, self.image_bytes)
                self.assertEqual(result["status"], "unknown")
                self.assertEqual(result["error_code"], code)
                self.assertNotIn("secret", result["message"])
                self.plugin._client.upload.assert_awaited_once()
                self.assert_clean()

    async def test_known_remote_rejection_is_failed(self):
        self.plugin._client.upload.side_effect = LskyError(
            "secret", code="unauthorized", status=401
        )
        result = await self.plugin.upload_generated_image(self.event, self.image_bytes)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "unauthorized")
        self.assertNotIn("secret", result["message"])
        self.assert_clean()

    async def test_gateway_error_is_unknown_without_retry(self):
        self.plugin._client.upload.side_effect = LskyError(
            "secret gateway response", code="upstream_error", status=502
        )
        result = await self.plugin.upload_generated_image(self.event, self.image_bytes)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["error_code"], "upstream_error")
        self.assertNotIn("secret", result["message"])
        self.plugin._client.upload.assert_awaited_once()
        self.assert_clean()

    async def test_remote_success_with_local_record_failure_is_unknown(self):
        with patch.object(
            self.plugin._store, "record_upload", side_effect=OSError("test-record-error")
        ):
            result = await self.plugin.upload_generated_image(self.event, self.image_bytes)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["error_code"], "record_failed")
        self.assertEqual(result["photo_id"], "7")
        self.plugin._client.upload.assert_awaited_once()
        self.assert_clean()

    async def test_cancellation_propagates_and_cleans_up(self):
        entered = asyncio.Event()

        async def upload(path, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        self.plugin._client.upload.side_effect = upload
        task = asyncio.create_task(
            self.plugin.upload_generated_image(self.event, self.image_bytes)
        )
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assert_clean()

    async def test_manual_upload_still_records_and_replies(self):
        source = self.root / "manual.png"
        source.write_bytes(self.image_bytes)
        event = Event(images=[Image(source)])
        event.stop_event = lambda: None
        await self.plugin.upload_image(event)
        self.plugin._send_text.assert_awaited_once_with(
            event, "图片已上传到专属相册。"
        )
        record = self.plugin._store.list_uploads()[0]
        self.assertEqual(record["event_id"], "message-1")
        self.assertEqual(self.uploaded[0][0], self.image_bytes)
        self.assertTrue(source.is_file())
        self.assertFalse(self.plugin._running)
        self.assertFalse(list(self.plugin._temp_dir.iterdir()))
