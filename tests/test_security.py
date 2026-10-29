from __future__ import annotations

import unittest

from astrbot_plugin_moxtu_gallery.security import (
    SecurityError,
    ensure_https_api_url,
    redact_secret,
    validate_image_url,
)


class SecurityTest(unittest.TestCase):
    def test_redact_secret(self):
        text = redact_secret("Authorization: Bearer abc123 token=xyz")
        self.assertNotIn("abc123", text)
        self.assertNotIn("xyz", text)
        self.assertIn("<redacted>", text)

    def test_validate_image_url(self):
        self.assertEqual(
            validate_image_url(
                "https://moxtu.sbs/uploads/a.jpg",
                allowed_hosts={"moxtu.sbs"},
            ),
            "https://moxtu.sbs/uploads/a.jpg",
        )
        self.assertEqual(
            validate_image_url(
                "http://127.0.0.1:8000/a.jpg",
                allowed_hosts={"127.0.0.1"},
                allow_local_http=True,
            ),
            "http://127.0.0.1:8000/a.jpg",
        )
        with self.assertRaises(SecurityError):
            validate_image_url(
                "https://example.com/a.jpg",
                allowed_hosts={"moxtu.sbs"},
            )
        with self.assertRaises(SecurityError):
            validate_image_url(
                "http://example.com/a.jpg",
                allowed_hosts={"example.com"},
                allow_local_http=True,
            )

    def test_local_http_only_allowed_for_loopback_api(self):
        self.assertEqual(
            ensure_https_api_url("http://127.0.0.1:8000", allow_local_http=True),
            "http://127.0.0.1:8000",
        )
        with self.assertRaises(SecurityError):
            ensure_https_api_url("http://moxtu.sbs", allow_local_http=True)
