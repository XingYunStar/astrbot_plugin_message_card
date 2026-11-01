"""Generate README preview images with the real card pipeline.

Usage (from the repository root):
    PYTHONPATH=. python -m astrbot_plugin_message_card.tests.generate_previews

This uses the synthetic banners in ``docs/`` rather than the optional host image
API, so previews are reproducible and do not depend on a running service.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from astrbot_plugin_message_card.card_assets import AssetConfig, AssetResolver
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
from astrbot_plugin_message_card.card_theme import accent_for, palette_for

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"

DARK_TEXT = """# 愿此行，终抵群星

这是一条由 **AstrBot** 渲染的消息卡片，支持 Markdown、HTML、LaTeX 与图片。

> 主题会根据 `Asia/Shanghai` 的日间区间自动切换；这是一条夜间消息。

## 一张卡片能做什么

- 表格、列表、引用、任务清单与代码高亮
- 行内公式 $e^{i\\pi}+1=0$，以及块级公式
- 把消息链里的表情包 / 图片嵌入正文
- 页眉、页脚使用本地图、固定 URL 或横屏图片 API

| 能力 | 状态 |
| --- | --- |
| Markdown / HTML | 支持 |
| LaTeX / MathML | 原生渲染 |
| 昼夜主题 | 自动切换 |

```python
async def render(text: str) -> Image:
    return await card.render(text)
```

$$
\\int_0^\\infty e^{-x^2}\\,dx = \\frac{\\sqrt{\\pi}}{2}
$$
"""

LIGHT_TEXT = """# 白昼来信

日间主题采用明亮纸张、青蓝强调色与柔和阴影，适合白天在群里阅读。

## 渲染范围

| 输入 | 输出 |
| --- | --- |
| Markdown | 标题、表格、代码、列表 |
| HTML 片段 | 白名单清洗后渲染 |
| LaTeX | 转原生 MathML |
| 消息图片 | 压入卡片正文 |

> 页眉与页脚都可以单独关闭；图片加载失败时自动回退到主题装饰。

行内公式 $\\sum_{i=1}^{n} i = \\frac{n(n+1)}{2}$，以及一段 `inline code`。
"""


async def build_preview(
    *,
    mode: str,
    text: str,
    header: str,
    footer: str | None,
    title: str,
    subtitle: str,
    output: str,
) -> None:
    palette = palette_for(mode)
    assets = AssetResolver(AssetConfig(fetch_timeout=10))
    try:
        header_uri = await assets.fetch_content_image(
            str(DOCS / header), trusted=True, max_side=2200
        )
        footer_uri = None
        if footer:
            footer_uri = await assets.fetch_content_image(
                str(DOCS / footer), trusted=True, max_side=1800
            )
        content = await ContentBuilder(ContentOptions()).build(text, palette=mode)
        data = CardData(
            content_html=content.html,
            mode=mode,
            palette=palette,
            theme_label="夜间模式" if mode == "dark" else "日间模式",
            header_image=header_uri,
            footer_image=footer_uri,
            header_title=title,
            header_subtitle=subtitle,
            footer_text="星穹助手",
            bot_name="星穹助手",
            sender="星陨",
            platform="QQ",
            time_text="22:00" if mode == "dark" else "13:30",
            date_text="2026-11-01",
            timezone="Asia/Shanghai",
            status_text="ONLINE" if mode == "dark" else "CARD",
        )
        html = build_card_html(
            data,
            CardOptions(width=720, font_size=28, accent=accent_for(mode, "")),
            pygments_css=pygments_css(mode),
        )
        renderer = CardRenderer(
            RenderOptions(
                width=720,
                device_scale_factor=2,
                browser_path="/usr/bin/chromium",
                timeout=60,
                max_slice_height=7500,
            )
        )
        try:
            images = await renderer.render(html, color_scheme=mode)
            target = DOCS / output
            target.write_bytes(images[0].data)
            print(f"wrote {target} ({images[0].width}x{images[0].height})")
        finally:
            await renderer.stop()
    finally:
        await assets.close()


async def main() -> None:
    DOCS.mkdir(parents=True, exist_ok=True)
    await build_preview(
        mode="dark",
        text=DARK_TEXT,
        header="banner-night.jpg",
        footer="banner-footer.jpg",
        title="星穹消息",
        subtitle="2026-11-01 · 22:00 · Asia/Shanghai",
        output="preview-dark.png",
    )
    await build_preview(
        mode="light",
        text=LIGHT_TEXT,
        header="banner-day.jpg",
        footer=None,
        title="白昼来信",
        subtitle="2026-11-01 · 13:30 · Asia/Shanghai",
        output="preview-light.png",
    )


if __name__ == "__main__":
    asyncio.run(main())
