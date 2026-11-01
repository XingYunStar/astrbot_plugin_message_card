"""HTML/CSS template for the rendered message card.

The card is intentionally self-contained: no external fonts, scripts, styles or
images are referenced.  Pygments and theme CSS are inlined, and every dynamic
string is escaped before it reaches this module.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any

try:
    from .card_theme import ThemePalette, safe_color
except ImportError:  # Support direct module imports from self-test scripts.
    from card_theme import ThemePalette, safe_color


def _escape(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def _clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def _clamp_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def _safe_css_text(value: str) -> str:
    """Prevent a config value from terminating the inline <style> element."""

    return re.sub(r"</\s*style", "<\\/style", str(value or ""), flags=re.IGNORECASE)


@dataclass(slots=True)
class CardOptions:
    width: int = 720
    radius: int = 26
    font_size: float = 28.0
    line_height: float = 1.72
    font_family: str = (
        '"Noto Sans SC", "Noto Sans CJK SC", "Source Han Sans SC", '
        '"Microsoft YaHei", "WenQuanYi Zen Hei", "PingFang SC", '
        '"Noto Color Emoji", "Apple Color Emoji", "Segoe UI Emoji", sans-serif'
    )
    code_font_family: str = (
        '"JetBrains Mono", "Cascadia Code", "SFMono-Regular", Consolas, '
        '"WenQuanYi Zen Hei Mono", monospace'
    )
    accent: str = ""
    header_height: int = 280
    footer_height: int = 160
    header_fit: str = "cover"
    header_position: str = "center"
    footer_fit: str = "cover"
    footer_position: str = "center"
    header_show_text: bool = True
    footer_show_text: bool = True
    show_meta: bool = True
    header_enabled: bool = True
    footer_enabled: bool = True
    image_box: int = 380
    custom_css: str = ""


@dataclass(slots=True)
class CardData:
    content_html: str
    mode: str
    palette: ThemePalette
    theme_label: str
    header_image: str | None = None
    footer_image: str | None = None
    header_title: str = "消息卡片"
    header_subtitle: str = ""
    footer_text: str = ""
    sender: str = ""
    platform: str = ""
    time_text: str = ""
    date_text: str = ""
    timezone: str = ""
    eyebrow: str = "MESSAGE CARD"
    status_text: str = ""
    avatar_image: str | None = None
    bot_name: str = "机器人"
    bot_avatar_image: str | None = None


def build_card_html(
    data: CardData,
    options: CardOptions,
    *,
    pygments_css: str = "",
) -> str:
    palette = data.palette
    width = _clamp_int(options.width, 720, 520, 1400)
    radius = _clamp_int(options.radius, 26, 0, 48)
    font_size = _clamp_float(options.font_size, 28.0, 16.0, 36.0)
    line_height = _clamp_float(options.line_height, 1.72, 1.2, 2.4)
    header_height = _clamp_int(options.header_height, 280, 140, 520)
    footer_height = _clamp_int(options.footer_height, 160, 90, 360)
    header_fit = "contain" if str(options.header_fit).lower() == "contain" else "cover"
    footer_fit = "contain" if str(options.footer_fit).lower() == "contain" else "cover"
    header_position = (
        options.header_position
        if re.fullmatch(r"(?:center|top|bottom|left|right|[0-9]{1,3}%[ ]?[0-9]{1,3}%)", str(options.header_position))
        else "center"
    )
    footer_position = (
        options.footer_position
        if re.fullmatch(r"(?:center|top|bottom|left|right|[0-9]{1,3}%[ ]?[0-9]{1,3}%)", str(options.footer_position))
        else "center"
    )
    accent = safe_color(options.accent, palette.accent)
    image_box = _clamp_int(options.image_box, 380, 180, 640)
    custom_css = _safe_css_text(options.custom_css)

    hero_image = ""
    if data.header_image:
        hero_image = (
            f'<img class="hero-img" src="{_escape(data.header_image)}" '
            f'alt="" style="object-fit:{header_fit};object-position:{header_position}">'
        )
    hero_text = ""
    if options.header_show_text:
        hero_text = f"""
      <div class="hero-copy">
        <div class="eyebrow"><span class="eyebrow-line"></span>{_escape(data.eyebrow)}</div>
        <h1 class="hero-title">{_escape(data.header_title)}</h1>
        <div class="hero-subtitle">{_escape(data.header_subtitle)}</div>
      </div>"""

    meta_html = ""
    if options.show_meta:
        initial = (data.sender or "A").strip()[:1].upper()
        if not initial or initial.isspace():
            initial = "A"
        if data.avatar_image:
            avatar_inner = (
                f'<img class="avatar-img" src="{_escape(data.avatar_image)}" '
                'alt="" loading="eager">'
            )
        else:
            avatar_inner = _escape(initial)
        meta_html = f"""
      <div class="meta-row">
        <div class="avatar" aria-hidden="true">{avatar_inner}</div>
        <div class="meta-main">
          <div class="meta-name">{_escape(data.sender or "AstrBot")}</div>
          <div class="meta-sub">{_escape(data.platform or "AstrBot")}{(" · " + _escape(data.time_text)) if data.time_text else ""}</div>
        </div>
        <div class="meta-date">{_escape(data.date_text)}</div>
      </div>"""

    bot_name = data.bot_name or "机器人"
    bot_initial = bot_name.strip()[:1].upper() or "B"
    if data.bot_avatar_image:
        brand_mark = (
            f'<div class="brand-mark brand-avatar">'
            f'<img class="brand-avatar-img" src="{_escape(data.bot_avatar_image)}" alt="" loading="eager">'
            f'</div>'
        )
    else:
        brand_mark = f'<div class="brand-mark">{_escape(bot_initial)}</div>'

    footer_html = ""
    footer_class = "footer"
    if not options.footer_enabled or (not data.footer_image and not options.footer_show_text):
        footer_html = ""
    elif data.footer_image:
        footer_class += " footer-has-image"
        footer_copy = ""
        if options.footer_show_text:
            footer_copy = f"""
        <div class="footer-scrim"></div>
        <div class="footer-inner footer-over-image">
          <div class="footer-brand">
            {brand_mark}
            <div>
              <div class="footer-title">{_escape(data.footer_text or bot_name)}</div>
              <div class="footer-hint">MESSAGE CARD · {_escape(data.date_text)}</div>
            </div>
          </div>
          <div class="footer-side">
            <span class="theme-dot"></span><span>{_escape(data.theme_label)}</span>
            <span class="footer-sep">/</span><span>{_escape(data.status_text or "CARD")}</span>
          </div>
        </div>"""
        footer_html = f"""
      <footer class="{footer_class}" style="height:{footer_height}px">
        <img class="footer-img" src="{_escape(data.footer_image)}" alt=""
             style="object-fit:{footer_fit};object-position:{footer_position}">
        {footer_copy}
      </footer>"""
    elif options.footer_show_text:
        footer_html = f"""
      <footer class="{footer_class}">
        <div class="footer-inner">
          <div class="footer-brand">
            {brand_mark}
            <div>
              <div class="footer-title">{_escape(data.footer_text or bot_name)}</div>
              <div class="footer-hint">MESSAGE CARD · {_escape(data.date_text)}</div>
            </div>
          </div>
          <div class="footer-side">
            <span class="theme-dot"></span><span>{_escape(data.theme_label)}</span>
            <span class="footer-sep">/</span><span>{_escape(data.timezone or "")}</span>
          </div>
        </div>
      </footer>"""

    hero_class = "hero" + (" hero-has-image" if data.header_image else " hero-theme")
    css = f"""
