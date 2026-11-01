"""Headless Chromium renderer used by the message-card plugin.

The renderer keeps one browser alive for the lifetime of the plugin, opens a
fresh context for every card, blocks every non-data request, and slices very
tall cards into several images so a single screenshot never exceeds Chromium's
raster limits.
"""

from __future__ import annotations

import asyncio
import math
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from astrbot.api import logger


class RenderError(RuntimeError):
    """Raised when Chromium is unavailable or a screenshot fails."""


@dataclass(slots=True)
class RenderOptions:
    width: int = 920
    device_scale_factor: float = 2.0
    image_format: str = "png"
    jpeg_quality: int = 92
    timeout: float = 45.0
    max_slice_height: int = 7000
    timezone_id: str = "Asia/Shanghai"
    color_scheme: str = "light"
    browser_path: str = ""
    max_concurrent: int = 2


@dataclass(slots=True)
class RenderedImage:
    data: bytes
    width: int
    height: int
    index: int = 0
    total: int = 1


_COMMON_BROWSERS = (
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/microsoft-edge",
    "/snap/bin/chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)


def _png_size(data: bytes) -> tuple[int, int]:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    # JPEG: scan for SOF markers.  Only used for diagnostics; failing is fine.
    if data.startswith(b"\xff\xd8"):
        index = 2
        while index + 9 < len(data):
            if data[index] != 0xFF:
                index += 1
                continue
            marker = data[index + 1]
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                return int.from_bytes(data[index + 7 : index + 9], "big"), int.from_bytes(data[index + 5 : index + 7], "big")
            if index + 4 > len(data):
                break
            length = int.from_bytes(data[index + 2 : index + 4], "big")
            index += 2 + max(2, length)
    return 0, 0


class CardRenderer:
    """Lifecycle-managed Playwright renderer."""

    def __init__(self, options: RenderOptions | None = None) -> None:
        self.options = options or RenderOptions()
        self._playwright_manager = None
        self._playwright = None
        self._browser = None
        self._start_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(max(1, int(self.options.max_concurrent)))
        self._executable_path = ""
        self.last_error = ""

    def update_options(self, options: RenderOptions) -> None:
        self.options = options
        # The concurrency limit is cheap to recreate; browser and format are
        # re-read per render.
        self._semaphore = asyncio.Semaphore(max(1, int(options.max_concurrent)))

    # ------------------------------------------------------------------ setup

    def _discover_executable(self) -> str:
        candidates: list[str] = []
        configured = str(self.options.browser_path or "").strip()
        if configured:
            candidates.append(configured)
        for env_name in (
            "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH",
            "MESSAGE_CARD_CHROME_PATH",
            "CHROME_PATH",
            "CHROMIUM_PATH",
        ):
            value = os.environ.get(env_name, "").strip()
            if value:
                candidates.append(value)
        for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "chrome"):
            found = shutil.which(name)
            if found:
                candidates.append(found)
        candidates.extend(_COMMON_BROWSERS)
        for candidate in candidates:
            try:
                path = Path(candidate).expanduser()
                if path.is_file() and os.access(path, os.X_OK):
                    return str(path)
            except OSError:
                continue
        return ""

    async def _launch_browser(self):
        try:
            from playwright.async_api import async_playwright
        except Exception as exc:  # pragma: no cover - depends on host install
            raise RenderError(
                "未安装 playwright。请先执行 `pip install playwright`，"
                "再执行 `python -m playwright install chromium`（或配置浏览器路径）。"
            ) from exc

        try:
            self._playwright_manager = async_playwright()
            self._playwright = await self._playwright_manager.start()
        except Exception as exc:
            raise RenderError(f"Playwright 初始化失败：{exc}") from exc

        args = [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--hide-scrollbars",
            "--mute-audio",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-sync",
            "--disable-extensions",
            "--force-color-profile=srgb",
            "--font-render-hinting=medium",
        ]
        executable = self._discover_executable()
        launch_errors: list[str] = []
        candidates: list[str | None] = []
        if executable:
            candidates.append(executable)
        candidates.append(None)
        for candidate in candidates:
            try:
                kwargs = {"headless": True, "args": args}
                if candidate:
                    kwargs["executable_path"] = candidate
                self._browser = await self._playwright.chromium.launch(**kwargs)
                self._executable_path = candidate or "playwright-bundled"
                logger.info("[message_card] Chromium 已启动：%s", self._executable_path)
                return
            except Exception as exc:
                launch_errors.append(f"{candidate or 'bundled'}: {exc}")
                self._browser = None
                continue
        await self._stop_playwright()
        detail = " | ".join(launch_errors)[-1200:]
        raise RenderError(
            "无法启动 Chromium。请执行 `python -m playwright install chromium`，"
            "或在插件配置里填写可用的 Chromium/Chrome 路径。"
            f" 详情：{detail}"
        )

    async def start(self) -> None:
        if self._browser is not None and getattr(self._browser, "is_connected", lambda: False)():
            return
        async with self._start_lock:
            if self._browser is not None and getattr(self._browser, "is_connected", lambda: False)():
                return
            await self._launch_browser()

    async def _stop_playwright(self) -> None:
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
            self._playwright_manager = None

    async def stop(self) -> None:
        async with self._start_lock:
            await self._stop_playwright()

    # ----------------------------------------------------------------- render

    async def render(self, html: str, *, color_scheme: str = "light") -> list[RenderedImage]:
        options = self.options
        fmt = "jpeg" if str(options.image_format).lower() in {"jpeg", "jpg"} else "png"
        quality = max(40, min(100, int(options.jpeg_quality)))
        scale = max(1.0, min(3.0, float(options.device_scale_factor)))
        timeout = max(8.0, min(180.0, float(options.timeout)))
        slice_css_height = max(1200, int(options.max_slice_height))
        # Chromium's raster limit is in device pixels.  Keep each slice under
        # ~15000 physical pixels even when the user selects 3x scaling.
        max_css_height = max(1200, min(slice_css_height, int(15000 / scale)))

        async with self._semaphore:
            try:
                await asyncio.wait_for(self.start(), timeout=min(30.0, timeout))
                return await asyncio.wait_for(
                    self._render_once(
                        html,
                        fmt=fmt,
                        quality=quality,
                        scale=scale,
                        timeout=timeout,
                        max_css_height=max_css_height,
                        color_scheme=color_scheme,
                    ),
                    timeout=timeout,
                )
            except RenderError:
                raise
            except asyncio.TimeoutError as exc:
                self.last_error = "渲染超时"
                # A hung context can poison the browser; recycle it lazily.
                await self._stop_playwright()
                raise RenderError("渲染超时，已回收浏览器实例") from exc
            except Exception as exc:
                self.last_error = str(exc)
                raise RenderError(f"截图失败：{exc}") from exc

    async def _render_once(
        self,
        html: str,
        *,
        fmt: str,
        quality: int,
        scale: float,
        timeout: float,
        max_css_height: int,
        color_scheme: str,
    ) -> list[RenderedImage]:
        assert self._browser is not None
        options = self.options
        viewport_width = max(600, min(1600, int(options.width) + 40))
        context = await self._browser.new_context(
            viewport={"width": viewport_width, "height": 1200},
            device_scale_factor=scale,
            locale="zh-CN",
            timezone_id=options.timezone_id or "Asia/Shanghai",
            color_scheme="dark" if color_scheme == "dark" else "light",
            java_script_enabled=False,
            bypass_csp=True,
        )

        async def block_requests(route):
            url = route.request.url
            if url.startswith(("data:", "about:", "blob:")):
                await route.continue_()
            else:
                await route.abort()

        await context.route("**/*", block_requests)
        page = await context.new_page()
        page.set_default_timeout(timeout * 1000)
        try:
            await page.set_content(html, wait_until="load", timeout=timeout * 1000)
            # JS is disabled, but Playwright's own evaluate still works; this
            # waits for webfonts and data-URI images before measuring.
            await page.evaluate(
                """async () => {
                    if (document.fonts && document.fonts.ready) await document.fonts.ready;
                    await Promise.all(Array.from(document.images).map((img) => {
                        if (img.complete) return Promise.resolve();
                        return new Promise((resolve) => {
                            img.addEventListener('load', resolve, {once: true});
                            img.addEventListener('error', resolve, {once: true});
                            setTimeout(resolve, 3000);
                        });
                    }));
                }"""
            )
            # Normalize every content image to a fixed long edge while
            # preserving its aspect ratio.  CSS alone would either leave small
            # memes tiny or crop large images to a square.  JS is disabled for
            # page scripts, but this controlled evaluate runs via CDP.
            await page.evaluate(
                """() => {
                    const root = document.documentElement;
                    const raw = getComputedStyle(root).getPropertyValue('--image-box');
                    const box = Number.parseFloat(raw) || 380;
                    for (const img of document.querySelectorAll('.content img')) {
                        const w = img.naturalWidth || 0;
                        const h = img.naturalHeight || 0;
                        if (!w || !h) continue;
                        const scale = box / Math.max(w, h);
                        img.style.width = Math.round(w * scale) + 'px';
                        img.style.height = Math.round(h * scale) + 'px';
                        img.style.maxWidth = '100%';
                        img.style.maxHeight = 'none';
                        img.style.objectFit = 'contain';
                    }
                }"""
            )
            await page.wait_for_timeout(80)
            card = page.locator("#card")
            box = await card.bounding_box()
            if not box or box["width"] <= 0 or box["height"] <= 0:
                raise RenderError("页面中没有可截图的卡片")
            total_height = float(box["height"])
            screenshots: list[bytes] = []
            if total_height <= max_css_height:
                kwargs = {
                    "type": fmt,
                    "omit_background": fmt == "png",
                    "animations": "disabled",
                    "caret": "hide",
                    "scale": "device",
                }
                if fmt == "jpeg":
                    kwargs["quality"] = quality
                screenshots.append(await card.screenshot(**kwargs))
            else:
                # Page.screenshot(clip=...) is limited to the viewport in
                # headless Chromium.  Resize the viewport to the exact slice
                # height and scroll to the slice; this is reliable for very
                # tall cards and avoids one enormous bitmap.
                viewport_width = max(320, math.ceil(float(box["width"])))
                offset = 0.0
                while offset < total_height:
                    height = min(float(max_css_height), total_height - offset)
                    await page.set_viewport_size(
                        {
                            "width": viewport_width,
                            "height": max(1, math.ceil(height)),
                        }
                    )
                    await page.evaluate("(y) => window.scrollTo(0, y)", offset)
                    await page.wait_for_timeout(35)
                    kwargs = {
                        "type": fmt,
                        "omit_background": fmt == "png",
                        "animations": "disabled",
                        "caret": "hide",
                        "scale": "device",
                    }
                    if fmt == "jpeg":
                        kwargs["quality"] = quality
                    screenshots.append(await page.screenshot(**kwargs))
                    offset += height
            results: list[RenderedImage] = []
            for index, data in enumerate(screenshots):
                width_px, height_px = _png_size(data)
                results.append(
                    RenderedImage(
                        data=data,
                        width=width_px,
                        height=height_px,
                        index=index,
                        total=len(screenshots),
                    )
                )
            return results
        finally:
            try:
                await context.close()
            except Exception:
                pass

    def diagnostics(self) -> dict:
        """Return non-secret state for a /render status command and logs."""

        try:
            from playwright.async_api import async_playwright  # noqa: F401
        except Exception:
            pass
        return {
            "playwright_installed": _module_available("playwright"),
            "browser_running": bool(self._browser is not None),
            "browser_path": self._executable_path,
            "detected_browser": self._discover_executable(),
            "last_error": self.last_error,
        }


def _module_available(name: str) -> bool:
    try:
        return __import__(name) is not None
    except Exception:
        return False


__all__ = ["CardRenderer", "RenderError", "RenderOptions", "RenderedImage"]
