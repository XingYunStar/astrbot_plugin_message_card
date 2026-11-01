"""Message Card — render AstrBot replies as beautiful images.

The plugin converts outgoing LLM results (and an explicit ``/render`` command)
into a single, theme-aware card.  Markdown, sanitized HTML, LaTeX/MathML and
images already present in the message chain are supported.  Header and footer
images can come from a local file, a URL, or a landscape image API; when the API
is bound to the Docker host, a best-effort gateway fallback is available.

Design goals:
* never break normal chat when rendering fails;
* never let untrusted message content execute scripts or reach the network;
* keep the day/night decision in a real IANA time zone;
* work with the normal AstrBot output pipeline, including images added by
  plugins such as meme_manager that use ``on_decorating_result``.
"""

from __future__ import annotations

import asyncio
import random
import re
import sys
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PLUGIN_NAME = "astrbot_plugin_message_card"

try:  # AstrBot loads plugins as packages in normal installations.
    from .card_assets import AssetConfig, AssetError, AssetResolver
    from .card_content import ContentBuilder, ContentOptions, pygments_css
    from .card_renderer import CardRenderer, RenderError, RenderOptions
    from .card_template import CardData, CardOptions, build_card_html
    from .card_theme import accent_for, palette_for, resolve_theme
    from .page_web import PageWebUI
except ImportError:  # Support direct ``python main.py`` style repositories.
    _PLUGIN_DIR = str(Path(__file__).resolve().parent)
    if _PLUGIN_DIR not in sys.path:
        sys.path.insert(0, _PLUGIN_DIR)
    from card_assets import AssetConfig, AssetError, AssetResolver
    from card_content import ContentBuilder, ContentOptions, pygments_css
    from card_renderer import CardRenderer, RenderError, RenderOptions
    from card_template import CardData, CardOptions, build_card_html
    from card_theme import accent_for, palette_for, resolve_theme
    from page_web import PageWebUI

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import (
    At,
    AtAll,
    Face,
    Image,
    Plain,
    Record,
    Reply,
    Video,
)
from astrbot.api.star import Context, Star
from astrbot.core.message.message_event_result import ResultContentType
from astrbot.core.star.star_tools import StarTools

HELP_TEXT = """消息卡片渲染
/render <内容>        渲染一段 Markdown/HTML/LaTeX 内容
/render html <内容>   强制按 HTML 片段渲染
/render md <内容>     强制按 Markdown 渲染
/render               渲染被引用消息（或直接回复一条消息发送 /render）
/render status        查看浏览器、主题与 API 配置状态
/render help          显示本帮助

提示：插件默认只自动转换 LLM 回复；可在 WebUI 的插件配置中开启全量转换、调整昼夜时段、页眉页脚图片和图片 API。"""

_COMMAND_NAMES = ("render", "渲染", "消息卡片")
_PLATFORM_LABELS = {
    "aiocqhttp": "QQ",
    "qq_official": "QQ 频道",
    "qq_official_webhook": "QQ 频道",
    "telegram": "Telegram",
    "discord": "Discord",
    "lark": "飞书",
    "wecom": "企业微信",
    "wecom_ai_bot": "企业微信",
    "dingtalk": "钉钉",
    "slack": "Slack",
    "kook": "KOOK",
    "vocechat": "VoceChat",
    "weixin_official_account": "微信公众号",
    "weixin_oc": "微信",
    "satori": "Satori",
    "misskey": "Misskey",
    "line": "LINE",
    "matrix": "Matrix",
    "mattermost": "Mattermost",
    "webchat": "WebChat",
}


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on", "启用", "是"}:
            return True
        if lowered in {"0", "false", "no", "off", "禁用", "否", ""}:
            return False
    return default


def _as_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        result = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, result))


def _as_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, result))


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _split_platforms(value: Any) -> frozenset[str]:
    if isinstance(value, (list, tuple, set)):
        parts = [str(item) for item in value]
    else:
        parts = re.split(r"[\s,，、;；|]+", str(value or ""))
    return frozenset(part.strip().lower() for part in parts if part.strip())


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _format_template(template: str, values: dict[str, Any]) -> str:
    try:
        return str(template or "").format_map(_SafeDict(values))
    except Exception:
        return str(template or "")


@dataclass(slots=True)
class _Settings:
    enabled: bool = True
    trigger_mode: str = "llm"
    min_chars: int = 24
    render_when_rich: bool = True
    skip_streaming: bool = True
    render_images: bool = True
    render_image_only: bool = False
    include_meme_manager_pending: bool = True
    preserve_segmented_reply: bool = False
    preserve_tts: bool = False
    preserve_forward_threshold: bool = False
    skip_platforms: frozenset[str] = field(default_factory=frozenset)

    theme_mode: str = "auto"
    timezone: str = "Asia/Shanghai"
    day_start: str = "06:30"
    day_end: str = "18:30"
    accent_light: str = ""
    accent_dark: str = ""

    width: int = 720
    radius: int = 26
    font_size: float = 28.0
    line_height: float = 1.72
    custom_css: str = ""
    eyebrow: str = "MESSAGE CARD"
    show_meta: bool = True
    header_title: str = "消息卡片"
    header_subtitle: str = ""
    footer_text: str = "{bot_name}"

    header_enabled: bool = True
    header_source: str = "theme"
    header_path: str = ""
    header_url: str = ""
    header_api_url: str = "http://127.0.0.1:8888/image/firefly_cg/?mode=pc"
    header_api_json_path: str = ""
    header_host_fallback: bool = True
    header_cache_seconds: int = 45
    header_height: int = 280
    header_fit: str = "cover"
    header_position: str = "center"
    header_show_text: bool = True

    footer_enabled: bool = True
    footer_source: str = "theme"
    footer_path: str = ""
    footer_url: str = ""
    footer_api_url: str = ""
    footer_api_json_path: str = ""
    footer_host_fallback: bool = True
    footer_cache_seconds: int = 45
    footer_height: int = 160
    footer_fit: str = "cover"
    footer_position: str = "center"
    footer_show_text: bool = True

    image_format: str = "png"
    jpeg_quality: int = 92
    device_scale_factor: float = 2.0
    render_timeout: float = 45.0
    max_slice_height: int = 7000
    browser_path: str = ""
    max_concurrent: int = 2
    keep_renders: int = 120

    input_mode: str = "auto"
    allow_inline_style: bool = True
    allow_remote_images: bool = True
    allow_private_network_images: bool = False
    allow_local_images: bool = False
    max_remote_images: int = 8
    math_enabled: bool = True
    image_size: int = 380


