"""Image acquisition, validation and conversion for message cards.

All images that reach Chromium are raster data URIs.  Remote content images are
subject to an SSRF guard and a size/pixel limit; header/footer images come from
plugin configuration and are allowed to point at a private host such as a local
Firefly CG API.  The ASCII-only JSON helper also supports common wrapper shapes
returned by image APIs.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import io
import ipaddress
import json
import os
import re
import socket
import struct
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
from astrbot.api import logger
from PIL import Image, UnidentifiedImageError

_DATA_URI = re.compile(
    r"^data:(image/(?:png|jpe?g|gif|webp|bmp));base64,(.*)$",
    re.IGNORECASE | re.DOTALL,
)
_HTTP_URL = re.compile(r"^https?://", re.IGNORECASE)

class AssetError(Exception):
    """Raised when an image cannot be safely read or decoded."""


@dataclass(slots=True)
class AssetConfig:
    allow_remote_images: bool = True
    allow_private_network_images: bool = False
    allow_local_images: bool = False
    fetch_timeout: float = 12.0
    max_image_mb: float = 10.0
    max_pixels: int = 40_000_000
    max_side: int = 2200
    header_max_side: int = 2400
    message_max_side: int = 1800
    api_cache_seconds: int = 45


def _data_uri(mime: str, data: bytes) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _normalize_image(
    data: bytes,
    cfg: AssetConfig,
    *,
    max_side: int | None = None,
    flatten: bool = False,
    background: tuple[int, int, int] = (11, 23, 38),
) -> str:
    """Validate a raster image and return a compact data URI.

    Re-encoding strips metadata, neutralises malformed or polyglot payloads, and
    keeps the generated HTML small enough for chat adapters.
    """

    if len(data) > int(cfg.max_image_mb * 1024 * 1024):
        raise AssetError("图片体积超过限制")
    try:
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            if width <= 0 or height <= 0:
                raise AssetError("图片尺寸无效")
            if width * height > cfg.max_pixels:
                raise AssetError("图片像素数超过限制")
            image.seek(0)
            has_alpha = image.mode in {"RGBA", "LA", "PA"} or (
                image.mode == "P" and "transparency" in image.info
            )
            if flatten:
                base = Image.new("RGB", image.size, background)
                source = image.convert("RGBA")
                base.paste(source, mask=source.getchannel("A"))
                frame = base
                has_alpha = False
            else:
                frame = image.convert("RGBA" if has_alpha else "RGB")
            limit = max_side or cfg.max_side
            if max(frame.size) > limit:
                frame.thumbnail((limit, limit), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            if has_alpha:
                frame.save(output, format="PNG", optimize=True)
                mime = "image/png"
            else:
                frame.save(output, format="JPEG", quality=90, optimize=True)
                mime = "image/jpeg"
            return _data_uri(mime, output.getvalue())
    except AssetError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise AssetError(f"无法识别图片：{exc}") from exc


def _load_data_uri(
    src: str,
    cfg: AssetConfig,
    *,
    max_side: int | None = None,
    flatten: bool = False,
) -> str:
    match = _DATA_URI.match(src.strip())
    if not match:
        raise AssetError("仅支持 PNG/JPEG/GIF/WebP/BMP 的 data URI")
    try:
        data = base64.b64decode(re.sub(r"\s+", "", match.group(2)), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AssetError("data URI 不是有效的 base64") from exc
    return _normalize_image(data, cfg, max_side=max_side, flatten=flatten)


def _load_base64(
    src: str,
    cfg: AssetConfig,
    *,
    max_side: int | None = None,
    flatten: bool = False,
) -> str:
    payload = src.split("base64://", 1)[1]
    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AssetError("base64 图片无效") from exc
    return _normalize_image(data, cfg, max_side=max_side, flatten=flatten)


def _file_source_to_path(src: str) -> Path:
    raw = src.strip()
    if raw.startswith("file://"):
        parsed = urllib.parse.urlparse(raw)
        path = urllib.parse.unquote(parsed.path)
        if os.name == "nt" and path.startswith("/") and len(path) > 2 and path[2] == ":":
            path = path[1:]
        return Path(path)
    return Path(raw).expanduser()


def _looks_like_json(data: bytes) -> bool:
    head = data.lstrip(b"\xef\xbb\xbf \t\r\n")[:1]
    return head in {b"{", b"["}


def _json_get(payload: Any, path: str) -> Any:
    if not path:
        return None
    current = payload
    for part in re.split(r"\.|\[|\]", path.strip(".[]")):
        if not part:
            continue
        if isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def _find_image_url(
    payload: Any,
    depth: int = 0,
    *,
    allow_any_http: bool = False,
) -> str | None:
    if depth > 8:
        return None
    if isinstance(payload, str):
        value = payload.strip()
        if value.startswith(("data:image/", "base64://")):
            return value
        if allow_any_http and value.startswith(("/", "./", "../")):
            return value
        if _HTTP_URL.match(value) and (
            allow_any_http
            or re.search(r"\.(?:png|jpe?g|gif|webp|bmp)(?:[?#]|$)", value, re.IGNORECASE)
        ):
            return value
        return None
    if isinstance(payload, list):
        for item in payload:
            found = _find_image_url(item, depth + 1, allow_any_http=allow_any_http)
            if found:
                return found
        return None
    if isinstance(payload, dict):
        # Values under image-shaped keys are valid even without a file suffix.
        for key in ("url", "image", "image_url", "img", "src", "data", "result", "images"):
            if key in payload:
                found = _find_image_url(payload[key], depth + 1, allow_any_http=True)
                if found:
                    return found
        for value in payload.values():
            found = _find_image_url(value, depth + 1)
            if found:
                return found
    return None


def _default_gateway() -> str | None:
    """Best-effort Docker bridge gateway discovery on Linux."""

    try:
        for line in Path("/proc/net/route").read_text(encoding="utf-8").splitlines()[1:]:
            fields = line.split()
            if len(fields) < 3:
                continue
            destination, gateway = fields[1], fields[2]
            if destination != "00000000" or gateway == "00000000":
                continue
            return socket.inet_ntoa(struct.pack("<L", int(gateway, 16)))
    except (OSError, ValueError):
        return None
    return None


class AssetResolver:
    """Fetch and normalize images for cards and message chains."""

    def __init__(self, cfg: AssetConfig) -> None:
        self.cfg = cfg
        self._session: aiohttp.ClientSession | None = None
        self._cache: dict[str, tuple[float, str]] = {}
        self._failure_cache: dict[str, float] = {}
        self._warned: set[str] = set()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(
                total=max(2.0, self.cfg.fetch_timeout),
                connect=min(6.0, max(2.0, self.cfg.fetch_timeout)),
            )
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                headers={"User-Agent": "AstrBot-MessageCard/1.0"},
                trust_env=True,
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _assert_public_url(self, url: str) -> None:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise AssetError("仅允许 http/https 图片地址")
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError as exc:
            raise AssetError("图片地址端口无效") from exc
        try:
            infos = await asyncio.to_thread(
                socket.getaddrinfo,
                parsed.hostname,
                port,
                type=socket.SOCK_STREAM,
            )
        except OSError as exc:
            raise AssetError(f"无法解析图片域名：{exc}") from exc
        for info in infos:
            address = info[4][0]
            try:
                ip = ipaddress.ip_address(address)
            except ValueError:
                continue
            if not ip.is_global:
                raise AssetError("出于安全原因，正文图片不能访问内网/本机地址")

    async def _read_limited(self, response: aiohttp.ClientResponse) -> bytes:
        limit = int(self.cfg.max_image_mb * 1024 * 1024)
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.content.iter_chunked(64 * 1024):
            total += len(chunk)
            if total > limit:
                raise AssetError("远程图片体积超过限制")
            chunks.append(chunk)
        return b"".join(chunks)

    async def _fetch_raw(
        self,
        url: str,
        *,
        trusted: bool,
        allow_private: bool,
        max_redirects: int = 3,
        request_timeout: float | None = None,
    ) -> tuple[bytes, str, str]:
        if not _HTTP_URL.match(url):
            raise AssetError("不是 http/https 地址")
        current = url
        session = await self._get_session()
        for redirect in range(max_redirects + 1):
            if not _HTTP_URL.match(current):
                raise AssetError("图片重定向到了非 http/https 地址")
            if not trusted and not allow_private:
                await self._assert_public_url(current)
            try:
                request_kwargs = {"allow_redirects": False}
                if request_timeout:
                    request_kwargs["timeout"] = aiohttp.ClientTimeout(
                        total=max(2.0, request_timeout),
                        connect=min(4.0, max(1.0, request_timeout)),
                    )
                async with session.get(current, **request_kwargs) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        if redirect >= max_redirects:
                            raise AssetError("图片地址重定向过多")
                        location = response.headers.get("Location")
                        if not location:
                            raise AssetError("图片地址重定向缺少 Location")
                        current = urllib.parse.urljoin(current, location)
                        continue
                    if response.status != 200:
                        raise AssetError(f"图片接口返回 HTTP {response.status}")
                    data = await self._read_limited(response)
                    content_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
                    return data, content_type, current
            except asyncio.TimeoutError as exc:
                raise AssetError("图片请求超时") from exc
            except aiohttp.ClientError as exc:
                raise AssetError(f"图片请求失败：{exc}") from exc
        raise AssetError("图片请求失败")

    def _candidate_urls(self, url: str, *, allow_host_fallback: bool) -> list[str]:
        candidates = [url]
        if not allow_host_fallback:
            return candidates
        parsed = urllib.parse.urlparse(url)
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            return candidates
        replacements = ["host.docker.internal"]
        gateway = _default_gateway()
        if gateway:
            replacements.append(gateway)
        replacements.append("172.17.0.1")
        for host in replacements:
            replacement = parsed._replace(netloc=f"{host}:{parsed.port}" if parsed.port else host)
            candidate = urllib.parse.urlunparse(replacement)
            if candidate not in candidates:
                candidates.append(candidate)
        return candidates

    async def fetch_trusted_api(
        self,
        url: str,
        *,
        json_path: str = "",
        allow_host_fallback: bool = True,
        cache_seconds: int | None = None,
        kind: str = "api",
        max_side: int | None = None,
    ) -> str | None:
        """Fetch an admin-configured image API (may live on a private host)."""

        if not url.strip():
            return None
        key = f"{kind}|{url}|{json_path}"
        ttl = self.cfg.api_cache_seconds if cache_seconds is None else cache_seconds
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached and cached[0] > now:
            return cached[1]
        failed_until = self._failure_cache.get(key, 0.0)
        if failed_until > now:
            return None

        last_error: Exception | None = None
        for candidate in self._candidate_urls(url, allow_host_fallback=allow_host_fallback):
            try:
                data, _content_type, final_url = await self._fetch_raw(
                    candidate,
                    trusted=True,
                    allow_private=True,
                    request_timeout=min(6.0, max(3.0, self.cfg.fetch_timeout)),
                )
                if _looks_like_json(data):
                    try:
                        payload = json.loads(data.decode("utf-8", errors="replace"))
                    except json.JSONDecodeError as exc:
                        raise AssetError(f"图片接口返回了无效 JSON：{exc}") from exc
                    found = _json_get(payload, json_path) if json_path else None
                    if not isinstance(found, str):
                        found = _find_image_url(payload)
                    if not found:
                        raise AssetError("JSON 中没有找到图片地址")
                    if found.startswith("base64://"):
                        data_uri = _load_base64(found, self.cfg, max_side=(max_side or self.cfg.header_max_side), flatten=True)
                    elif found.startswith("data:image/"):
                        data_uri = _load_data_uri(found, self.cfg, max_side=(max_side or self.cfg.header_max_side), flatten=True)
                    else:
                        image_url = urllib.parse.urljoin(final_url, found)
                        raw, _, _ = await self._fetch_raw(
                            image_url,
                            trusted=True,
                            allow_private=True,
                        )
                        data_uri = _normalize_image(
                            raw,
                            self.cfg,
                            max_side=(max_side or self.cfg.header_max_side),
                            flatten=True,
                        )
                else:
                    plain = data.strip()
                    plain_text = plain.decode("utf-8", errors="ignore").strip()
                    if plain_text.startswith(("http://", "https://")):
                        image_url = urllib.parse.urljoin(final_url, plain_text)
                        raw, _, _ = await self._fetch_raw(
                            image_url,
                            trusted=True,
                            allow_private=True,
                        )
                        data_uri = _normalize_image(
                            raw,
                            self.cfg,
                            max_side=(max_side or self.cfg.header_max_side),
                            flatten=True,
                        )
                    else:
                        data_uri = _normalize_image(
                            data,
                            self.cfg,
                            max_side=(max_side or self.cfg.header_max_side),
                            flatten=True,
                        )
                self._cache[key] = (now + max(0, ttl), data_uri)
                if candidate != url:
                    logger.info("[message_card] 图片 API 已通过容器回退地址访问：%s", candidate)
                return data_uri
            except (AssetError, OSError, ValueError) as exc:
                last_error = exc
                continue
        self._failure_cache[key] = now + 90.0
        if url not in self._warned:
            logger.warning("[message_card] 图片接口不可用，已回退到主题装饰：%s（%s）", url, last_error)
            self._warned.add(url)
        return None

    async def resolve_configured(
        self,
        source: str,
        *,
        path: str = "",
        url: str = "",
        api_url: str = "",
        json_path: str = "",
        allow_host_fallback: bool = True,
        cache_seconds: int | None = None,
        base_dir: Path | None = None,
        data_dir: Path | None = None,
        max_side: int | None = None,
    ) -> str | None:
        kind = str(source or "theme").strip().lower()
        if kind in {"theme", "none", "off", ""}:
            return None
        if kind == "api":
            if not api_url.strip():
                return None
            data_uri = await self.fetch_trusted_api(
                api_url.strip(),
                json_path=json_path,
                allow_host_fallback=allow_host_fallback,
                cache_seconds=cache_seconds,
                kind="footer" if max_side and max_side <= 1800 else "header",
                max_side=max_side,
            )
            return data_uri
        if kind == "url":
            if not url.strip():
                return None
            key = f"url|{url}"
            now = time.monotonic()
            cached = self._cache.get(key)
            if cached and cached[0] > now:
                return cached[1]
            raw, _, _ = await self._fetch_raw(url.strip(), trusted=True, allow_private=True)
            data_uri = _normalize_image(raw, self.cfg, max_side=max_side, flatten=True)
            ttl = self.cfg.api_cache_seconds if cache_seconds is None else cache_seconds
            self._cache[key] = (now + max(0, ttl), data_uri)
            return data_uri
        if kind == "local":
            if not path.strip():
                return None
            local = Path(path.strip()).expanduser()
            if not local.is_absolute():
                candidates = []
                if base_dir:
                    candidates.append(base_dir / local)
                if data_dir:
                    candidates.append(data_dir / local)
                local = next((item for item in candidates if item.exists()), candidates[0] if candidates else local)
            if not local.is_file():
                raise AssetError(f"本地图片不存在：{local}")
            key = f"local|{local}|{local.stat().st_mtime_ns}"
            cached = self._cache.get(key)
            if cached and cached[0] > time.monotonic():
                return cached[1]
            raw = await asyncio.to_thread(local.read_bytes)
            data_uri = await asyncio.to_thread(
                _normalize_image,
                raw,
                self.cfg,
                max_side=max_side,
                flatten=True,
            )
            self._cache[key] = (time.monotonic() + 3600, data_uri)
            return data_uri
        raise AssetError(f"未知图片源：{kind}")

    async def fetch_content_image(
        self, src: str, trusted: bool = False, *, max_side: int | None = None
    ) -> str | None:
        """Resolve an image referenced by message content.

        Returns ``None`` on any failure; callers render a visible fallback
        placeholder instead of failing the whole card.
        """

        value = (src or "").strip()
        if not value:
            return None
        limit = max_side or self.cfg.message_max_side
        try:
            if value.startswith("data:"):
                return await asyncio.to_thread(_load_data_uri, value, self.cfg, max_side=limit)
            if value.startswith("base64://"):
                return await asyncio.to_thread(_load_base64, value, self.cfg, max_side=limit)
            if value.startswith(("http://", "https://")):
                if not self.cfg.allow_remote_images:
                    return None
                raw, _, _ = await self._fetch_raw(
                    value,
                    trusted=trusted,
                    allow_private=trusted or self.cfg.allow_private_network_images,
                )
                return await asyncio.to_thread(
                    _normalize_image, raw, self.cfg, max_side=limit
                )
            if value.startswith("file://") or (
                self.cfg.allow_local_images and Path(value).is_file()
            ):
                if not (trusted or self.cfg.allow_local_images):
                    return None
                local = _file_source_to_path(value)
                if not local.is_file():
                    return None
                raw = await asyncio.to_thread(local.read_bytes)
                return await asyncio.to_thread(
                    _normalize_image, raw, self.cfg, max_side=limit
                )
            if trusted:
                local = Path(value).expanduser()
                if local.is_file():
                    raw = await asyncio.to_thread(local.read_bytes)
                    return await asyncio.to_thread(
                        _normalize_image, raw, self.cfg, max_side=limit
                    )
        except (AssetError, OSError, ValueError) as exc:
            logger.debug("[message_card] 图片读取失败 %s: %s", value[:120], exc)
        return None

    async def component_to_data_uri(self, component: Any) -> str | None:
        """Convert an AstrBot ``Image`` component to a data URI."""

        if component is None:
            return None
        source = (
            getattr(component, "url", None)
            or getattr(component, "file", None)
            or getattr(component, "path", None)
            or ""
        )
        if not source:
            return None
        if isinstance(source, str) and source.startswith("file://"):
            local = _file_source_to_path(source)
            if not local.is_file():
                alt = getattr(component, "path", None)
                if alt and Path(alt).is_file():
                    local = Path(alt)
            try:
                raw = await asyncio.to_thread(local.read_bytes)
                return await asyncio.to_thread(
                    _normalize_image, raw, self.cfg, max_side=self.cfg.message_max_side
                )
            except (OSError, AssetError) as exc:
                logger.debug("[message_card] 消息图片读取失败：%s", exc)
                return None
        if isinstance(source, str) and (
            source.startswith(("http://", "https://", "data:", "base64://"))
        ):
            return await self.fetch_content_image(
                source, trusted=True, max_side=self.cfg.message_max_side
            )
        path_value = getattr(component, "path", None)
        if path_value:
            try:
                local = Path(path_value)
                if local.is_file():
                    raw = await asyncio.to_thread(local.read_bytes)
                    return await asyncio.to_thread(
                        _normalize_image, raw, self.cfg, max_side=self.cfg.message_max_side
                    )
            except (OSError, AssetError):
                pass
        return None


# Keep mimetypes imported for third-party callers that may want suffix checks.
__all__ = ["AssetConfig", "AssetError", "AssetResolver"]
