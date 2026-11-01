import asyncio
import base64
import io
import unittest

from PIL import Image

from astrbot_plugin_message_card.card_assets import (
    AssetConfig,
    AssetError,
    AssetResolver,
    _normalize_image,
)


def tiny_png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGBA", (32, 18), (255, 0, 0, 170)).save(buffer, format="PNG")
    return buffer.getvalue()


class AssetTests(unittest.TestCase):
    def test_data_uri_image(self):
        async def run():
            resolver = AssetResolver(AssetConfig())
            try:
                uri = await resolver.fetch_content_image(
                    "data:image/png;base64,"
                    + base64.b64encode(tiny_png()).decode()
                )
                self.assertTrue(uri and uri.startswith("data:image/"))
                return uri
            finally:
                await resolver.close()

        asyncio.run(run())

    def test_flatten_removes_alpha(self):
        uri = _normalize_image(tiny_png(), AssetConfig(), max_side=64, flatten=True)
        self.assertTrue(uri.startswith("data:image/jpeg;base64,"))

    def test_private_url_is_rejected(self):
        async def run():
            resolver = AssetResolver(AssetConfig())
            try:
                with self.assertRaises(AssetError):
                    await resolver._assert_public_url("http://127.0.0.1:8888/image")
            finally:
                await resolver.close()

        asyncio.run(run())

    def test_non_image_rejected(self):
        with self.assertRaises(AssetError):
            _normalize_image(b"not an image", AssetConfig())


if __name__ == "__main__":
    unittest.main()
