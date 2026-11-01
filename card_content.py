"""Turn Markdown/HTML and message images into sanitized card HTML.

The renderer intentionally supports a useful subset of HTML rather than
arbitrary web pages.  All element content is sanitized with Bleach, scripts and
styles are removed, and embedded images are fetched by the caller and converted
to data URIs before the browser sees the page.  The browser itself is also
locked down by the renderer, so this is defence in depth rather than the only
line of defence.
"""

from __future__ import annotations

import asyncio
import html
import re
import secrets
import urllib.parse
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import bleach
from bleach.css_sanitizer import CSSSanitizer
from markdown import Markdown
from pygments.formatters import HtmlFormatter

try:  # latex2mathml is an optional import only during unit tests of the parser.
    from latex2mathml.converter import convert as latex_to_mathml
except Exception:  # pragma: no cover - exercised only on broken installs
    latex_to_mathml = None


# ---------------------------------------------------------------------------
# Public data types


@dataclass(slots=True)
class ContentOptions:
    """Per-render content options derived from the plugin configuration."""

    input_mode: str = "auto"
    allow_inline_style: bool = True
    allow_remote_images: bool = True
    allow_local_images: bool = False
    max_remote_images: int = 8
    code_theme_light: str = "friendly"
    code_theme_dark: str = "monokai"
    math_enabled: bool = True
    sanitize_html: bool = True


@dataclass(slots=True)
class ContentResult:
    html: str
    mode: str
    math_count: int = 0
    image_count: int = 0
    failed_images: int = 0
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Math extraction / rendering


_MATHML_TAGS = {
    "math",
    "mrow",
    "mi",
    "mn",
    "mo",
    "mtext",
    "mspace",
    "mfrac",
    "msqrt",
    "mroot",
    "mstyle",
    "merror",
    "mpadded",
    "mphantom",
    "mfenced",
    "menclose",
    "msub",
    "msup",
    "msubsup",
    "munder",
    "mover",
    "munderover",
    "mmultiscripts",
    "mprescripts",
    "none",
    "mtable",
    "mtr",
    "mtd",
    "mlabeledtr",
    "maction",
    "semantics",
    "mstack",
    "mlongdiv",
    "msgroup",
    "msrow",
    "mscarries",
    "mscarry",
    "msline",
}


def _safe_math_attribute(tag: str, name: str, value: str) -> bool:
    """Allow ordinary MathML presentation attributes, never active content."""

    lowered = name.strip().lower()
    if lowered.startswith("on") or lowered in {"href", "src", "style"}:
        return False
    return "javascript:" not in str(value).lower()


def _sanitize_mathml(mathml: str) -> str:
    return bleach.clean(
        mathml,
        tags=_MATHML_TAGS,
        attributes=_safe_math_attribute,
        protocols=set(),
        strip=True,
        strip_comments=True,
    )


def _render_math(latex: str, *, display: bool) -> str | None:
    """Render one LaTeX expression to sanitized MathML, or return ``None``."""

    if latex_to_mathml is None or not latex.strip():
        return None
    try:
        mathml = latex_to_mathml(latex, display="block" if display else "inline")
    except Exception:
        return None
    if not mathml:
        return None
    safe = _sanitize_mathml(mathml)
    if not safe.lstrip().startswith("<math"):
        return None
    if display:
        return f'<span class="math-block">{safe}</span>'
    return f'<span class="math-inline">{safe}</span>'


def _looks_like_inline_math(value: str) -> bool:
    """Conservative heuristic that keeps prices and shell variables as text."""

    text = value.strip()
    if not text or len(text) > 500 or "\n" in text:
        return False
    if text != value:
        return False
    if re.fullmatch(r"\d+(?:[.,]\d+)?", text):
        return False
    if re.search(r"\\[A-Za-z]+", text):
        return True
    if len(text) == 1 and re.fullmatch(r"[A-Za-z]", text):
        return True
    if re.search(r"[A-Za-z0-9)}\]]\s*[+\-*/=<>]\s*[A-Za-z0-9({[]", text):
        return True
    if re.search(r"[\^_]", text) and re.search(r"[A-Za-z0-9{}]", text):
        return True
    if re.search(r"[{}]", text) and re.search(r"[A-Za-z\\]", text):
        return True
    return False