:root {{
  --card-w: {width}px;
  --radius: {radius}px;
  --accent: {accent};
  --accent-soft: {palette.accent_soft};
  --accent-ink: {palette.accent_ink};
  --page: {palette.page};
  --card: {palette.card};
  --card-alt: {palette.card_alt};
  --text: {palette.text};
  --muted: {palette.muted};
  --faint: {palette.faint};
  --line: {palette.line};
  --code-bg: {palette.code_bg};
  --code-border: {palette.code_border};
  --chip: {palette.chip};
  --shadow: {palette.shadow};
  --hero-scrim: {palette.hero_scrim};
  --content-size: {font_size}px;
  --content-leading: {line_height};
  --image-box: {image_box}px;
  --font: {options.font_family};
  --mono: {options.code_font_family};
}}
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; padding: 0; background: transparent; }}
body {{
  font-family: var(--font);
  color: var(--text);
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
  text-rendering: optimizeLegibility;
  font-synthesis: weight;
}}
#stage {{ width: var(--card-w); margin: 0; padding: 0; }}
#card {{
  width: var(--card-w);
  overflow: hidden;
  border-radius: var(--radius);
  background: var(--card);
  box-shadow: var(--shadow);
  position: relative;
}}
.hero {{
  position: relative;
  height: {header_height}px;
  overflow: hidden;
  background: #0c2238;
  isolation: isolate;
}}
.hero-theme {{
  background:
    radial-gradient(120% 160% at 92% 10%, rgba(255,255,255,.20), transparent 53%),
    radial-gradient(90% 130% at 8% 110%, rgba(113,227,255,.36), transparent 58%),
    linear-gradient(128deg, #0b6684 0%, #168fa4 42%, #102943 100%);
}}
.theme-light .hero-theme {{
  background:
    radial-gradient(120% 160% at 92% 8%, rgba(255,255,255,.30), transparent 52%),
    radial-gradient(80% 130% at 10% 110%, rgba(165,239,248,.55), transparent 62%),
    linear-gradient(126deg, #0c708d 0%, #1498aa 45%, #0b3349 100%);
}}
.hero-theme::before {{
  content: "";
  position: absolute;
  inset: 0;
  opacity: .38;
  background-image:
    radial-gradient(circle at 14% 30%, #fff 0 1px, transparent 1.4px),
    radial-gradient(circle at 34% 72%, #fff 0 1px, transparent 1.4px),
    radial-gradient(circle at 58% 22%, #fff 0 1.2px, transparent 1.6px),
    radial-gradient(circle at 78% 54%, #fff 0 .9px, transparent 1.3px),
    radial-gradient(circle at 90% 28%, #fff 0 1.1px, transparent 1.5px);
  background-size: 230px 190px, 290px 240px, 210px 260px, 330px 210px, 260px 300px;
}}
.hero-theme::after {{
  content: "";
  position: absolute;
  left: 9%;
  right: 9%;
  top: 52%;
  height: 1px;
  background: linear-gradient(90deg, transparent, rgba(193,246,255,.55), transparent);
  transform: rotate(-8deg);
}}
.hero-img {{
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  display: block;
  filter: saturate(1.03);
}}
.hero-has-image::after {{
  content: "";
  position: absolute;
  inset: 0;
  background: var(--hero-scrim);
}}
.hero-copy {{
  position: absolute;
  z-index: 2;
  left: 46px;
  right: 46px;
  bottom: 34px;
  color: #fff;
  text-shadow: 0 2px 22px rgba(0,0,0,.82), 0 1px 2px rgba(0,0,0,.55);
}}
.eyebrow {{
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 13px;
  letter-spacing: .18em;
  text-transform: uppercase;
  font-weight: 700;
  color: rgba(255,255,255,.80);
}}
.eyebrow-line {{
  width: 30px;
  height: 1px;
  background: var(--accent);
  box-shadow: 0 0 12px var(--accent);
}}
.hero-title {{
  margin: 10px 0 8px;
  font-size: 42px;
  line-height: 1.12;
  letter-spacing: -.02em;
  font-weight: 750;
}}
.hero-subtitle {{
  font-size: 18px;
  line-height: 1.45;
  letter-spacing: .02em;
  color: rgba(255,255,255,.75);
}}
.hero-badge {{
  position: absolute;
  right: 32px;
  top: 28px;
  z-index: 3;
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 9px 15px;
  border-radius: 999px;
  color: #fff;
  background: rgba(6, 15, 27, .44);
  border: 1px solid rgba(255,255,255,.24);
  backdrop-filter: blur(8px);
  font-size: 12px;
  letter-spacing: .12em;
  text-transform: uppercase;
  font-weight: 650;
}}
.hero-badge .badge-dot {{
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--accent);
  box-shadow: 0 0 12px var(--accent);
}}
.card-body {{ padding: 32px 46px 42px; background: var(--card); }}
.meta-row {{
  display: flex;
  align-items: center;
  gap: 15px;
  padding-bottom: 26px;
  margin-bottom: 28px;
  border-bottom: 1px solid var(--line);
}}
.avatar {{
  flex: 0 0 auto;
  width: 54px;
  height: 54px;
  border-radius: 16px;
  display: grid;
  place-items: center;
  color: var(--accent-ink);
  background:
    radial-gradient(120% 120% at 20% 10%, rgba(255,255,255,.75), transparent 50%),
    linear-gradient(140deg, var(--accent), rgba(255,255,255,.20));
  border: 1px solid rgba(255,255,255,.30);
  font-size: 22px;
  font-weight: 800;
  box-shadow: 0 6px 16px rgba(0,0,0,.12);
}}
.avatar-img {{
  width: 100%;
  height: 100%;
  display: block;
  object-fit: cover;
  border-radius: 15px;
}}
.meta-main {{ min-width: 0; flex: 1 1 auto; }}
.meta-name {{
  font-size: 19px;
  font-weight: 750;
  letter-spacing: .01em;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}}
.meta-sub {{
  margin-top: 3px;
  font-size: 13.5px;
  color: var(--faint);
  letter-spacing: .06em;
  text-transform: uppercase;
}}
.meta-date {{
  flex: 0 0 auto;
  font-family: var(--mono);
  font-size: 13px;
  letter-spacing: .06em;
  color: var(--faint);
  padding: 6px 9px;
  border-radius: 7px;
  border: 1px solid var(--line);
  background: var(--card-alt);
}}
.content {{
  font-size: var(--content-size);
  line-height: var(--content-leading);
  color: var(--text);
  overflow-wrap: anywhere;
  word-break: break-word;
}}
.content > :first-child {{ margin-top: 0 !important; }}
.content > :last-child {{ margin-bottom: 0 !important; }}
.content p {{ margin: 0 0 1.05em; }}
.content h1, .content h2, .content h3, .content h4, .content h5, .content h6 {{
  color: var(--text);
  line-height: 1.32;
  letter-spacing: -.018em;
  font-weight: 760;
  margin: 1.55em 0 .62em;
  scroll-margin-top: 10px;
}}
.content h1 {{ font-size: 1.62em; padding-bottom: .25em; border-bottom: 1px solid var(--line); }}
.content h2 {{ font-size: 1.34em; padding-left: 14px; position: relative; }}
.content h2::before {{
  content: "";
  position: absolute;
  left: 0; top: .18em; bottom: .18em;
  width: 4px; border-radius: 4px;
  background: linear-gradient(180deg, var(--accent), transparent);
}}
.content h3 {{ font-size: 1.14em; color: var(--accent); }}
.content h4 {{ font-size: 1.03em; }}
.content strong, .content b {{ font-weight: 760; color: var(--text); }}
.content em {{ color: var(--muted); }}
.content a {{ color: var(--accent); text-decoration-thickness: 1px; text-underline-offset: 3px; }}
.content hr {{ border: 0; height: 1px; background: var(--line); margin: 1.7em 0; }}
.content blockquote {{
  margin: 1.2em 0;
  padding: 15px 18px;
  border-left: 3px solid var(--accent);
  border-radius: 0 12px 12px 0;
  background: var(--accent-soft);
  color: var(--muted);
}}
.content blockquote p:last-child {{ margin-bottom: 0; }}
.content ul, .content ol {{ margin: .55em 0 1.1em; padding-left: 1.55em; }}
.content li {{ margin: .28em 0; padding-left: .12em; }}
.content li::marker {{ color: var(--accent); font-weight: 700; }}
.content table {{
  width: 100%;
  border-collapse: separate;
  border-spacing: 0;
  margin: 1.2em 0;
  border: 1px solid var(--line);
  border-radius: 12px;
  overflow: hidden;
  font-size: .92em;
}}
.content th {{
  background: var(--card-alt);
  color: var(--text);
  font-weight: 750;
  text-align: left;
}}
.content th, .content td {{
  padding: 10px 13px;
  border-bottom: 1px solid var(--line);
  vertical-align: top;
}}
.content tr:last-child td {{ border-bottom: 0; }}
.content tr:nth-child(even) td {{ background: color-mix(in srgb, var(--card-alt) 58%, transparent); }}
.content code {{
  font-family: var(--mono);
  font-size: .88em;
  padding: .16em .42em;
  border-radius: 5px;
  border: 1px solid var(--code-border);
  background: var(--code-bg);
  color: var(--accent);
}}
.content pre {{
  margin: 1.2em 0;
  padding: 17px 18px;
  overflow-x: hidden;
  white-space: pre-wrap;
  word-break: break-word;
  border-radius: 13px;
  border: 1px solid var(--code-border);
  background: var(--code-bg);
  line-height: 1.56;
}}
.content pre code {{
  display: block;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--text);
  font-size: .84em;
  white-space: pre-wrap;
  word-break: break-word;
}}
.content .codehilite {{ margin: 1.2em 0; border-radius: 13px; overflow: hidden; border: 1px solid var(--code-border); }}
.content .codehilite pre {{ margin: 0; border: 0; border-radius: 0; }}
.content .codehilite .hll {{ background: var(--accent-soft); }}
.content kbd {{
  font-family: var(--mono);
  font-size: .82em;
  padding: 2px 6px;
  border-radius: 5px;
  border: 1px solid var(--line);
  border-bottom-width: 2px;
  background: var(--card-alt);
}}
.content mark {{ background: var(--accent-soft); color: var(--text); padding: 0 .2em; border-radius: 3px; }}
.content figure {{ margin: 1.2em 0; }}
.content .message-image {{ display: block; margin: 1.2em 0; }}
.content .message-image img {{ margin: 0 auto; }}
.content img {{
  display: block;
  max-width: var(--image-box);
  max-height: var(--image-box);
  width: auto;
  height: auto;
  margin: 1.25em auto;
  border-radius: 13px;
  border: 1px solid var(--line);
  background: var(--card-alt);
  object-fit: contain;
  padding: 0;
}}
.content .image-fallback {{
  display: inline-block;
  padding: .15em .55em;
  border-radius: 5px;
  border: 1px dashed var(--line);
  color: var(--faint);
  font-size: .86em;
}}
.content .math-inline {{ display: inline; white-space: nowrap; vertical-align: -0.08em; }}
.content .math-block {{
  margin: 1.15em 0;
  padding: .4em 0;
  overflow-x: auto;
  text-align: center;
}}
.content math {{ font-size: 1.02em; }}
.content math[display="block"] {{ margin: .2em auto; }}
.content .footnote, .content .admonition {{
  font-size: .92em;
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 14px 16px;
  background: var(--card-alt);
}}
.content .admonition-title {{ font-weight: 750; margin-bottom: .4em; color: var(--accent); }}
.content details {{ border: 1px solid var(--line); border-radius: 10px; padding: 10px 14px; margin: 1em 0; }}
.content summary {{ cursor: default; font-weight: 700; }}
.content .toc {{ border: 1px solid var(--line); border-radius: 12px; padding: 14px 18px; background: var(--card-alt); }}
.footer {{
  position: relative;
  min-height: {footer_height}px;
  background: var(--card-alt);
  border-top: 1px solid var(--line);
  overflow: hidden;
}}
.footer-inner {{
  position: relative;
  z-index: 2;
  min-height: {footer_height}px;
  padding: 26px 46px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 18px;
}}
.footer-brand {{ display: flex; align-items: center; gap: 12px; min-width: 0; }}
.brand-avatar {{ padding: 0; overflow: hidden; }}
.brand-avatar-img {{
  width: 100%;
  height: 100%;
  display: block;
  object-fit: cover;
  border-radius: 10px;
}}
.brand-mark {{
  flex: 0 0 auto;
  width: 42px;
  height: 42px;
  display: grid;
  place-items: center;
  border-radius: 10px;
  color: var(--accent-ink);
  background: linear-gradient(145deg, var(--accent), rgba(255,255,255,.35));
  font-family: var(--mono);
  font-size: 13px;
  font-weight: 800;
  letter-spacing: -.04em;
  box-shadow: 0 7px 18px rgba(0,0,0,.14);
}}
.footer-title {{
  max-width: 470px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text);
  font-size: 18px;
  font-weight: 700;
}}
.footer-hint {{
  margin-top: 4px;
  color: var(--faint);
  font-size: 11px;
  letter-spacing: .13em;
  font-family: var(--mono);
}}
.footer-side {{
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 0 0 auto;
  color: var(--faint);
  font-family: var(--mono);
  font-size: 12px;
  letter-spacing: .06em;
  text-transform: uppercase;
}}
.theme-dot {{ width: 6px; height: 6px; border-radius: 50%; background: var(--accent); box-shadow: 0 0 9px var(--accent); }}
.footer-sep {{ opacity: .45; }}
.footer-has-image {{ background: #0b1726; border-top-color: rgba(255,255,255,.12); }}
.footer-img {{ position: absolute; inset: 0; width: 100%; height: 100%; display: block; }}
.footer-scrim {{ position: absolute; inset: 0; background: linear-gradient(180deg, rgba(5,13,24,.12), rgba(5,13,24,.82)); }}
.footer-over-image .footer-title {{ color: #fff; }}
.footer-over-image .footer-side {{ color: rgba(255,255,255,.72); }}
.footer-over-image .footer-hint {{ color: rgba(255,255,255,.55); }}
.footer-over-image .brand-mark {{ color: #04212c; }}
{pygments_css}
{custom_css}
"""
    css = _safe_css_text(css)

    badge = ""
    if options.header_show_text:
        badge = (
            '<div class="hero-badge"><span class="badge-dot"></span>'
            f"<span>{'NIGHT' if data.mode == 'dark' else 'DAY'}</span></div>"
        )
    header_html = ""
    if options.header_enabled:
        header_html = f"""
      <header class="{hero_class}">
        {hero_image}
        {badge}
        {hero_text}
      </header>"""

    return f"""<!DOCTYPE html>
<html lang="zh-CN" data-theme="{_escape(data.mode)}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Message Card</title>
<style>{css}</style>
</head>
<body class="theme-{_escape(data.mode)}">
  <div id="stage">
    <article id="card" class="{('is-dark' if data.mode == 'dark' else 'is-light')}">{header_html}
      <main class="card-body">
        {meta_html}
        <section class="content">{data.content_html}</section>
      </main>
      {footer_html}
    </article>
  </div>
</body>
</html>"""


__all__ = ["CardData", "CardOptions", "build_card_html"]
