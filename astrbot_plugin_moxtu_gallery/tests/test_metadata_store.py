from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from astrbot_plugin_moxtu_gallery.metadata_store import MetadataStore


class MetadataStoreTest(unittest.TestCase):
    def test_upload_is_idempotent_for_event_and_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MetadataStore(Path(directory) / "data.sqlite3")
            record = {
                "event_id": "event-1",
                "message_id": "message-1",
                "platform_name": "qq_official",
                "platform_id": "default",
                "bot_self_id": "bot",
                "group_id": "group",
                "sender_id": "sender",
                "sender_name": "Alice",
                "lsky_version": "v2",
                "album_id": "5",
                "photo_id": "10",
                "photo_key": "key",
                "filename": "a.jpg",
                "mime_type": "image/jpeg",
                "width": 100,
                "height": 80,
                "size_bytes": 1000,
                "sha256": "hash",
                "public_url": "https://moxtu.sbs/a.jpg",
                "visibility": "public",
                "caption": "a picture",
                "caption_status": "ok",
                "upload_status": "success",
                "uploaded_at": "2026-10-29T10:00:00+08:00",
            }
            first = store.record_upload(record)
            record["sender_name"] = "Alice updated"
            second = store.record_upload(record)
            self.assertEqual(first, second)
            rows = store.list_uploads()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["sender_name"], "Alice updated")

    def test_reply_hashes_are_limited_and_caption_is_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MetadataStore(Path(directory) / "data.sqlite3")
            for index in range(3):
                store.record_reply(
                    group_id="group",
                    photo_id=index,
                    source_type="public",
                    source_url_hash=f"hash-{index}",
                    caption_status="ok",
                    replied_at=f"2026-10-29T10:0{index}:00+08:00",
                )
            self.assertEqual(
                store.recent_reply_hashes("group", limit=2),
                {"hash-1", "hash-2"},
            )
            caption_id = store.record_caption(
                photo_id=3,
                sha256="hash-3",
                provider_id="provider",
                model_name="vision",
                caption="description",
                status="ok",
            )
            self.assertGreater(caption_id, 0)

    def test_reply_hashes_can_be_filtered_by_time_window(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MetadataStore(Path(directory) / "data.sqlite3")
            store.record_reply(
                group_id="group",
                photo_id=1,
                source_type="public",
                source_url_hash="recent",
                caption_status="ok",
                replied_at="2026-10-29T10:20:00+00:00",
            )
            store.record_reply(
                group_id="group",
                photo_id=2,
                source_type="public",
                source_url_hash="old",
                caption_status="ok",
                replied_at="2026-10-29T09:00:00+00:00",
            )
            store.record_reply(
                group_id="group",
                photo_id=3,
                source_type="public",
                source_url_hash="offset",
                caption_status="ok",
                replied_at="2026-10-29T18:20:00+08:00",
            )

            hashes = store.recent_reply_hashes(
                "group",
                limit=2,
                window_seconds=1800,
                now=datetime(2026, 10, 29, 10, 30, tzinfo=timezone.utc),
            )

            self.assertEqual(hashes, {"recent", "offset"})
