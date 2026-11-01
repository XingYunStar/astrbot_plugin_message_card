"""Optional end-to-end rendering smoke test.

Run with:
    RUN_RENDER_TESTS=1 python -m unittest tests.test_smoke_render

Requires playwright and a Chromium binary.  The normal unit tests do not need a
browser, which keeps CI fast and makes the plugin importable in bare Python.
"""

import asyncio
import os
import unittest
from pathlib import Path

from astrbot_plugin_message_card.card_content import (
    ContentBuilder,
    ContentOptions,
    pygments_css,
)
from astrbot_plugin_message_card.card_renderer import CardRenderer, RenderOptions
from astrbot_plugin_message_card.card_template import (
    CardData,
    CardOptions,
    build_card_html,
)
from astrbot_plugin_message_card.card_theme import palette_for


@unittest.skipUnless(os.getenv("RUN_RENDER_TESTS") == "1", "set RUN_RENDER_TESTS=1")
class RenderSmokeTest(unittest.TestCase):
    def test_render_light_and_dark(self):
        async def run():
            builder = ContentBuilder(ContentOptions())
            content = await builder.build(
                "# 渲染测试\n\n公式 $E=mc^2$。\n\n| A | B |\n| - | - |\n| 1 | 2 |",
                palette="dark",
            )
            data = CardData(
                content_html=content.html,
                mode="dark",
                palette=palette_for("dark"),
                theme_label="夜间模式",
                header_title="消息卡片",
                header_subtitle="2026-11-01 · 22:00 · Asia/Shanghai",
                footer_text="AstrBot · 测试",
                sender="测试",
                platform="QQ",
                time_text="22:00",
                date_text="2026-11-01",
                timezone="Asia/Shanghai",
            )
            html = build_card_html(data, CardOptions(width=720), pygments_css=pygments_css("dark"))
            renderer = CardRenderer(
                RenderOptions(
                    width=720,
                    device_scale_factor=1,
                    browser_path=os.getenv("MESSAGE_CARD_CHROME", ""),
                    timeout=60,
                )
            )
            try:
                images = await renderer.render(html, color_scheme="dark")
                self.assertEqual(len(images), 1)
                self.assertGreater(len(images[0].data), 10_000)
                output = Path("/tmp/message_card_smoke.png")
                output.write_bytes(images[0].data)
            finally:
                await renderer.stop()

        asyncio.run(run())

    def test_tall_card_is_sliced(self):
        async def run():
            paragraphs = ["# 长图切片\n"] + [
                f"## 小节 {index}\n\n这是第 {index} 段内容，用于触发多图切片。" * 2
                for index in range(1, 60)
            ]
            builder = ContentBuilder(ContentOptions())
            content = await builder.build("\n\n".join(paragraphs), palette="dark")
            data = CardData(
                content_html=content.html,
                mode="dark",
                palette=palette_for("dark"),
                theme_label="夜间模式",
                header_title="长图切片",
                footer_text="AstrBot · 切片测试",
                sender="测试",
                platform="QQ",
                time_text="22:00",
                date_text="2026-11-01",
                timezone="Asia/Shanghai",
            )
            html = build_card_html(data, CardOptions(width=640))
            renderer = CardRenderer(
                RenderOptions(
                    width=640,
                    device_scale_factor=1,
                    browser_path=os.getenv("MESSAGE_CARD_CHROME", ""),
                    timeout=90,
                    max_slice_height=1200,
                )
            )
            try:
                images = await renderer.render(html, color_scheme="dark")
                self.assertGreater(len(images), 1)
                self.assertTrue(all(len(image.data) > 1_000 for image in images))
            finally:
                await renderer.stop()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