class MessageCardPlugin(Star):
    """将 AstrBot 回复渲染为带昼夜主题、页眉页脚和 LaTeX 的卡片图片。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self.data_dir = self._resolve_data_dir()
        self.renders_dir = self.data_dir / "renders"
        self.renders_dir.mkdir(parents=True, exist_ok=True)
        self.assets = AssetResolver(self._asset_config(self._settings()))
        self.renderer = CardRenderer(self._render_options(self._settings()))
        self.webui = PageWebUI(self)
        self.webui.register()
        self._avatar_cache: dict[str, tuple[float, str | None]] = {}
        self._bot_profile_cache: dict[str, tuple[float, tuple[str, str | None]]] = {}
        self._render_count = 0
        self._failed_count = 0
        self._last_error = ""
        logger.info("[message_card] 插件已加载，渲染目录：%s", self.renders_dir)

    # ------------------------------------------------------------------ paths

    @staticmethod
    def _resolve_data_dir() -> Path:
        try:
            return Path(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception:
            fallback = Path("data") / "plugin_data" / PLUGIN_NAME
            fallback.mkdir(parents=True, exist_ok=True)
            return fallback

    # ----------------------------------------------------------------- config

    def _group(self, name: str) -> dict[str, Any]:
        value = None
        try:
            value = self.config.get(name)
        except Exception:
            value = None
        return value if isinstance(value, dict) else {}

    def _get(self, group: str, key: str, default: Any) -> Any:
        grouped = self._group(group)
        if key in grouped:
            return grouped.get(key)
        try:
            return self.config.get(f"{group}.{key}", default)
        except Exception:
            return default

    def _settings(self) -> _Settings:
        s = _Settings()
        s.enabled = _as_bool(self.config.get("enabled", True), True)
        s.trigger_mode = _as_str(self._get("trigger", "mode", "llm"), "llm").lower()
        if s.trigger_mode not in {"llm", "all", "command"}:
            s.trigger_mode = "llm"
        s.min_chars = _as_int(self._get("trigger", "min_chars", 24), 24, 0, 100000)
        s.render_when_rich = _as_bool(self._get("trigger", "render_when_rich", True), True)
        s.skip_streaming = _as_bool(self._get("trigger", "skip_streaming", True), True)
        s.render_images = _as_bool(self._get("trigger", "render_images", True), True)
        s.render_image_only = _as_bool(self._get("trigger", "render_image_only", False), False)
        s.include_meme_manager_pending = _as_bool(
            self._get("trigger", "include_meme_manager_pending", True), True
        )
        s.preserve_segmented_reply = _as_bool(
            self._get("compat", "preserve_segmented_reply", False), False
        )
        s.preserve_tts = _as_bool(self._get("compat", "preserve_tts", False), False)
        s.preserve_forward_threshold = _as_bool(
            self._get("compat", "preserve_forward_threshold", False), False
        )
        s.skip_platforms = _split_platforms(self._get("trigger", "skip_platforms", ""))

        s.theme_mode = _as_str(self._get("theme", "mode", "auto"), "auto").lower()
        if s.theme_mode not in {"auto", "light", "dark"}:
            s.theme_mode = "auto"
        s.timezone = _as_str(self._get("theme", "timezone", "Asia/Shanghai"), "Asia/Shanghai")
        s.day_start = _as_str(self._get("theme", "day_start", "06:30"), "06:30")
        s.day_end = _as_str(self._get("theme", "day_end", "18:30"), "18:30")
        s.accent_light = _as_str(self._get("theme", "accent_light", ""))
        s.accent_dark = _as_str(self._get("theme", "accent_dark", ""))

        s.width = _as_int(self._get("appearance", "width", 720), 720, 520, 1400)
        s.radius = _as_int(self._get("appearance", "radius", 26), 26, 0, 48)
        s.font_size = _as_float(self._get("appearance", "font_size", 28), 28.0, 16.0, 36.0)
        s.line_height = _as_float(self._get("appearance", "line_height", 1.72), 1.72, 1.2, 2.4)
        s.custom_css = _as_str(self._get("appearance", "custom_css", ""))
        s.eyebrow = _as_str(self._get("appearance", "eyebrow", "MESSAGE CARD"), "MESSAGE CARD")
        s.show_meta = _as_bool(self._get("appearance", "show_meta", True), True)
        s.header_title = _as_str(self._get("header", "title", "消息卡片"), "消息卡片")
        s.header_subtitle = _as_str(self._get("header", "subtitle", ""))
        s.footer_text = _as_str(
            self._get("footer", "text", "{bot_name}"),
            "{bot_name}",
        )

        s.header_enabled = _as_bool(self._get("header", "enabled", True), True)
        s.header_source = _as_str(self._get("header", "source", "theme"), "theme").lower()
        s.header_path = _as_str(self._get("header", "path", ""))
        s.header_url = _as_str(self._get("header", "url", ""))
        s.header_api_url = _as_str(
            self._get("header", "api_url", "http://127.0.0.1:8888/image/firefly_cg/?mode=pc"),
            "http://127.0.0.1:8888/image/firefly_cg/?mode=pc",
        )
        s.header_api_json_path = _as_str(self._get("header", "api_json_path", ""))
        s.header_host_fallback = _as_bool(self._get("header", "host_fallback", True), True)
        s.header_cache_seconds = _as_int(self._get("header", "cache_seconds", 45), 45, 0, 86400)
        s.header_height = _as_int(self._get("header", "height", 280), 280, 140, 520)
        s.header_fit = _as_str(self._get("header", "fit", "cover"), "cover").lower()
        s.header_position = _as_str(self._get("header", "position", "center"), "center").lower()
        s.header_show_text = _as_bool(self._get("header", "show_text", True), True)

        s.footer_enabled = _as_bool(self._get("footer", "enabled", True), True)
        s.footer_source = _as_str(self._get("footer", "source", "theme"), "theme").lower()
        s.footer_path = _as_str(self._get("footer", "path", ""))
        s.footer_url = _as_str(self._get("footer", "url", ""))
        s.footer_api_url = _as_str(self._get("footer", "api_url", ""))
        s.footer_api_json_path = _as_str(self._get("footer", "api_json_path", ""))
        s.footer_host_fallback = _as_bool(self._get("footer", "host_fallback", True), True)
        s.footer_cache_seconds = _as_int(self._get("footer", "cache_seconds", 45), 45, 0, 86400)
        s.footer_height = _as_int(self._get("footer", "height", 160), 160, 90, 360)
        s.footer_fit = _as_str(self._get("footer", "fit", "cover"), "cover").lower()
        s.footer_position = _as_str(self._get("footer", "position", "center"), "center").lower()
        s.footer_show_text = _as_bool(self._get("footer", "show_text", True), True)

        s.image_format = _as_str(self._get("render", "format", "png"), "png").lower()
        if s.image_format not in {"png", "jpeg", "jpg"}:
            s.image_format = "png"
        s.jpeg_quality = _as_int(self._get("render", "quality", 92), 92, 40, 100)
        s.device_scale_factor = _as_float(
            self._get("render", "device_scale_factor", 2.0), 2.0, 1.0, 3.0
        )
        s.render_timeout = _as_float(self._get("render", "timeout", 45), 45.0, 8.0, 180.0)
        s.max_slice_height = _as_int(
            self._get("render", "max_slice_height", 7000), 7000, 1200, 15000
        )
        s.browser_path = _as_str(self._get("render", "browser_path", ""))
        s.max_concurrent = _as_int(self._get("render", "max_concurrent", 2), 2, 1, 6)
        s.keep_renders = _as_int(self._get("render", "keep_renders", 120), 120, 10, 2000)

        s.input_mode = _as_str(self._get("content", "input_mode", "auto"), "auto").lower()
        if s.input_mode not in {"auto", "markdown", "html"}:
            s.input_mode = "auto"
        s.allow_inline_style = _as_bool(
            self._get("content", "allow_inline_style", True), True
        )
        s.allow_remote_images = _as_bool(
            self._get("content", "allow_remote_images", True), True
        )
        s.allow_private_network_images = _as_bool(
            self._get("content", "allow_private_network_images", False), False
        )
        s.allow_local_images = _as_bool(
            self._get("content", "allow_local_images", False), False
        )
        s.max_remote_images = _as_int(
            self._get("content", "max_remote_images", 8), 8, 0, 30
        )
        s.math_enabled = _as_bool(self._get("content", "math", True), True)
        s.image_size = _as_int(self._get("content", "image_size", 380), 380, 180, 640)
        return s

    def _asset_config(self, s: _Settings) -> AssetConfig:
        return AssetConfig(
            allow_remote_images=s.allow_remote_images,
            allow_private_network_images=s.allow_private_network_images,
            allow_local_images=s.allow_local_images,
            fetch_timeout=max(3.0, min(30.0, s.render_timeout / 3.0)),
            max_image_mb=10.0,
            api_cache_seconds=s.header_cache_seconds,
        )

    def _render_options(self, s: _Settings) -> RenderOptions:
        return RenderOptions(
            width=s.width,
            device_scale_factor=s.device_scale_factor,
            image_format=s.image_format,
            jpeg_quality=s.jpeg_quality,
            timeout=s.render_timeout,
            max_slice_height=s.max_slice_height,
            timezone_id=s.timezone,
            browser_path=s.browser_path,
            max_concurrent=s.max_concurrent,
        )

    # ------------------------------------------------------------ message data

    def _platform_label(self, event: AstrMessageEvent) -> str:
        name = ""
        try:
            name = event.get_platform_name() or ""
        except Exception:
            pass
        return _PLATFORM_LABELS.get(str(name).lower(), str(name) or "AstrBot")

    async def _component_image_map(
        self,
        images: list[Image],
    ) -> tuple[dict[str, str], int]:
        image_map: dict[str, str] = {}
        failed = 0
        if not images:
            return image_map, failed
        results = await asyncio.gather(
            *(self.assets.component_to_data_uri(image) for image in images),
            return_exceptions=True,
        )
        for index, result in enumerate(results):
            if isinstance(result, Exception) or not result:
                failed += 1
                logger.debug("[message_card] 消息链图片 %s 转换失败：%s", index, result)
                continue
            image_map[f"attachment://{index}"] = result
        return image_map, failed

    def _chain_parts(
        self,
        chain: Iterable[Any],
        *,
        render_images: bool,
    ) -> tuple[str, list[Image], int]:
        """Flatten a message chain, preserving image positions as Markdown images.

        The neutral attachment URL is converted to a data URI by the content
        builder.  HTML mode strips these Markdown placeholders and appends a
        gallery instead, so the caller does not need to know the input mode at
        flattening time.
        """

        parts: list[str] = []
        images: list[Image] = []
        for component in chain:
            if isinstance(component, Plain):
                parts.append(component.text or "")
            elif isinstance(component, Image):
                if not render_images:
                    continue
                token = f"attachment://{len(images)}"
                images.append(component)
                parts.append(f"\n\n![消息图片]({token})\n\n")
            elif isinstance(component, AtAll):
                parts.append("@所有人 ")
            elif isinstance(component, At):
                name = getattr(component, "name", "") or getattr(component, "qq", "")
                parts.append(f"@{name} ")
            elif isinstance(component, Face):
                parts.append("[表情] ")
            elif isinstance(component, Record):
                parts.append("[语音] ")
            elif isinstance(component, Video):
                parts.append("[视频] ")
            elif isinstance(component, Reply):
                # Quoted context is intentionally not folded into the reply card.
                continue
            else:
                parts.append(f"[{component.__class__.__name__}] ")
        return "".join(parts).strip(), images, len(images)

    @staticmethod
    def _looks_rich(text: str) -> bool:
        if not text:
            return False
        patterns = (
            r"(?m)^\s{0,3}#{1,6}\s+\S",
            r"(?m)^\s{0,3}(?:```|~~~)",
            r"(?m)^\s{0,3}>\s+\S",
            r"(?m)^\s{0,3}(?:[-*+]|\d+[.)])\s+\S",
            r"(?m)^\s*\|?.+\|.+\n\s*\|?\s*:?-{3,}",
            r"\$\$.+?\$\$",
            r"(?<!\\)\$[^$\n]{1,300}\$",
            r"\\\(|\\\[",
            r"(?i)<(?:div|table|section|article|h[1-6]|pre|style|html|body)\b",
            r"!\[[^\]]*\]\([^)]+\)",
            r"\[[^\]]+\]\([^)]+\)",
        )
        return any(re.search(pattern, text, re.DOTALL) for pattern in patterns)

    def _should_render(self, s: _Settings, text: str, image_count: int, rich: bool) -> bool:
        if not s.enabled:
            return False
        if s.trigger_mode == "command":
            return False
        if not text.strip() and image_count == 0:
            return False
        if not text.strip() and image_count > 0 and not s.render_image_only:
            return False
        if len(text.strip()) < s.min_chars and not (rich and s.render_when_rich):
            return False
        return True

    # ---------------------------------------------------------------- built-ins

    def _core_config(self, event: AstrMessageEvent) -> Any:
        """Read the effective AstrBot config for this session.

        ``self.config`` is the plugin's own configuration; the built-in
        segmentation/forward/TTS switches live in AstrBot's main config.  A
        session may also override platform settings, so prefer the event's UMO.
        """

        try:
            umo = getattr(event, "unified_msg_origin", "") or ""
            if umo and hasattr(self.context, "get_config"):
                return self.context.get_config(umo)
            if hasattr(self.context, "get_config"):
                return self.context.get_config()
        except Exception:
            pass
        return {}

    @staticmethod
    def _cfg_get(config: Any, *keys: str, default: Any = None) -> Any:
        current = config
        for key in keys:
            if not isinstance(current, dict):
                return default
            current = current.get(key)
            if current is None:
                return default
        return current

    @staticmethod
    def _split_text_like_astrbot(text: str, segmented: dict[str, Any]) -> list[str]:
        """Mirror ResultDecorateStage's segmentation enough to know if it acts."""

        try:
            threshold = int(segmented.get("words_count_threshold", 150))
        except (TypeError, ValueError):
            threshold = 150
        if len(text) > threshold:
            return [text]

        mode = str(segmented.get("split_mode", "regex") or "regex").lower()
        if mode == "words":
            words = [
                str(word)
                for word in (segmented.get("split_words") or ["。", "？", "！", "~", "…"])
                if str(word)
            ]
            if not words:
                return [text]
            escaped = sorted((re.escape(word) for word in words), key=len, reverse=True)
            pattern = re.compile(f"(.*?({'|'.join(escaped)})|.+$)", re.DOTALL)
            pieces = pattern.findall(text)
            result: list[str] = []
            for piece in pieces:
                if isinstance(piece, tuple):
                    content = piece[0]
                    for word in words:
                        if content.endswith(word):
                            content = content[: -len(word)]
                            break
                    if content.strip():
                        result.append(content)
            return result or [text]

        regex = segmented.get("regex")
        try:
            pieces = re.findall(
                str(regex or r".*?[。？！~…]+|.+$"),
                text,
                re.DOTALL | re.MULTILINE,
            )
        except re.error:
            pieces = re.findall(r".*?[。？！~…]+|.+$", text, re.DOTALL | re.MULTILINE)
        cleaned: list[str] = []
        cleanup = str(segmented.get("content_cleanup_rule", "") or "")
        for piece in pieces:
            value = str(piece)
            if cleanup:
                try:
                    value = re.sub(cleanup, "", value)
                except re.error:
                    pass
            if value.strip():
                cleaned.append(value.strip())
        return cleaned or [text]

    def _segmentation_would_apply(
        self, event: AstrMessageEvent, result: Any, plain_texts: list[str]
    ) -> bool:
        cfg = self._core_config(event)
        segmented = self._cfg_get(cfg, "platform_settings", "segmented_reply", default={})
        if not isinstance(segmented, dict) or not segmented.get("enable"):
            return False
        platform = ""
        try:
            platform = str(event.get_platform_name() or "").lower()
        except Exception:
            pass
        if platform in {"qq_official", "weixin_official_account", "dingtalk"}:
            return False
        only_llm = bool(segmented.get("only_llm_result", True))
        if only_llm:
            try:
                if not result.is_model_result():
                    return False
            except Exception:
                return False

        texts = [text for text in plain_texts if text]
        if not texts:
            return False
        prefix = str(
            self._cfg_get(cfg, "platform_settings", "reply_prefix", default="") or ""
        )
        if prefix:
            texts[0] = prefix + texts[0]
        for text in texts:
            if len(self._split_text_like_astrbot(text, segmented)) > 1:
                return True
        return False

    def _forward_threshold_would_apply(
        self, event: AstrMessageEvent, plain_texts: list[str]
    ) -> bool:
        try:
            platform = str(event.get_platform_name() or "").lower()
        except Exception:
            return False
        if platform != "aiocqhttp":
            return False
        cfg = self._core_config(event)
        try:
            threshold = int(
                self._cfg_get(
                    cfg, "platform_settings", "forward_threshold", default=1500
                )
            )
        except (TypeError, ValueError):
            threshold = 1500
        texts = [text for text in plain_texts if text]
        if not texts:
            return 0 > threshold
        prefix = str(
            self._cfg_get(cfg, "platform_settings", "reply_prefix", default="") or ""
        )
        if prefix:
            texts[0] = prefix + texts[0]
        return sum(len(text) for text in texts) > threshold

    def _compat_skip_reason(
        self,
        s: _Settings,
        event: AstrMessageEvent,
        result: Any,
        plain_texts: list[str],
    ) -> str:
        if s.preserve_segmented_reply and self._segmentation_would_apply(
            event, result, plain_texts
        ):
            return "已启用「保留分段回复」，本次不转图"
        if s.preserve_forward_threshold and self._forward_threshold_would_apply(
            event, plain_texts
        ):
            return "已启用「保留转发消息字数阈值」，本次不转图"
        return ""

    async def _make_tts_record(
        self, event: AstrMessageEvent, result: Any, plain_text: str
    ) -> Record | None:
        """Synthesize TTS ourselves so an image and a voice message coexist.

        AstrBot's TTS stage only processes ``Plain`` components.  Since the
        card replaces them with images, we synthesize the same text here and
        append a ``Record`` after the image.  Core TTS then sees no Plain text
        and does not double-send audio.
        """

        if not plain_text.strip():
            return None
        try:
            if not result.is_llm_result():
                return None
        except Exception:
            return None
        cfg = self._core_config(event)
        tts_cfg = self._cfg_get(cfg, "provider_tts_settings", default={})
        if not isinstance(tts_cfg, dict) or not tts_cfg.get("enable"):
            return None
        try:
            from astrbot.core.star.session_llm_manager import SessionServiceManager

            if not await SessionServiceManager.should_process_tts_request(event):
                return None
        except Exception as exc:
            logger.debug("[message_card] TTS 会话开关检查失败：%s", exc)
            return None
        try:
            probability = max(0.0, min(float(tts_cfg.get("trigger_probability", 1)), 1.0))
        except (TypeError, ValueError):
            probability = 1.0
        if random.random() > probability:
            return None
        try:
            provider = self.context.get_using_tts_provider(event.unified_msg_origin)
        except Exception as exc:
            logger.debug("[message_card] 获取 TTS Provider 失败：%s", exc)
            return None
        if provider is None:
            return None
        try:
            audio_path = await asyncio.wait_for(provider.get_audio(plain_text), timeout=60)
        except asyncio.TimeoutError:
            logger.warning("[message_card] TTS 合成超时，仅发送图片。")
            return None
        except Exception as exc:
            logger.warning("[message_card] TTS 合成失败，仅发送图片：%s", exc)
            return None
        if not audio_path:
            logger.warning("[message_card] TTS 未返回音频路径，仅发送图片。")
            return None

        file_value = str(audio_path)
        if tts_cfg.get("use_file_service"):
            callback = str(cfg.get("callback_api_base", "") or "")
            if callback:
                try:
                    from astrbot.core import file_token_service

                    token = await file_token_service.register_file(file_value)
                    file_value = f"{callback}/api/file/{token}"
                except Exception as exc:
                    logger.warning("[message_card] TTS 文件服务注册失败，使用本地音频：%s", exc)
        return Record(file=file_value, url=file_value, text=plain_text)

    # ---------------------------------------------------------------- rendering

    def _theme(self, s: _Settings):
        return resolve_theme(
            s.theme_mode,
            s.timezone,
            s.day_start,
            s.day_end,
        )

    @staticmethod
    def _message_type_label(event: AstrMessageEvent) -> str:
        try:
            return "群聊" if event.is_group() else "私聊"
        except Exception:
            return "私聊"

    def _template_values(
        self,
        event: AstrMessageEvent,
        theme,
        *,
        sender: str,
        platform: str,
        bot_name: str = "机器人",
        bot_id: str = "",
    ) -> dict[str, Any]:
        return {
            "date": theme.date_text,
            "time": theme.time_text,
            "datetime": theme.datetime_text,
            "weekday": theme.weekday_text,
            "timezone": theme.timezone,
            "tz": theme.timezone,
            "theme": theme.label,
            "mode": theme.mode,
            "sender": sender,
            "platform": platform,
            "message_type": self._message_type_label(event),
            "bot": bot_name,
            "bot_name": bot_name,
            "bot_id": bot_id,
        }

    @staticmethod
    def _sender_avatar_candidate(event: AstrMessageEvent, sender_id: str) -> str:
        for key in ("sender_avatar", "avatar_url", "avatar", "user_avatar", "member_avatar"):
            value = event.get_extra(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        message = getattr(event, "message_obj", None)
        sender = getattr(message, "sender", None)
        for attr in ("avatar", "avatar_url", "head_img", "headimgurl", "head_url"):
            value = getattr(sender, attr, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
        raw = getattr(message, "raw_message", None)
        if isinstance(raw, dict):
            for key in ("sender_avatar", "avatar", "avatar_url"):
                value = raw.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            sender_raw = raw.get("sender")
            if isinstance(sender_raw, dict):
                for key in ("avatar", "avatar_url", "head_img", "headimgurl"):
                    value = sender_raw.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
        try:
            platform = str(event.get_platform_name() or "").lower()
        except Exception:
            platform = ""
        if platform == "aiocqhttp" and str(sender_id).isdigit():
            # OneBot v11 does not expose an avatar field; this is the official
            # QQ avatar endpoint and works for regular QQ numbers.
            return f"https://q1.qlogo.cn/g?b=qq&nk={sender_id}&s=640"
        return ""

    async def _resolve_avatar(
        self, event: AstrMessageEvent, sender_id: str, s: _Settings
    ) -> str | None:
        if not s.show_meta or not sender_id:
            return None
        try:
            platform = str(event.get_platform_name() or "").lower()
        except Exception:
            platform = ""
        key = f"{platform}:{sender_id}"
        now = time.monotonic()
        cached = self._avatar_cache.get(key)
        if cached and cached[0] > now:
            return cached[1]
        url = self._sender_avatar_candidate(event, sender_id)
        if not url:
            self._avatar_cache[key] = (now + 600, None)
            return None
        try:
            data_uri = await self.assets.fetch_content_image(
                url, trusted=True, max_side=160
            )
        except Exception as exc:
            logger.debug("[message_card] 头像获取失败：%s", exc)
            data_uri = None
        self._avatar_cache[key] = (now + (3600 if data_uri else 300), data_uri)
        return data_uri

    async def _resolve_bot_profile(
        self, event: AstrMessageEvent
    ) -> tuple[str, str | None]:
        """Return the bot nickname and avatar.

        OneBot v11 does not include the login profile in the event, so ask the
        adapter for ``get_login_info``.  The avatar uses the same QQ endpoint as
        sender avatars.  Both results are cached to keep rendering fast.
        """

        self_id = ""
        try:
            self_id = str(event.get_self_id() or "")
        except Exception:
            try:
                self_id = str(getattr(event.message_obj, "self_id", "") or "")
            except Exception:
                self_id = ""
        try:
            platform = str(event.get_platform_name() or "").lower()
        except Exception:
            platform = ""
        key = f"{platform}:{self_id}"
        now = time.monotonic()
        cached = self._bot_profile_cache.get(key)
        if cached and cached[0] > now:
            return cached[1]

        name = ""
        for extra_key in ("bot_name", "self_name", "login_nickname"):
            value = event.get_extra(extra_key)
            if isinstance(value, str) and value.strip():
                name = value.strip()
                break
        bot = getattr(event, "bot", None)
        if not name and bot is not None and hasattr(bot, "call_action"):
            try:
                info = await asyncio.wait_for(
                    bot.call_action("get_login_info"), timeout=4
                )
                if isinstance(info, dict):
                    name = str(info.get("nickname") or info.get("name") or "").strip()
                    if info.get("user_id"):
                        self_id = str(info["user_id"])
            except Exception as exc:
                logger.debug("[message_card] 获取机器人昵称失败：%s", exc)
        name = name or "机器人"

        avatar_url = ""
        for extra_key in ("bot_avatar", "bot_avatar_url", "self_avatar"):
            value = event.get_extra(extra_key)
            if isinstance(value, str) and value.strip():
                avatar_url = value.strip()
                break
        if not avatar_url and platform == "aiocqhttp" and self_id.isdigit():
            avatar_url = f"https://q1.qlogo.cn/g?b=qq&nk={self_id}&s=640"
        avatar: str | None = None
        if avatar_url:
            try:
                avatar = await self.assets.fetch_content_image(
                    avatar_url, trusted=True, max_side=160
                )
            except Exception as exc:
                logger.debug("[message_card] 获取机器人头像失败：%s", exc)
        result = (name, avatar)
        self._bot_profile_cache[key] = (now + 3600, result)
        return result

    async def _resolve_frame_images(
        self,
        s: _Settings,
        *,
        header_values: dict[str, Any],
        footer_values: dict[str, Any],
    ) -> tuple[str | None, str | None]:
        async def header() -> str | None:
            if not s.header_enabled:
                return None
            try:
                return await self.assets.resolve_configured(
                    s.header_source,
                    path=_format_template(s.header_path, header_values),
                    url=_format_template(s.header_url, header_values),
                    api_url=_format_template(s.header_api_url, header_values),
                    json_path=s.header_api_json_path,
                    allow_host_fallback=s.header_host_fallback,
                    cache_seconds=s.header_cache_seconds,
                    base_dir=Path(__file__).resolve().parent,
                    data_dir=self.data_dir,
                    max_side=2400,
                )
            except Exception as exc:
                logger.warning("[message_card] 页眉图片加载失败：%s", exc)
                return None

        async def footer() -> str | None:
            if not s.footer_enabled:
                return None
            try:
                return await self.assets.resolve_configured(
                    s.footer_source,
                    path=_format_template(s.footer_path, footer_values),
                    url=_format_template(s.footer_url, footer_values),
                    api_url=_format_template(s.footer_api_url, footer_values),
                    json_path=s.footer_api_json_path,
                    allow_host_fallback=s.footer_host_fallback,
                    cache_seconds=s.footer_cache_seconds,
                    base_dir=Path(__file__).resolve().parent,
                    data_dir=self.data_dir,
                    max_side=1800,
                )
            except Exception as exc:
                logger.warning("[message_card] 页脚图片加载失败：%s", exc)
                return None

        return await asyncio.gather(header(), footer())

    async def _render_content(
        self,
        event: AstrMessageEvent,
        *,
        text: str,
        images: list[Image],
        mode_override: str = "auto",
        force: bool = False,
    ) -> list[Path]:
        s = self._settings()
        if force:
            s.enabled = True
            s.min_chars = 0
        # Pick up WebUI configuration changes without a plugin reload.
        self.assets.cfg = self._asset_config(s)
        theme = self._theme(s)
        accent = accent_for(theme.mode, s.accent_light if theme.mode == "light" else s.accent_dark)
        palette = palette_for(theme.mode)
        sender = ""
        sender_id = ""
        try:
            sender = event.get_sender_name() or event.get_sender_id() or ""
            sender_id = str(event.get_sender_id() or "")
        except Exception:
            sender = ""
            sender_id = ""
        platform = self._platform_label(event)
        avatar_image = await self._resolve_avatar(event, sender_id, s)
        bot_name, bot_avatar = ("机器人", None)
        if s.footer_enabled:
            bot_name, bot_avatar = await self._resolve_bot_profile(event)
        values = self._template_values(
            event,
            theme,
            sender=str(sender),
            platform=platform,
            bot_name=bot_name,
            bot_id=str(event.get_self_id() or ""),
        )
        header_values = dict(values)
        footer_values = dict(values)

        header_image, footer_image = await self._resolve_frame_images(
            s,
            header_values=header_values,
            footer_values=footer_values,
        )

        input_mode = mode_override if mode_override in {"markdown", "html"} else s.input_mode
        html_mode = input_mode == "html" or (
            input_mode == "auto"
            and bool(
                re.search(
                    r"(?is)<(?:div|table|section|article|h[1-6]|style|html|body)\b",
                    text,
                )
            )
        )
        if not text.strip() and images and not html_mode:
            text = "\n\n".join(
                f"![消息图片](attachment://{i})" for i in range(len(images))
            )

        if html_mode and images:
            gallery = "".join(
                f'<figure class="message-image"><img src="attachment://{i}" alt="消息图片"></figure>'
                for i in range(len(images))
            )
            # The flattened chain contains Markdown image placeholders even
            # when the surrounding content is HTML; turn them into a real image
            # gallery before parsing the fragment.
            text = re.sub(r"!\[[^\]]*\]\(attachment://\d+\)", "", text).strip()
            text = (text + "\n" + gallery).strip() if text else gallery

        image_map, failed_images = await self._component_image_map(images)
        content_options = ContentOptions(
            input_mode=input_mode,
            allow_inline_style=s.allow_inline_style,
            allow_remote_images=s.allow_remote_images,
            allow_local_images=s.allow_local_images,
            max_remote_images=s.max_remote_images,
            math_enabled=s.math_enabled,
        )
        content_builder = ContentBuilder(content_options)

        async def fetch_image(src: str, trusted: bool = False) -> str | None:
            return await self.assets.fetch_content_image(src, trusted=trusted)

        content = await content_builder.build(
            text,
            mode=input_mode,
            image_map=image_map,
            fetch_image=fetch_image,
            palette=theme.mode,
        )
        if content.failed_images:
            failed_images += content.failed_images
        if content.errors:
            for error in content.errors[:3]:
                logger.debug("[message_card] 内容提示：%s", error)

        header_title = _format_template(s.header_title, header_values)
        if not header_title:
            header_title = "消息卡片"
        header_subtitle = _format_template(s.header_subtitle, header_values)
        if not header_subtitle:
            header_subtitle = f"{theme.date_text}  ·  {theme.time_text}  ·  {theme.timezone}"
        footer_text = _format_template(s.footer_text, footer_values)
        card_options = CardOptions(
            width=s.width,
            radius=s.radius,
            font_size=s.font_size,
            line_height=s.line_height,
            accent=accent,
            header_height=s.header_height,
            footer_height=s.footer_height,
            header_fit=s.header_fit,
            header_position=s.header_position,
            footer_fit=s.footer_fit,
            footer_position=s.footer_position,
            header_show_text=s.header_show_text,
            footer_show_text=s.footer_show_text,
            show_meta=s.show_meta,
            header_enabled=s.header_enabled,
            footer_enabled=s.footer_enabled,
            image_box=s.image_size,
            custom_css=s.custom_css,
        )
        card_data = CardData(
            content_html=content.html,
            mode=theme.mode,
            palette=palette,
            theme_label=theme.label,
            header_image=header_image,
            footer_image=footer_image,
            header_title=header_title,
            header_subtitle=header_subtitle,
            footer_text=footer_text,
            sender=str(sender),
            platform=platform,
            time_text=theme.time_text,
            date_text=theme.date_text,
            timezone=theme.timezone,
            eyebrow=s.eyebrow,
            status_text="ONLINE" if header_image else "CARD",
            avatar_image=avatar_image,
            bot_name=bot_name,
            bot_avatar_image=bot_avatar,
        )
        # Generate Pygments CSS matching the theme.
        html_doc = build_card_html(
            card_data,
            card_options,
            pygments_css=pygments_css(theme.mode),
        )

        render_options = self._render_options(s)
        # Use the timezone validated by resolve_theme; an invalid user value
        # must not make Chromium's new_context() fail.
        render_options.timezone_id = theme.timezone
        self.renderer.update_options(render_options)
        rendered = await self.renderer.render(html_doc, color_scheme=theme.mode)
        suffix = ".jpg" if s.image_format in {"jpeg", "jpg"} else ".png"
        base = f"{int(time.time())}_{uuid.uuid4().hex[:10]}"
        paths: list[Path] = []
        for item in rendered:
            name = f"{base}_{item.index}{suffix}" if item.total > 1 else f"{base}{suffix}"
            path = self.renders_dir / name
            await asyncio.to_thread(path.write_bytes, item.data)
            paths.append(path)
        await self._prune_renders(s.keep_renders)
        self._render_count += 1
        logger.info(
            "[message_card] 已渲染卡片：theme=%s, mode=%s, chars=%s, images=%s, slices=%s, failed_images=%s",
            theme.mode,
            content.mode,
            len(text),
            content.image_count,
            len(paths),
            failed_images,
        )
        return paths

    async def _prune_renders(self, keep: int) -> None:
        def _prune() -> None:
            try:
                files = sorted(
                    (path for path in self.renders_dir.iterdir() if path.is_file()),
                    key=lambda path: path.stat().st_mtime,
                    reverse=True,
                )
                for stale in files[keep:]:
                    try:
                        stale.unlink()
                    except OSError:
                        pass
            except OSError:
                pass

        await asyncio.to_thread(_prune)

    # ------------------------------------------------------------------- hooks

    @filter.on_decorating_result(priority=-100)
    async def on_decorating_result(self, event: AstrMessageEvent):
        s = self._settings()
        if not s.enabled or s.trigger_mode == "command":
            return
        if s.skip_platforms and self._platform_label(event).lower() in s.skip_platforms:
            return
        try:
            platform_name = str(event.get_platform_name() or "").lower()
        except Exception:
            platform_name = ""
        if platform_name in s.skip_platforms:
            return
        if event.get_extra("message_card_rendering"):
            return
        result = event.get_result()
        if result is None or not result.chain:
            return
        content_type = getattr(result, "result_content_type", None)
        if content_type is not None:
            # Streamed platforms send incrementally.  A partial chain must
            # never be turned into a final card; the finish event is skipped by
            # default because some adapters have already flushed the text.
            if content_type == ResultContentType.STREAMING_RESULT:
                return
            if s.skip_streaming and content_type == ResultContentType.STREAMING_FINISH:
                return
        is_llm_result = result.is_llm_result() or (
            content_type == ResultContentType.STREAMING_FINISH
        )
        if s.trigger_mode == "llm" and not is_llm_result:
            return

        render_chain = list(result.chain)
        pending_memes = []
        if s.render_images and s.include_meme_manager_pending:
            candidate = event.get_extra("meme_manager_pending_images")
            if isinstance(candidate, (list, tuple)):
                in_chain = {id(component) for component in render_chain}
                pending_memes = [
                    component
                    for component in candidate
                    if isinstance(component, Image) and id(component) not in in_chain
                ]
                render_chain.extend(pending_memes)

        text, images, image_count = self._chain_parts(
            render_chain,
            render_images=s.render_images,
        )
        plain_texts = [
            component.text or ""
            for component in result.chain
            if isinstance(component, Plain)
        ]
        plain_text = "".join(plain_texts).strip()
        if image_count and not plain_text and not s.render_image_only:
            return
        skip_reason = self._compat_skip_reason(s, event, result, plain_texts)
        if skip_reason:
            logger.info("[message_card] %s", skip_reason)
            return
        text_for_length = re.sub(
            r"!\[[^\]]*\]\(attachment://\d+\)", "", text
        ).strip()
        rich = self._looks_rich(text_for_length) or bool(images)
        if not self._should_render(s, text_for_length, image_count, rich):
            return

        event.set_extra("message_card_rendering", True)
        try:
            paths = await self._render_content(
                event,
                text=text,
                images=images,
                mode_override=s.input_mode,
            )
            if not paths:
                return
            result.chain = [Image.fromFileSystem(str(path)) for path in paths]
            result.use_t2i_ = False
            result.use_markdown_ = False
            if s.preserve_tts:
                tts_record = await self._make_tts_record(event, result, plain_text)
                if tts_record is not None:
                    result.chain.append(tts_record)
            if pending_memes:
                # These were folded into the card; prevent meme_manager's
                # after_message_sent hook from sending them a second time.
                event.set_extra("meme_manager_pending_images", None)
            logger.info(
                "[message_card] 已将消息转换为 %s 张图片%s",
                len(paths),
                f"（并入 {len(pending_memes)} 张待发表情）" if pending_memes else "",
            )
        except (RenderError, AssetError, OSError) as exc:
            self._failed_count += 1
            self._last_error = str(exc)
            logger.error("[message_card] 自动渲染失败，保留原文：%s", exc)
        except Exception as exc:
            self._failed_count += 1
            self._last_error = str(exc)
            logger.exception("[message_card] 自动渲染异常，保留原文：%s", exc)
        finally:
            event.set_extra("message_card_rendering", False)

    # ----------------------------------------------------------------- command

    def _command_args(self, event: AstrMessageEvent) -> str:
        raw = str(getattr(event, "message_str", "") or "").strip()
        if not raw:
            return ""
        # AstrBot normally strips the leading slash and command prefix, but
        # different adapters are inconsistent; handle both shapes.
        raw = re.sub(r"^[/!！、。]\s*", "", raw)
        for name in _COMMAND_NAMES:
            if raw == name:
                return ""
            if raw.startswith(name + " "):
                return raw[len(name):].strip()
            if raw.startswith(name + "\n"):
                return raw[len(name):].strip()
        # If the command prefix was already removed, the remaining text is the
        # argument.  Avoid treating a leading ordinary word as an argument when
        # message_str is the complete original message.
        first, _, rest = raw.partition(" ")
        if first in _COMMAND_NAMES:
            return rest.strip()
        return raw

    def _reply_text(self, event: AstrMessageEvent) -> tuple[str, list[Image]]:
        message = getattr(getattr(event, "message_obj", None), "message", None) or []
        for component in message:
            if not isinstance(component, Reply):
                continue
            text = str(getattr(component, "message_str", "") or "").strip()
            images: list[Image] = []
            reply_chain = getattr(component, "chain", None) or []
            if reply_chain:
                text2, images, _ = self._chain_parts(
                    reply_chain, render_images=True
                )
                if text2:
                    text = text2
            return text, images
        return "", []

    @filter.command("render", alias={"渲染", "消息卡片"})
    async def render_command(self, event: AstrMessageEvent):
        """将 Markdown/HTML/LaTeX 内容或引用消息渲染为图片。"""
        args = self._command_args(event)
        lowered = args.strip().lower()
        if lowered in {"help", "帮助", "?", "？"}:
            yield event.plain_result(HELP_TEXT)
            return
        if lowered in {"status", "状态", "debug", "诊断"}:
            yield event.plain_result(self._status_text(event))
            return

        mode = "auto"
        if lowered.startswith("html "):
            mode = "html"
            args = args.strip()[5:].strip()
        elif lowered.startswith("md "):
            mode = "markdown"
            args = args.strip()[3:].strip()

        text = args
        images: list[Image] = []
        if not text:
            text, images = self._reply_text(event)
        if not text.strip() and not images:
            yield event.plain_result(HELP_TEXT)
            return
        try:
            paths = await self._render_content(
                event,
                text=text,
                images=images,
                mode_override=mode,
                force=True,
            )
            if not paths:
                yield event.plain_result("没有生成图片。")
                return
            yield event.chain_result(
                [Image.fromFileSystem(str(path)) for path in paths]
            )
        except (RenderError, AssetError, OSError) as exc:
            self._failed_count += 1
            self._last_error = str(exc)
            logger.error("[message_card] 手动渲染失败：%s", exc)
            yield event.plain_result(f"消息卡片渲染失败：{exc}")
        except Exception as exc:
            self._failed_count += 1
            self._last_error = str(exc)
            logger.exception("[message_card] 手动渲染异常：%s", exc)
            yield event.plain_result("消息卡片渲染失败，请查看 AstrBot 日志。")

    def _status_text(self, event: AstrMessageEvent) -> str:
        s = self._settings()
        theme = self._theme(s)
        diag = self.renderer.diagnostics()
        return (
            "消息卡片状态\n"
            f"· 当前主题：{theme.label}（{theme.timezone} {theme.now:%Y-%m-%d %H:%M}）\n"
            f"· 判定：{theme.reason}\n"
            f"· 自动触发：{s.trigger_mode}，最小字数 {s.min_chars}，跳过流式 {s.skip_streaming}\n"
            f"· 浏览器：{diag.get('browser_path') or diag.get('detected_browser') or '未检测到'}\n"
            f"· Playwright：{'已安装' if diag.get('playwright_installed') else '未安装'}\n"
            f"· 页眉：{s.header_source}；页脚：{s.footer_source}\n"
            f"· 已渲染：{self._render_count} 次；失败：{self._failed_count} 次\n"
            f"· 上次错误：{self._last_error or '无'}"
        )

    # --------------------------------------------------------------- lifecycle

    async def initialize(self) -> None:
        await self._prune_renders(self._settings().keep_renders)
        logger.info(
            "[message_card] 就绪：昼夜 %s–%s %s，浏览器将在首次渲染时启动。",
            self._settings().day_start,
            self._settings().day_end,
            self._settings().timezone,
        )

    async def terminate(self) -> None:
        """Release Chromium and the shared HTTP session on unload/reload."""

        try:
            await self.renderer.stop()
        except Exception:
            pass
        try:
            await self.assets.close()
        except Exception:
            pass
        logger.info("[message_card] 已卸载。")


__all__ = ["MessageCardPlugin"]
