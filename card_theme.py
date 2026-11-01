"""Theme decisions and palette values for the message card renderer.

This module deliberately has no AstrBot imports so it can be unit-tested on its
own.  A theme is chosen from the configured day/night interval in a real IANA
time zone; when both ends of the interval are equal the user explicitly forces
day or night depending on the selected mode.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_DAY_START = "06:30"
DEFAULT_DAY_END = "18:30"

_HEX_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
_RGB_COLOR = re.compile(
    r"^rgba?\(\s*\d{1,3}%?\s*[, ]\s*\d{1,3}%?\s*[, ]\s*\d{1,3}%?(?:\s*[,/]\s*(?:0|1|0?\.\d+|\d{1,3}%))?\s*\)$"
)


@dataclass(frozen=True, slots=True)
class ThemePalette:
    """Colors and surfaces used by one card theme."""

    mode: str
    label: str
    page: str
    card: str
    card_alt: str
    text: str
    muted: str
    faint: str
    line: str
    accent: str
    accent_soft: str
    accent_ink: str
    code_bg: str
    code_border: str
    chip: str
    shadow: str
    hero_scrim: str


LIGHT_PALETTE = ThemePalette(
    mode="light",
    label="日间模式",
    page="#edf3f8",
    card="#ffffff",
    card_alt="#f6f9fc",
    text="#17243b",
    muted="#61748c",
    faint="#8495a8",
    line="#e1e9f1",
    accent="#0b7f9e",
    accent_soft="rgba(11, 127, 158, 0.11)",
    accent_ink="#064b60",
    code_bg="#f3f7fa",
    code_border="#e0e9f1",
    chip="rgba(255, 255, 255, 0.78)",
    shadow="0 30px 80px rgba(31, 57, 84, 0.16), 0 2px 8px rgba(31, 57, 84, 0.06)",
    hero_scrim="linear-gradient(180deg, rgba(7, 20, 34, 0.02) 10%, rgba(7, 20, 34, 0.42) 52%, rgba(7, 20, 34, 0.92) 100%)",
)

DARK_PALETTE = ThemePalette(
    mode="dark",
    label="夜间模式",
    page="#070d18",
    card="#0e1828",
    card_alt="#111e31",
    text="#e8f3ff",
    muted="#9cb1c8",
    faint="#7288a0",
    line="#22344a",
    accent="#71e3ff",
    accent_soft="rgba(113, 227, 255, 0.12)",
    accent_ink="#032c38",
    code_bg="#0a1422",
    code_border="#21364d",
    chip="rgba(10, 22, 38, 0.72)",
    shadow="0 34px 90px rgba(0, 0, 0, 0.52), 0 2px 10px rgba(0, 0, 0, 0.28)",
    hero_scrim="linear-gradient(180deg, rgba(3, 9, 18, 0.02) 10%, rgba(3, 9, 18, 0.48) 52%, rgba(3, 9, 18, 0.94) 100%)",
)


@dataclass(frozen=True, slots=True)
class ThemeDecision:
    """Result of resolving the automatic day/night theme."""

    mode: str
    label: str
    timezone: str
    now: datetime
    is_day: bool
    reason: str

    @property
    def date_text(self) -> str:
        return self.now.strftime("%Y-%m-%d")

    @property
    def time_text(self) -> str:
        return self.now.strftime("%H:%M")

    @property
    def datetime_text(self) -> str:
        return self.now.strftime("%Y-%m-%d %H:%M")

    @property
    def weekday_text(self) -> str:
        return "一二三四五六日"[self.now.weekday()]


def parse_clock(value: object, default: str) -> time:
    """Parse ``HH:MM`` or ``HH:MM:SS`` into :class:`datetime.time`.

    Invalid values fall back to ``default`` instead of preventing the plugin
    from rendering.  This mirrors how AstrBot plugins usually treat WebUI
    configuration: a bad value should degrade, not crash the bot.
    """

    raw = str(value or "").strip()
    fallback = str(default or "00:00").strip()
    for candidate in (raw, fallback):
        match = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", candidate)
        if not match:
            continue
        hour, minute, second = match.group(1), match.group(2), match.group(3)
        try:
            h, m, s = int(hour), int(minute), int(second or 0)
            if 0 <= h <= 23 and 0 <= m <= 59 and 0 <= s <= 59:
                return time(h, m, s)
        except ValueError:
            continue
    return time(0, 0)


def normalize_timezone(value: object, *, default: str = DEFAULT_TIMEZONE) -> ZoneInfo:
    """Return a valid :class:`ZoneInfo`, falling back to ``default``/UTC."""

    for candidate in (str(value or "").strip(), default, "UTC"):
        if not candidate:
            continue
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return ZoneInfo("UTC")


def _time_in_interval(current: time, start: time, end: time) -> bool:
    """Return whether ``current`` is in the half-open interval ``[start, end)``.

    A start later than the end describes an interval crossing midnight, which is
    useful for polar day/night schedules or unusual shift work.
    """

    if start == end:
        return True
    if start < end:
        return start <= current < end
    return current >= start or current < end


def resolve_theme(
    mode: object = "auto",
    timezone: object = DEFAULT_TIMEZONE,
    day_start: object = DEFAULT_DAY_START,
    day_end: object = DEFAULT_DAY_END,
    *,
    now: datetime | None = None,
) -> ThemeDecision:
    """Resolve ``light``/``dark`` from the configured schedule.

    Args:
        mode: ``auto``, ``light``, or ``dark``.  Anything else is treated as
            ``auto`` so a typo does not break rendering.
        timezone: IANA time zone name, e.g. ``Asia/Shanghai``.
        day_start: Start of the local daytime interval (inclusive).
        day_end: End of the local daytime interval (exclusive).
        now: Optional timezone-aware datetime used by tests.

    Returns:
        A :class:`ThemeDecision` containing the chosen mode and useful display
        strings for the card footer.
    """

    tz = normalize_timezone(timezone)
    selected = str(mode or "auto").strip().lower()
    if selected not in {"auto", "light", "dark"}:
        selected = "auto"

    current = now or datetime.now(tz)
    if current.tzinfo is None:
        current = current.replace(tzinfo=tz)
    else:
        current = current.astimezone(tz)

    start = parse_clock(day_start, DEFAULT_DAY_START)
    end = parse_clock(day_end, DEFAULT_DAY_END)
    is_day = _time_in_interval(current.timetz().replace(tzinfo=None), start, end)

    if selected == "light":
        theme_mode = "light"
        reason = "配置强制日间主题"
    elif selected == "dark":
        theme_mode = "dark"
        reason = "配置强制夜间主题"
    else:
        theme_mode = "light" if is_day else "dark"
        reason = (
            f"当地时间 {current:%H:%M} 位于日间区间 "
            f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')}"
            if is_day
            else f"当地时间 {current:%H:%M} 位于夜间区间（日间为 "
            f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')}）"
        )

    return ThemeDecision(
        mode=theme_mode,
        label="日间模式" if theme_mode == "light" else "夜间模式",
        timezone=str(getattr(tz, "key", None) or timezone or "UTC"),
        now=current,
        is_day=is_day,
        reason=reason,
    )


def palette_for(mode: str) -> ThemePalette:
    return DARK_PALETTE if str(mode).lower() == "dark" else LIGHT_PALETTE


def safe_color(value: object, default: str) -> str:
    """Accept only simple CSS color literals from configuration.

    This prevents a maliciously edited config from injecting arbitrary CSS while
    still allowing hex and ``rgb()/rgba()`` values.  Named colors are also
    accepted only when they look like a plain CSS identifier.
    """

    raw = str(value or "").strip()
    if _HEX_COLOR.fullmatch(raw) or _RGB_COLOR.fullmatch(raw):
        return raw
    if re.fullmatch(r"[a-zA-Z]{3,24}", raw):
        return raw
    return default


def accent_for(mode: str, configured: object = "") -> str:
    base = palette_for(mode).accent
    return safe_color(configured, base)


__all__ = [
    "DARK_PALETTE",
    "DEFAULT_DAY_END",
    "DEFAULT_DAY_START",
    "DEFAULT_TIMEZONE",
    "LIGHT_PALETTE",
    "ThemeDecision",
    "ThemePalette",
    "accent_for",
    "normalize_timezone",
    "palette_for",
    "parse_clock",
    "resolve_theme",
    "safe_color",
]