class _MathExtractor:
    """Replace math spans with collision-resistant plain-text placeholders.

    Math has to be lifted out before Markdown/HTML processing so underscores,
    asterisks and angle brackets inside formulas are not interpreted as markup.
    Placeholders contain only letters and digits and survive both parsers and
    the HTML sanitizer.
    """

    def __init__(self) -> None:
        self.prefix = f"MC{secrets.token_hex(5).upper()}"
        self.counter = 0
        self._rendered: dict[str, str] = {}
        self.count = 0
        self._errors: list[str] = []

    def extract(self, text: str, *, html_mode: bool = False) -> str:
        protected = _protected_ranges(text, html_mode=html_mode)
        candidates: list[tuple[int, int, str, bool]] = []

        # Delimited display math first.  All patterns are non-overlapping, and
        # overlaps are filtered below so ``$$x$$`` is not also parsed inline.
        for pattern, display in (
            (re.compile(r"(?<!\\)\$\$(.+?)(?<!\\)\$\$", re.DOTALL), True),
            (re.compile(r"(?<!\\)\\\[(.+?)(?<!\\)\\\]", re.DOTALL), True),
            (re.compile(r"(?<!\\)\\\((.+?)(?<!\\)\\\)", re.DOTALL), False),
            (
                re.compile(
                    r"(?<!\\)(?<!\$)\$(?!\$)(?!\s)([^\n$]{1,500}?)(?<!\s)(?<!\\)\$(?!\$)"
                ),
                False,
            ),
        ):
            for match in pattern.finditer(text):
                start, end = match.span()
                if _overlaps(start, end, protected) or _overlaps_any(
                    start, end, candidates
                ):
                    continue
                body = match.group(1)
                if not display and not _looks_like_inline_math(body):
                    continue
                candidates.append((start, end, body, display))

        # A few users paste an environment without dollar delimiters.  Support
        # the common display environments as a convenience.
        env_pattern = re.compile(
            r"(?<!\\)\\begin\{(equation\*?|align\*?|gather\*?|multline\*?|"
            r"cases|matrix|pmatrix|bmatrix|vmatrix|array)\}.*?\\end\{\1\}",
            re.DOTALL,
        )
        for match in env_pattern.finditer(text):
            start, end = match.span()
            if _overlaps(start, end, protected) or _overlaps_any(
                start, end, candidates
            ):
                continue
            candidates.append((start, end, match.group(0), True))

        candidates.sort(key=lambda item: item[0])
        if not candidates:
            return text

        pieces: list[str] = []
        cursor = 0
        for start, end, body, display in candidates:
            if start < cursor:
                continue
            pieces.append(text[cursor:start])
            token = f"{self.prefix}MATH{self.counter:04d}END"
            self.counter += 1
            rendered = _render_math(body, display=display)
            if rendered is None:
                # Keep the original expression visible rather than silently
                # dropping malformed LaTeX.
                literal = "$" + html.escape(body.strip()) + "$" if not display else (
                    "$$" + html.escape(body.strip()) + "$$"
                )
                pieces.append(literal)
                self._errors.append(f"无法渲染公式：{body.strip()[:80]}")
            else:
                self._rendered[token] = rendered
                self.count += 1
                pieces.append(token)
            cursor = end
        pieces.append(text[cursor:])
        return "".join(pieces)

    def restore(self, rendered_html: str) -> str:
        for token, replacement in self._rendered.items():
            # Display math tokens are usually wrapped in a paragraph by
            # Markdown.  Unwrap that paragraph so a block element is valid HTML.
            rendered_html = re.sub(
                rf"<p>\s*{re.escape(token)}\s*</p>",
                replacement,
                rendered_html,
                flags=re.IGNORECASE,
            )
            rendered_html = rendered_html.replace(token, replacement)
        return rendered_html

    @property
    def errors(self) -> list[str]:
        return list(self._errors)


