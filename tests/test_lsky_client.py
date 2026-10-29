from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from astrbot_plugin_moxtu_gallery.lsky_client import (
    LskyClient,
    LskyError,
    LskySettings,
    _photo,
)


class LskyClientTest(IsolatedAsyncioTestCase):
    async def test_request_rejects_redirect_without_following_it(self):
        client = LskyClient(LskySettings(api_version="v2"))
        calls = []

        class Response:
            status = 302
            headers = {"Location": "https://example.invalid/next"}

            async def text(self):
                return ""

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return False

        class Session:
            def request(self, method, url, **kwargs):
                calls.append((method, url, kwargs))
                return Response()

        async def get_session():
            return Session()

        client._get_session = get_session
        with self.assertRaises(LskyError) as context:
            await client._request("GET", "/explore/photos", version="v2")

        self.assertEqual(context.exception.code, "redirect")
        self.assertEqual(calls[0][2]["allow_redirects"], False)

    async def test_public_photo_list_unwraps_paginated_data(self):
        client = LskyClient(LskySettings(api_version="v2", token="secret"))

        async def request(method, path, **kwargs):
            self.assertEqual((method, path), ("GET", "/explore/photos"))
            return {
                "status": True,
                "data": {
                    "current_page": 1,
                    "data": [
                        {
                            "id": 7,
                            "key": "abc",
                            "public_url": "https://moxtu.sbs/uploads/a.jpg",
                            "width": 640,
                            "height": 480,
                        }
                    ],
                },
            }

        client._request = request
        photos, meta = await client.list_public_photos()
        self.assertEqual(meta["current_page"], 1)
        self.assertEqual(len(photos), 1)
        self.assertEqual(photos[0].id, 7)
        self.assertEqual(photos[0].source, "public")

    async def test_upload_auto_fallback_rebuilds_version_specific_form(self):
        client = LskyClient(
            LskySettings(api_version="auto", storage_id=3, strategy_id=9)
        )
        calls = []

        async def request(method, path, *, version=None, data=None, **kwargs):
            calls.append((method, path, version, data))
            if version == "v2":
                raise LskyError("not found", code="upstream_error", status=404)
            return {
                "status": True,
                "data": {
                    "id": 11,
                    "key": "legacy-key",
                    "public_url": "https://moxtu.sbs/a.jpg",
                },
            }

        client._request = request
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "a.jpg"
            image.write_bytes(b"not-an-image")
            photo = await client.upload(image)

        self.assertEqual(photo.id, 11)
        self.assertEqual(client._resolved_version, "v1")
        self.assertEqual([item[2] for item in calls], ["v2", "v1"])

    async def test_upload_auto_uses_v1_when_only_strategy_id_is_configured(self):
        client = LskyClient(
            LskySettings(api_version="auto", strategy_id=9)
        )
        calls = []

        async def request(method, path, *, version=None, data=None, **kwargs):
            calls.append(version)
            return {
                "status": True,
                "data": {
                    "id": 12,
                    "key": "legacy-only",
                    "public_url": "https://moxtu.sbs/a.jpg",
                },
            }

        client._request = request
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "a.jpg"
            image.write_bytes(b"not-an-image")
            photo = await client.upload(image)

        self.assertEqual(photo.id, 12)
        self.assertEqual(calls, ["v1"])

    async def test_business_error_is_raised(self):
        client = LskyClient(LskySettings(api_version="v2"))

        async def request(*args, **kwargs):
            raise LskyError("拒绝", code="business_error", status=200)

        client._request = request
        with self.assertRaises(LskyError) as context:
            await client.list_public_photos()
        self.assertEqual(context.exception.code, "business_error")

    async def test_read_request_retries_transient_upstream_failure(self):
        client = LskyClient(LskySettings(api_version="v2", request_retries=1))
        calls = 0

        async def request_once(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise LskyError("暂时不可用", code="upstream_error", status=503)
            return {"status": True, "data": []}

        client._request_once = request_once
        with patch(
            "astrbot_plugin_moxtu_gallery.lsky_client.asyncio.sleep",
            new=AsyncMock(),
        ):
            payload = await client._request("GET", "/explore/photos", version="v2")

        self.assertEqual(calls, 2)
        self.assertEqual(payload["status"], True)

    async def test_private_url_requires_url_in_response(self):
        client = LskyClient(LskySettings(api_version="v2"))

        async def request(*args, **kwargs):
            return {"status": True, "data": {"photo": 1}}

        client._request = request
        with self.assertRaises(LskyError):
            await client.private_url(1)

    async def test_private_url_sends_configured_ttl_to_v2(self):
        client = LskyClient(LskySettings(api_version="v2", private_url_ttl=301))
        calls = []

        async def request(method, path, **kwargs):
            calls.append((method, path, kwargs))
            return {
                "status": "success",
                "data": {"private_url": "https://moxtu.sbs/private/1"},
            }

        client._request = request
        self.assertEqual(
            await client.private_url(1),
            "https://moxtu.sbs/private/1",
        )
        self.assertEqual(calls[0][0:2], ("POST", "/user/photos/1/private-url"))
        self.assertEqual(calls[0][2]["version"], "v2")
        self.assertEqual(calls[0][2]["data"], '{"minutes": 6}')
        self.assertEqual(
            calls[0][2]["headers"]["Content-Type"],
            "application/json",
        )

    def test_photo_visibility_accepts_v1_permission_field(self):
        public = _photo({"id": 1, "permission": "1", "url": "https://example.test/a.jpg"})
        private = _photo({"id": 2, "permission": "0", "url": "https://example.test/b.jpg"})

        self.assertEqual(public.visibility, "public")
        self.assertEqual(private.visibility, "private")

    def test_v1_key_is_used_as_photo_id_and_thumbnail_is_parsed(self):
        photo = _photo(
            {
                "key": "legacy-key",
                "links": {
                    "url": "https://example.test/a.jpg",
                    "thumbnail_url": "https://example.test/thumb.jpg",
                },
            }
        )

        self.assertEqual(photo.id, "legacy-key")
        self.assertEqual(photo.public_url, "https://example.test/a.jpg")
        self.assertEqual(photo.thumbnail_url, "https://example.test/thumb.jpg")
