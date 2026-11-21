"""Τα tokens του κώδικα πρέπει να συμφωνούν με το docs/ui-style.md §1."""
from pathlib import Path
from unittest import TestCase
import re

from shared.ui import theme

ROOT = Path(__file__).resolve().parents[1]


def style_guide_tokens() -> dict[str, str]:
    text = (ROOT / "docs" / "ui-style.md").read_text(encoding="utf-8")
    return dict(re.findall(r"^\| `([a-z_]+)` \| `(#[0-9a-fA-F]{6})` \|", text, re.M))


class ThemeTokenTests(TestCase):
    def test_tokens_match_style_guide(self):
        documented = style_guide_tokens()
        self.assertGreaterEqual(len(documented), 14)
        self.assertEqual({k: v.lower() for k, v in documented.items()}, theme.TOKENS)

    def test_constants_match_token_table(self):
        for name, value in theme.TOKENS.items():
            self.assertEqual(getattr(theme, name.upper()), value)

    def test_app_icon_source_uses_app_icon_token(self):
        svg = theme.APP_ICON_SVG.read_text(encoding="utf-8")
        self.assertIn(f'fill="{theme.APP_ICON}"', svg)

    def test_stylesheet_uses_only_tokens(self):
        allowed = set(theme.TOKENS.values()) | {theme.WHITE}
        used = {c.lower() for c in re.findall(r"#[0-9a-fA-F]{6}\b", theme.stylesheet())}
        self.assertLessEqual(used, allowed)
        for asset in ("check.svg", "radio_dot.svg"):
            self.assertTrue((theme.ASSETS_DIR / asset).is_file())

    def test_ui_code_has_no_scattered_hex_colours(self):
        for path in [ROOT / "director" / "qt_gui.py", ROOT / "shared" / "ui" / "icons.py"]:
            with self.subTest(path=path.name):
                self.assertEqual(re.findall(r"#[0-9a-fA-F]{6}\b", path.read_text(encoding="utf-8")), [])