def _ranges_from_regex(text: str, pattern: re.Pattern[str]) -> list[tuple[int, int]]:
    return [match.span() for match in pattern.finditer(text)]


def _protected_ranges(text: str, *, html_mode: bool) -> list[tuple[int, int]]:
    """Return spans where math must not be extracted (code and raw HTML blocks)."""

    ranges: list[tuple[int, int]] = []

    # Fenced code blocks, including tildes and info strings.
    fence: str | None = None
    fence_start = 0
    for match in re.finditer(r"(?m)^[ \t]{0,3}(`{3,}|~{3,})[^\n]*$", text):
        marker = match.group(1)
        if fence is None:
            fence = marker[0]
            fence_start = match.start()
        elif marker[0] == fence:
            ranges.append((fence_start, match.end()))
            fence = None
    if fence is not None:
        ranges.append((fence_start, len(text)))

    # Inline code outside fenced blocks.
    for match in re.finditer(r"(`+)([^\n]*?)\1", text):
        if not _overlaps(match.start(), match.end(), ranges):
            ranges.append((match.start(), match.end()))

    if html_mode:
        # Do not extract $...$ inside tag attributes such as title="$x$".
        ranges.extend(_ranges_from_regex(text, re.compile(r"</?[A-Za-z][^>]*>")))
        for tag in ("pre", "code", "script", "style", "textarea", "template"):
            for match in re.finditer(
                rf"<{tag}\b[^>]*>.*?</{tag}\s*>", text, re.DOTALL | re.IGNORECASE
            ):
                ranges.append((match.start(), match.end()))

    ranges.sort()
    merged: list[tuple[int, int]] = []
    for start, end in ranges:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _overlaps(start: int, end: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start < range_end and end > range_start for range_start, range_end in ranges)


def _overlaps_any(
    start: int, end: int, candidates: list[tuple[int, int, str, bool]]
) -> bool:
    return any(start < item[1] and end > item[0] for item in candidates)


# ---------------------------------------------------------------------------
# Markdown / HTML conversion


_MARKDOWN_EXTENSIONS = [
    "extra",
    "sane_lists",
    "nl2br",
    "codehilite",
    "admonition",
    "toc",
]


def markdown_to_html(text: str, options: ContentOptions, palette: str) -> str:
    code_theme = (
        options.code_theme_dark if palette == "dark" else options.code_theme_light
    )
    md = Markdown(
        extensions=_MARKDOWN_EXTENSIONS,
        extension_configs={
            "codehilite": {
                "css_class": "codehilite",
                "guess_lang": False,
                "linenums": False,
                "pygments_style": code_theme,
            },
            "toc": {"permalink": False},
        },
        output_format="html",
    )
    return md.convert(text)


def detect_input_mode(text: str, configured: str = "auto") -> str:
    mode = str(configured or "auto").strip().lower()
    if mode in {"markdown", "html"}:
        return mode

    stripped = text.lstrip()
    if re.search(r"<!doctype\s+html|<html\b|<body\b", stripped, re.IGNORECASE):
        return "html"
    # A line starting with a block-level HTML tag is a strong signal.  Inline
    # tags such as <span> or <br> are intentionally left to Markdown so plain
    # prose with a little HTML is not converted into a document fragment.
    if re.search(
        r"(?im)^\s*<(?:div|section|article|main|header|footer|aside|nav|"
        r"table|thead|tbody|tr|td|th|h[1-6]|p|ul|ol|li|blockquote|pre|"
        r"figure|figcaption|details|summary|style)\b",
        stripped,
    ):
        return "html"
    if re.search(r"(?is)<(?:style|table|section|div)\b[^>]*>", stripped):
        return "html"
    return "markdown"


