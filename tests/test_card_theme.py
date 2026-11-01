import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from astrbot_plugin_message_card.card_theme import (
    DARK_PALETTE,
    LIGHT_PALETTE,
    parse_clock,
    resolve_theme,
    safe_color,
)


class ThemeTests(unittest.TestCase):
    def test_day_and_night_boundaries(self):
        tz = ZoneInfo("Asia/Shanghai")
        day = resolve_theme(
            "auto", "Asia/Shanghai", "06:30", "18:30", now=datetime(2026, 11, 1, 12, 0, tzinfo=tz)
        )
        self.assertEqual(day.mode, "light")
        self.assertTrue(day.is_day)
        night = resolve_theme(
            "auto", "Asia/Shanghai", "06:30", "18:30", now=datetime(2026, 11, 1, 22, 0, tzinfo=tz)
        )
        self.assertEqual(night.mode, "dark")
        self.assertFalse(night.is_day)

    def test_start_inclusive_end_exclusive(self):
        tz = ZoneInfo("Asia/Shanghai")
        start = resolve_theme("auto", "Asia/Shanghai", "06:30", "18:30", now=datetime(2026, 11, 1, 6, 30, tzinfo=tz))
        end = resolve_theme("auto", "Asia/Shanghai", "06:30", "18:30", now=datetime(2026, 11, 1, 18, 30, tzinfo=tz))
        self.assertEqual(start.mode, "light")
        self.assertEqual(end.mode, "dark")

    def test_cross_midnight_day_interval(self):
        tz = ZoneInfo("Asia/Shanghai")
        day = resolve_theme("auto", "Asia/Shanghai", "20:00", "06:00", now=datetime(2026, 11, 1, 23, 0, tzinfo=tz))
        night = resolve_theme("auto", "Asia/Shanghai", "20:00", "06:00", now=datetime(2026, 11, 1, 12, 0, tzinfo=tz))
        self.assertEqual(day.mode, "light")
        self.assertEqual(night.mode, "dark")

    def test_force_and_invalid_timezone(self):
        forced = resolve_theme("light", "Not/AZone", "00:00", "00:00")
        self.assertEqual(forced.mode, "light")
        self.assertEqual(forced.timezone, "Asia/Shanghai")
        dark = resolve_theme("dark", "UTC")
        self.assertEqual(dark.mode, "dark")

    def test_parse_and_colors(self):
        self.assertEqual(parse_clock("6:05", "00:00").strftime("%H:%M"), "06:05")
        self.assertEqual(parse_clock("bad", "07:15").strftime("%H:%M"), "07:15")
        self.assertEqual(safe_color("#12AbEf", "#000000"), "#12AbEf")
        self.assertEqual(safe_color("red; background:url(x)", "#000000"), "#000000")
        self.assertIn("dark", DARK_PALETTE.mode)
        self.assertIn("light", LIGHT_PALETTE.mode)


if __name__ == "__main__":
    unittest.main()
