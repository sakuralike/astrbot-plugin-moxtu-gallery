from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image as PillowImage

from astrbot_plugin_moxtu_gallery.image_pipeline import (
    ImagePipelineError,
    prepare_image_component,
)


class ImageComponent:
    def __init__(self, file: str, local_path: Path) -> None:
        self.file = file
        self._local_path = local_path

    async def convert_to_file_path(self) -> str:
        return str(self._local_path)


class ImagePipelineTest(unittest.IsolatedAsyncioTestCase):
    async def test_network_file_source_is_validated_before_download(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "image.png"
            PillowImage.new("RGB", (2, 2), "white").save(source)
            component = ImageComponent("http://127.0.0.1/image.png", source)
            with self.assertRaises(ImagePipelineError) as context:
                await prepare_image_component(component, Path(directory) / "tmp")
            self.assertEqual(context.exception.code, "unsafe_source")

    async def test_local_file_uri_is_not_treated_as_remote_url(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "image.png"
            PillowImage.new("RGB", (2, 2), "white").save(source)
            component = ImageComponent(source.as_uri(), source)
            prepared = await prepare_image_component(component, Path(directory) / "tmp")
            try:
                self.assertEqual(prepared.format, "PNG")
            finally:
                prepared.path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