_SCRIPT_LIKE_BLOCK = re.compile(
    r"<(script|style|iframe|object|embed|noscript|template|form|input|button|"
    r"textarea|select|option|link|meta|base)\b[^>]*>.*?</\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
_SELF_CLOSING_SCRIPT_LIKE = re.compile(
    r"<(?:link|meta|base|input|embed)\b[^>]*?/?>",
    re.IGNORECASE,
)


def _extract_body(fragment: str) -> str:
    # Drop whole active/container elements and their contents before Bleach.
    fragment = re.sub(r"<!--.*?-->", "", fragment, flags=re.DOTALL)
    fragment = _SCRIPT_LIKE_BLOCK.sub("", fragment)
    fragment = _SELF_CLOSING_SCRIPT_LIKE.sub("", fragment)
    body = re.search(r"<body\b[^>]*>(.*?)</body\s*>", fragment, re.DOTALL | re.IGNORECASE)
    if body:
        fragment = body.group(1)
    # Remove doctype/head/html wrappers but keep the body contents.
    fragment = re.sub(r"<!doctype[^>]*>", "", fragment, flags=re.IGNORECASE)
    fragment = re.sub(r"</?(?:html|head|body)\b[^>]*>", "", fragment, flags=re.IGNORECASE)
    return fragment


_ALLOWED_TAGS = {
    "a",
    "abbr",
    "article",
    "aside",
    "b",
    "blockquote",
    "br",
    "caption",
    "center",
    "cite",
    "code",
    "col",
    "colgroup",
    "dd",
    "del",
    "details",
    "dfn",
    "div",
    "dl",
    "dt",
    "em",
    "figcaption",
    "figure",
    "footer",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "hr",
    "i",
    "img",
    "ins",
    "kbd",
    "li",
    "main",
    "mark",
    "ol",
    "p",
    "pre",
    "q",
    "s",
    "samp",
    "section",
    "small",
    "span",
    "strong",
    "sub",
    "summary",
    "sup",
    "table",
    "tbody",
    "td",
    "tfoot",
    "th",
    "thead",
    "tr",
    "u",
    "ul",
    "var",
    "wbr",
}

_ALLOWED_ATTRIBUTES = {
    "a": {"href", "title", "target", "rel"},
    "abbr": {"title"},
    "blockquote": {"cite"},
    "col": {"span", "width"},
    "colgroup": {"span"},
    "div": {"class", "style", "align", "title"},
    "img": {"src", "alt", "title", "width", "height", "class"},
    "li": {"value"},
    "ol": {"start", "type", "reversed"},
    "p": {"class", "style", "align", "title"},
    "span": {"class", "style", "title"},
    "table": {"class", "style", "border", "cellpadding", "cellspacing", "width"},
    "td": {
        "class",
        "style",
        "colspan",
        "rowspan",
        "align",
        "valign",
        "width",
        "height",
    },
    "th": {
        "class",
        "style",
        "colspan",
        "rowspan",
        "align",
        "valign",
        "width",
        "height",
        "scope",
    },
    "tr": {"class", "style", "align", "valign"},
    "ul": {"class", "type"},
    "code": {"class"},
    "pre": {"class"},
    "figure": {"class"},
    "figcaption": {"class"},
    "h1": {"class", "id"},
    "h2": {"class", "id"},
    "h3": {"class", "id"},
    "h4": {"class", "id"},
    "h5": {"class", "id"},
    "h6": {"class", "id"},
}

_ALLOWED_PROTOCOLS = {"http", "https", "mailto", "data", "attachment"}

# CSS properties that are safe in a screenshot and cannot trigger a network
# request.  In particular, background/background-image, position, @import and
# url() are deliberately absent.
_ALLOWED_CSS_PROPERTIES = {
    "background-color",
    "border",
    "border-bottom",
    "border-color",
    "border-left",
    "border-right",
    "border-top",
    "border-radius",
    "border-style",
    "border-width",
    "color",
    "display",
    "font-family",
    "font-size",
    "font-style",
    "font-weight",
    "letter-spacing",
    "line-height",
    "list-style-type",
    "margin",
    "margin-bottom",
    "margin-left",
    "margin-right",
    "margin-top",
    "max-width",
    "opacity",
    "overflow-wrap",
    "padding",
    "padding-bottom",
    "padding-left",
    "padding-right",
    "padding-top",
    "text-align",
    "text-decoration",
    "text-indent",
    "vertical-align",
    "white-space",
    "width",
    "word-break",
}

_CSS_SANITIZER = CSSSanitizer(allowed_css_properties=_ALLOWED_CSS_PROPERTIES)


def sanitize_user_html(
    fragment: str,
    *,
    allow_inline_style: bool = True,
    sanitize: bool = True,
) -> str:
    if not sanitize:
        return fragment
    fragment = _extract_body(fragment)
    attributes: dict | set[str] | list[str] | Callable = _ALLOWED_ATTRIBUTES
    if not allow_inline_style:
        attributes = {
            tag: {name for name in names if name != "style"}
            for tag, names in _ALLOWED_ATTRIBUTES.items()
        }
    return bleach.clean(
        fragment,
        tags=_ALLOWED_TAGS,
        attributes=attributes,
        protocols=_ALLOWED_PROTOCOLS,
        css_sanitizer=_CSS_SANITIZER if allow_inline_style else None,
        strip=True,
        strip_comments=True,
    )


# ---------------------------------------------------------------------------
# Image rewriting


_IMG_TAG = re.compile(r"<img\b[^>]*?/?>", re.IGNORECASE)
_ATTR = re.compile(
    r"([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s\"'>]+))"
)
_DATA_IMAGE = re.compile(
    r"^data:image/(png|jpe?g|gif|webp|bmp);base64,([A-Za-z0-9+/=\s]+)$",
    re.IGNORECASE,
)


def _parse_attrs(tag: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for match in _ATTR.finditer(tag):
        name = match.group(1).lower()
        value = match.group(2) or match.group(3) or match.group(4) or ""
        result[name] = html.unescape(value)
    return result


def _clean_img_tag(tag: str, src: str) -> str:
    attrs = _parse_attrs(tag)
    alt = html.escape(attrs.get("alt", ""), quote=True)
    title = html.escape(attrs.get("title", ""), quote=True)
    css_class = html.escape(attrs.get("class", ""), quote=True)
    width = attrs.get("width", "").strip()
    height = attrs.get("height", "").strip()
    width_attr = f' width="{html.escape(width, quote=True)}"' if re.fullmatch(r"\d{1,5}(?:px)?", width) else ""
    height_attr = f' height="{html.escape(height, quote=True)}"' if re.fullmatch(r"\d{1,5}(?:px)?", height) else ""
    return (
        f'<img src="{html.escape(src, quote=True)}" alt="{alt}"'
        + (f' title="{title}"' if title else "")
        + (f' class="{css_class}"' if css_class else "")
        + width_attr
        + height_attr
        + ">"
    )


async def _rewrite_images(
    fragment: str,
    *,
    image_map: dict[str, str],
    fetch_image: Callable[[str, bool], Awaitable[str | None]],
    options: ContentOptions,
) -> tuple[str, int, int, list[str]]:
    tags = _IMG_TAG.findall(fragment)
    if not tags:
        return fragment, 0, 0, []

    errors: list[str] = []
    resolved: dict[str, str | None] = {}
    seen: list[str] = []
    for tag in tags:
        src = _parse_attrs(tag).get("src", "").strip()
        if src and src not in resolved:
            resolved[src] = None
            seen.append(src)

    remote_count = 0
    semaphore = asyncio.Semaphore(max(1, options.max_remote_images))

    async def resolve_one(src: str) -> tuple[str, str | None]:
        nonlocal remote_count
        if src in image_map:
            return src, image_map[src]
        if src.startswith("attachment://"):
            return src, image_map.get(src)
        data_match = _DATA_IMAGE.match(src)
        if data_match:
            # Let the asset layer validate/re-encode the payload.  The `data:`
            # branch is trusted only after this validation.
            return src, await fetch_image(src, False)
        parsed = urllib.parse.urlparse(src)
        if parsed.scheme in {"http", "https"}:
            if not options.allow_remote_images:
                return src, None
            async with semaphore:
                remote_count += 1
                if remote_count > options.max_remote_images:
                    return src, None
                return src, await fetch_image(src, False)
        if src.startswith("data:"):
            return src, None
        if options.allow_local_images and (
            src.startswith("file://") or (parsed.scheme == "" and src)
        ):
            return src, await fetch_image(src, False)
        return src, None

    results = await asyncio.gather(*(resolve_one(src) for src in seen))
    resolved.update(dict(results))
    image_count = 0
    failed = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal image_count, failed
        tag = match.group(0)
        src = _parse_attrs(tag).get("src", "").strip()
        data_uri = resolved.get(src)
        if not data_uri:
            failed += 1
            errors.append(f"图片不可用：{src[:120]}")
            return '<span class="image-fallback">[图片不可用]</span>'
        image_count += 1
        return _clean_img_tag(tag, data_uri)

    fragment = _IMG_TAG.sub(replace, fragment)
    return fragment, image_count, failed, errors


# ---------------------------------------------------------------------------
# Content builder


class ContentBuilder:
    """Build safe HTML for one card."""

    def __init__(self, options: ContentOptions | None = None) -> None:
        self.options = options or ContentOptions()

    async def build(
        self,
        text: str,
        *,
        mode: str = "auto",
        image_map: dict[str, str] | None = None,
        fetch_image: Callable[[str, bool], Awaitable[str | None]] | None = None,
        palette: str = "light",
    ) -> ContentResult:
        image_map = image_map or {}
        if fetch_image is None:
            async def fetch_image(_src: str, _trusted: bool = False) -> None:
                return None

        chosen = detect_input_mode(text, mode)
        math = _MathExtractor() if self.options.math_enabled else None
        prepared = math.extract(text, html_mode=chosen == "html") if math else text

        if chosen == "html":
            rendered = sanitize_user_html(
                prepared,
                allow_inline_style=self.options.allow_inline_style,
                sanitize=self.options.sanitize_html,
            )
        else:
            rendered = markdown_to_html(prepared, self.options, palette)
            rendered = sanitize_user_html(
                rendered,
                allow_inline_style=self.options.allow_inline_style,
                sanitize=self.options.sanitize_html,
            )

        rendered, image_count, failed, image_errors = await _rewrite_images(
            rendered,
            image_map=image_map,
            fetch_image=fetch_image,
            options=self.options,
        )
        math_errors = math.errors if math else []
        if math:
            rendered = math.restore(rendered)

        return ContentResult(
            html=rendered,
            mode=chosen,
            math_count=math.count if math else 0,
            image_count=image_count,
            failed_images=failed,
            errors=[*image_errors, *math_errors],
        )


# ---------------------------------------------------------------------------
# Chunking for unusually long messages


def split_content(text: str, max_chars: int) -> list[str]:
    """Split Markdown text at paragraph boundaries without breaking code fences.

    The splitter is intentionally conservative: if a single paragraph is longer
    than the limit it is emitted intact.  This keeps code blocks and tables from
    being torn apart; the renderer can still screenshot a tall card.
    """

    if max_chars <= 0 or len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    in_fence: str | None = None

    for paragraph in re.split(r"(\n{2,})", text):
        if not paragraph:
            continue
        fence_match = re.match(r"^\s*(`{3,}|~{3,})", paragraph)
        if fence_match:
            marker = fence_match.group(1)[0]
            if in_fence is None:
                in_fence = marker
            elif marker == in_fence:
                in_fence = None
        if current and in_fence is None and current_len + len(paragraph) > max_chars:
            chunks.append("".join(current).strip())
            current = []
            current_len = 0
        current.append(paragraph)
        current_len += len(paragraph)
    if current:
        chunks.append("".join(current).strip())
    return [chunk for chunk in chunks if chunk] or [text]


# ---------------------------------------------------------------------------
# Pygments CSS


def pygments_css(mode: str, light_theme: str = "friendly", dark_theme: str = "monokai") -> str:
    style = dark_theme if mode == "dark" else light_theme
    try:
        return HtmlFormatter(style=style).get_style_defs(".codehilite")
    except Exception:
        return ""


__all__ = [
    "ContentBuilder",
    "ContentOptions",
    "ContentResult",
    "detect_input_mode",
    "markdown_to_html",
    "pygments_css",
    "sanitize_user_html",
    "split_content",
]
