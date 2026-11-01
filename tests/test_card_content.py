import asyncio
import unittest

from astrbot_plugin_message_card.card_content import (
    ContentBuilder,
    ContentOptions,
    detect_input_mode,
    sanitize_user_html,
    split_content,
)


class ContentTests(unittest.TestCase):
    def run_build(self, text, **kwargs):
        async def build():
            return await ContentBuilder(ContentOptions()).build(text, **kwargs)

        return asyncio.run(build())

    def test_markdown_table_code_and_math(self):
        text = """# 标题

| A | B |
| - | - |
| 1 | 2 |

```python
print(1)
```

行内 $E=mc^2$，块级 $$\\int_0^1 x dx$$。
"""
        result = self.run_build(text, palette="light")
        self.assertEqual(result.mode, "markdown")
        self.assertGreaterEqual(result.math_count, 2)
        self.assertIn("<table>", result.html)
        self.assertIn("codehilite", result.html)
        self.assertIn("<math", result.html)
        self.assertNotIn("<script", result.html)

    def test_html_is_sanitized(self):
        raw = """<div style="color:red;position:fixed;background:url(http://evil)">
        <b>ok</b><script>alert(1)</script><iframe src="http://evil"></iframe>
        <img src="data:image/svg+xml;base64,PHN2Zz48c2NyaXB0PjE8L3NjcmlwdD48L3N2Zz4=">
        </div>"""
        result = self.run_build(raw, mode="html", palette="light")
        self.assertNotIn("<script", result.html)
        self.assertNotIn("<iframe", result.html)
        self.assertNotIn("position", result.html)
        self.assertNotIn("url(", result.html)
        self.assertIn(">ok<", result.html)

    def test_math_text_cannot_inject_script(self):
        result = self.run_build(
            r"公式 $\text{<script>alert(2)</script>}$ 结束", palette="dark"
        )
        self.assertIn("<math", result.html)
        self.assertNotIn("<script", result.html)

    def test_math_inside_html_attribute_is_not_rewritten(self):
        result = self.run_build(
            '<div title="price $x$">价格 $5.00</div>', mode="html", palette="light"
        )
        self.assertIn('title="price $x$"', result.html)
        self.assertIn("$5.00", result.html)

    def test_detect_input_mode(self):
        self.assertEqual(detect_input_mode("<!DOCTYPE html><html></html>"), "html")
        self.assertEqual(detect_input_mode("<div>hi</div>"), "html")
        self.assertEqual(detect_input_mode("hello <b>world</b>"), "markdown")
        self.assertEqual(detect_input_mode("<div>x</div>", "markdown"), "markdown")

    def test_split_content_keeps_fences_together(self):
        text = ("A" * 60) + "\n\n" + "B" * 60 + "\n\n```python\n" + "\n".join(["print(1)"] * 30) + "\n```"
        chunks = split_content(text, 90)
        joined = "\n\n".join(chunks)
        self.assertEqual(joined, text)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(any("```python" in chunk for chunk in chunks))

    def test_unknown_html_protocol_removed(self):
        out = sanitize_user_html('<a href="javascript:alert(1)">x</a>')
        self.assertNotIn("javascript:", out)


if __name__ == "__main__":
    unittest.main()
