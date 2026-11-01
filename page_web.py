"""Plugin Page backend for the message-card settings UI.

The page is served by AstrBot's plugin page bridge.  AstrBot 4.25 ships Quart;
newer versions also expose ``astrbot.api.web``.  This module uses Quart's
``jsonify``/``request`` because it works on both versions.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import TYPE_CHECKING, Any

try:
    from quart import jsonify, request
except Exception:  # pragma: no cover - only outside AstrBot
    jsonify = None  # type: ignore[assignment]
    request = None  # type: ignore[assignment]

from astrbot.api import logger

if TYPE_CHECKING:
    from .main import MessageCardPlugin

PLUGIN_NAME = "astrbot_plugin_message_card"

DEFAULT_PREVIEW = """# 消息卡片预览

这是 **Markdown** 预览：标题、列表、表格和 LaTeX 都会进入同一张卡片。

- 字号更大，手机端直接阅读
- 正文图片统一缩放到固定长边
- 页脚显示机器人昵称和头像

| 能力 | 状态 |
| --- | --- |
| HTML / Markdown | 支持 |
| LaTeX / MathML | 支持 |
| 图片 | 支持 |

$$\\int_0^\\infty e^{-x^2}\\,dx=\\frac{\\sqrt{\\pi}}{2}$$
"""

# path -> (kind, minimum, maximum)
_INT_FIELDS: dict[str, tuple[int, int]] = {
    "appearance.width": (520, 1400),
    "appearance.radius": (0, 48),
    "appearance.font_size": (16, 36),
    "trigger.min_chars": (0, 10000),
    "content.max_remote_images": (0, 30),
    "header.height": (140, 520),
    "footer.height": (90, 360),
    "content.image_size": (180, 640),
    "render.max_slice_height": (1200, 15000),
    "render.keep_renders": (10, 2000),
    "render.quality": (40, 100),
    "render.max_concurrent": (1, 6),
}
_FLOAT_FIELDS: dict[str, tuple[float, float]] = {
    "appearance.line_height": (1.2, 2.4),
    "render.device_scale_factor": (1.0, 3.0),
    "render.timeout": (8.0, 180.0),
}
_BOOL_FIELDS = {
    "enabled",
    "trigger.render_image_only",
    "trigger.skip_streaming",
    "trigger.render_images",
    "trigger.include_meme_manager_pending",
    "appearance.show_meta",
    "header.enabled",
    "header.host_fallback",
    "header.show_text",
    "footer.enabled",
    "footer.host_fallback",
    "footer.show_text",
    "content.allow_inline_style",
    "content.allow_remote_images",
    "content.allow_private_network_images",
    "content.allow_local_images",
    "content.math",
    "compat.preserve_segmented_reply",
    "compat.preserve_tts",
    "compat.preserve_forward_threshold",
}
_STR_FIELDS = {
    "trigger.mode",
    "trigger.skip_platforms",
    "theme.mode",
    "theme.timezone",
    "theme.day_start",
    "theme.day_end",
    "theme.accent_light",
    "theme.accent_dark",
    "appearance.eyebrow",
    "header.source",
    "header.path",
    "header.url",
    "header.api_url",
    "header.api_json_path",
    "header.fit",
    "header.position",
    "header.title",
    "header.subtitle",
    "footer.source",
    "footer.path",
    "footer.url",
    "footer.api_url",
    "footer.api_json_path",
    "footer.fit",
    "footer.position",
    "footer.text",
    "content.input_mode",
    "render.format",
    "render.browser_path",
    "appearance.custom_css",
}
_OPTIONS: dict[str, set[str]] = {
    "trigger.mode": {"llm", "all", "command"},
    "theme.mode": {"auto", "light", "dark"},
    "header.source": {"theme", "local", "url", "api"},
    "footer.source": {"theme", "local", "url", "api"},
    "header.fit": {"cover", "contain"},
    "footer.fit": {"cover", "contain"},
    "content.input_mode": {"auto", "markdown", "html"},
    "render.format": {"png", "jpeg"},
}
_EDITABLE = _BOOL_FIELDS | set(_INT_FIELDS) | set(_FLOAT_FIELDS) | _STR_FIELDS


def _get_path(root: Any, path: str) -> Any:
    current = root
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _set_path(root: dict, path: str, value: Any) -> None:
    parts = path.split(".")
    current = root
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    current[parts[-1]] = value


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _json(payload: dict, status: int = 200):
    if jsonify is None:
        return payload, status
    return jsonify(payload), status


class _PreviewEvent:
    """Minimal event used to render the settings preview without a platform."""

    unified_msg_origin = "webui:message_card:preview"

    def get_extra(self, key: str, default: Any = None) -> Any:
        return default

    def set_extra(self, key: str, value: Any) -> None:
        return None

    def get_sender_name(self) -> str:
        return "星陨"

    def get_sender_id(self) -> str:
        return "10001"

    def get_platform_name(self) -> str:
        return "aiocqhttp"

    def get_self_id(self) -> str:
        return "10002"

    def is_group(self) -> bool:
        return True


class PageWebUI:
    def __init__(self, plugin: "MessageCardPlugin") -> None:
        self.plugin = plugin
        self.context = plugin.context

    def register(self) -> None:
        register = getattr(self.context, "register_web_api", None)
        if not callable(register):
            logger.warning("[message_card] 当前 AstrBot 不支持插件 Web API，跳过 WebUI 页面。")
            return
        routes = [
            ("/settings", self.get_settings, ["GET"], "消息卡片设置"),
            ("/settings/save", self.save_settings, ["POST"], "保存消息卡片设置"),
            ("/preview", self.preview, ["POST"], "渲染消息卡片预览"),
            ("/status", self.status, ["GET"], "消息卡片运行状态"),
        ]
        for path, handler, methods, desc in routes:
            register(
                f"/{PLUGIN_NAME}{path}",
                self._wrap(handler),
                methods,
                desc,
            )
        logger.info("[message_card] WebUI 设置页已注册。")

    def _wrap(self, handler):
        async def wrapped(*args, **kwargs):
            try:
                return await handler(*args, **kwargs)
            except Exception as exc:
                logger.exception("[message_card] WebUI 请求失败")
                return _json({"ok": False, "message": str(exc)}, 500)

        wrapped.__name__ = handler.__name__
        return wrapped

    def _values(self) -> dict[str, Any]:
        return {
            path: _get_path(self.plugin.config, path)
            for path in sorted(_EDITABLE)
        }

    def _status(self) -> dict[str, Any]:
        s = self.plugin._settings()
        theme = self.plugin._theme(s)
        diag = self.plugin.renderer.diagnostics()
        return {
            "theme": theme.mode,
            "theme_label": theme.label,
            "now": theme.datetime_text,
            "timezone": theme.timezone,
            "reason": theme.reason,
            "browser_path": diag.get("detected_browser") or diag.get("browser_path") or "",
            "browser_running": diag.get("browser_running"),
            "playwright_installed": diag.get("playwright_installed"),
            "rendered": self.plugin._render_count,
            "failed": self.plugin._failed_count,
            "last_error": self.plugin._last_error,
        }

    async def get_settings(self):
        return _json({"ok": True, "values": self._values(), "status": self._status()})

    async def status(self):
        return _json({"ok": True, "status": self._status()})

    async def save_settings(self):
        payload = await request.get_json(force=True, silent=True) or {}
        raw_values = payload.get("values")
        if not isinstance(raw_values, dict):
            return _json({"ok": False, "message": "values 必须是对象"}, 400)

        changed: dict[str, Any] = {}
        for path, raw in raw_values.items():
            if path not in _EDITABLE:
                continue
            if path in _BOOL_FIELDS:
                value = _bool(raw)
            elif path in _INT_FIELDS:
                minimum, maximum = _INT_FIELDS[path]
                try:
                    value = max(minimum, min(maximum, int(float(raw))))
                except (TypeError, ValueError):
                    continue
            elif path in _FLOAT_FIELDS:
                minimum, maximum = _FLOAT_FIELDS[path]
                try:
                    value = max(minimum, min(maximum, float(raw)))
                except (TypeError, ValueError):
                    continue
            else:
                value = str(raw or "").strip()
                options = _OPTIONS.get(path)
                if options and value not in options:
                    continue
            changed[path] = value

        if not changed:
            return _json({"ok": False, "message": "没有可保存的字段"}, 400)
        for path, value in changed.items():
            _set_path(self.plugin.config, path, value)
        try:
            save_config = getattr(self.plugin.config, "save_config", None)
            if callable(save_config):
                save_config()
        except Exception as exc:
            logger.exception("[message_card] 保存插件配置失败")
            return _json({"ok": False, "message": f"保存失败：{exc}"}, 500)

        # New header/avatar settings take effect immediately.
        self.plugin._avatar_cache.clear()
        self.plugin._bot_profile_cache.clear()
        return _json(
            {
                "ok": True,
                "message": "已保存",
                "values": self._values(),
                "status": self._status(),
            }
        )

    async def preview(self):
        payload = await request.get_json(force=True, silent=True) or {}
        text = str(payload.get("text") or DEFAULT_PREVIEW).strip()
        if not text:
            return _json({"ok": False, "message": "预览内容为空"}, 400)
        if len(text) > 8000:
            text = text[:8000]
        try:
            paths = await self.plugin._render_content(
                _PreviewEvent(),
                text=text,
                images=[],
                mode_override="auto",
                force=True,
            )
        except Exception as exc:
            logger.exception("[message_card] WebUI 预览渲染失败")
            return _json({"ok": False, "message": f"预览失败：{exc}"}, 500)
        if not paths:
            return _json({"ok": False, "message": "没有生成预览图片"}, 500)
        path = Path(paths[0])
        mime = "image/jpeg" if path.suffix.lower() in {".jpg", ".jpeg"} else "image/png"
        data = path.read_bytes()
        return _json(
            {
                "ok": True,
                "image": f"data:{mime};base64,{base64.b64encode(data).decode()}",
                "message": "预览已生成",
            }
        )


__all__ = ["PageWebUI"]
