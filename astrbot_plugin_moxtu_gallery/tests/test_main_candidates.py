from __future__ import annotations

import importlib
import sys
import types
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from astrbot_plugin_moxtu_gallery.lsky_client import LskyPhoto, LskySettings


def _load_main_module():
    command = lambda _name: (lambda handler: handler)
    modules = {
        "astrbot": types.ModuleType("astrbot"),
        "astrbot.api": types.ModuleType("astrbot.api"),
        "astrbot.api.event": types.ModuleType("astrbot.api.event"),
        "astrbot.api.message_components": types.ModuleType(
            "astrbot.api.message_components"
        ),
        "astrbot.api.star": types.ModuleType("astrbot.api.star"),
        "astrbot.core": types.ModuleType("astrbot.core"),
        "astrbot.core.utils": types.ModuleType("astrbot.core.utils"),
        "astrbot.core.utils.astrbot_path": types.ModuleType(
            "astrbot.core.utils.astrbot_path"
        ),
    }
    modules["astrbot.api"].AstrBotConfig = dict
    modules["astrbot.api"].logger = types.SimpleNamespace(
        error=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
        exception=lambda *args, **kwargs: None,
    )
    modules["astrbot.api.event"].AstrMessageEvent = object
    modules["astrbot.api.event"].MessageChain = list
    modules["astrbot.api.event"].filter = types.SimpleNamespace(command=command)
    modules["astrbot.api.message_components"].Image = types.SimpleNamespace(
        fromFileSystem=lambda path: path
    )
    modules["astrbot.api.message_components"].Plain = str
    modules["astrbot.api.star"].Context = object
    modules["astrbot.api.star"].Star = object
    modules["astrbot.core.utils.astrbot_path"].get_astrbot_data_path = (
        lambda: "."
    )
    with patch.dict(sys.modules, modules):
        return importlib.import_module("astrbot_plugin_moxtu_gallery.main")


class V1CandidateTests(IsolatedAsyncioTestCase):
    async def _assert_uses_authenticated_album_listing(self, settings):
        module = _load_main_module()
        plugin = object.__new__(module.MoxtuGalleryPlugin)
        plugin.config = {
            "api_version": settings.api_version,
            "public_source_weight": 100,
            "album_source_weight": 100,
            "candidate_pages": 1,
            "candidate_page_size": 1,
        }
        plugin._settings = settings

        class Client:
            _resolved_version = None

            def __init__(self):
                self.public_calls = 0
                self.public_album_calls = 0
                self.user_calls = 0

            async def list_public_photos(self, **kwargs):
                self.public_calls += 1
                raise AssertionError("v1 must not query the public square")

            async def list_public_album_photos(self, *args, **kwargs):
                self.public_album_calls += 1
                raise AssertionError("v1 must not query the public album endpoint")

            async def list_user_photos(self, **kwargs):
                self.user_calls += 1
                self.user_kwargs = kwargs
                return [LskyPhoto(id=7, source="album_private")], {}

        plugin._client = Client()

        candidates = await plugin._load_candidates()

        self.assertEqual([photo.id for photo in candidates], [7])
        self.assertEqual(plugin._client.public_calls, 0)
        self.assertEqual(plugin._client.public_album_calls, 0)
        self.assertEqual(plugin._client.user_calls, 1)
        self.assertEqual(plugin._client.user_kwargs["album_id"], 12)

    async def test_authenticated_album_public_photo_is_replyable(self):
        module = _load_main_module()
        plugin = object.__new__(module.MoxtuGalleryPlugin)
        plugin._settings = LskySettings(
            api_version="v1",
            album_id=12,
            allow_private_reply=False,
        )

        class Client:
            async def private_url(self, photo_id):
                raise AssertionError("public photos must not request a private URL")

        plugin._client = Client()
        photo = LskyPhoto(
            id=8,
            source="album_private",
            visibility="public",
            public_url="https://example.test/public.jpg",
        )

        self.assertEqual(
            await plugin._resolve_photo_url(photo),
            "https://example.test/public.jpg",
        )

    async def test_v1_uses_authenticated_album_listing(self):
        await self._assert_uses_authenticated_album_listing(
            LskySettings(
                api_version="v1",
                album_id=12,
                upload_is_public=True,
            )
        )

    async def test_auto_with_only_strategy_id_uses_authenticated_album_listing(self):
        await self._assert_uses_authenticated_album_listing(
            LskySettings(
                api_version="auto",
                album_id=12,
                strategy_id=9,
                upload_is_public=True,
            )
        )

    async def test_candidate_pages_are_all_loaded_when_first_page_is_full(self):
        module = _load_main_module()
        plugin = object.__new__(module.MoxtuGalleryPlugin)
        plugin.config = {
            "api_version": "v2",
            "public_source_weight": 100,
            "album_source_weight": 0,
            "candidate_pages": 2,
            "candidate_page_size": 1,
        }
        plugin._settings = LskySettings(
            api_version="v2",
            storage_id=4,
            album_id=0,
        )

        class Client:
            _resolved_version = "v2"

            def __init__(self):
                self.calls = []

            async def list_public_photos(self, **kwargs):
                self.calls.append(kwargs["page"])
                return [LskyPhoto(id=kwargs["page"], public_url=f"https://example.test/{kwargs['page']}.jpg")], {}

        plugin._client = Client()

        candidates = await plugin._load_candidates()

        self.assertEqual(plugin._client.calls, [1, 2])
        self.assertEqual({photo.id for photo in candidates}, {1, 2})

    async def test_auto_falls_back_to_authenticated_v1_album_on_first_v2_probe(self):
        module = _load_main_module()
        plugin = object.__new__(module.MoxtuGalleryPlugin)
        plugin.config = {
            "api_version": "auto",
            "public_source_weight": 100,
            "album_source_weight": 0,
            "candidate_pages": 1,
            "candidate_page_size": 1,
        }
        plugin._settings = LskySettings(
            api_version="auto",
            album_id=12,
            storage_id=4,
            strategy_id=9,
        )

        class Client:
            _resolved_version = None

            def __init__(self):
                self.public_calls = 0
                self.user_calls = 0

            async def list_public_photos(self, **kwargs):
                self.public_calls += 1
                from astrbot_plugin_moxtu_gallery.lsky_client import LskyError

                raise LskyError("not found", status=404)

            async def list_user_photos(self, **kwargs):
                self.user_calls += 1
                return [LskyPhoto(id="legacy-key", source="album_private")], {}

        plugin._client = Client()

        candidates = await plugin._load_candidates()

        self.assertEqual([photo.id for photo in candidates], ["legacy-key"])
        self.assertEqual(plugin._client.public_calls, 1)
        self.assertEqual(plugin._client.user_calls, 1)
        self.assertEqual(plugin._client._resolved_version, "v1")
