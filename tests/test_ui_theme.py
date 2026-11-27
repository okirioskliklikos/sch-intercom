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
        for path in [ROOT / "director" / "qt_gui.py", ROOT / "director" / "qt_security.py",
                     ROOT / "shared" / "ui" / "icons.py", ROOT / "client" / "qt_client.py",
                     ROOT / "client" / "status.py"]:
            with self.subTest(path=path.name):
                self.assertEqual(re.findall(r"#[0-9a-fA-F]{6}\b", path.read_text(encoding="utf-8")), [])


class ThemeMetricsTests(TestCase):
    def test_base_follows_system_but_never_below_minimum(self):
        self.assertEqual(theme.metrics_for(9.0).base_pt, theme.FONT_MIN_PT)
        self.assertEqual(theme.metrics_for(None).base_pt, theme.FONT_MIN_PT)
        self.assertEqual(theme.metrics_for(-1).base_pt, theme.FONT_MIN_PT)
        self.assertEqual(theme.metrics_for(14.0).base_pt, 14.0)

    def test_minimum_matches_qt_font_dpi_120_on_9pt_system(self):
        # Το QT_FONT_DPI=120 που άρεσε στον χρήστη = κλίμακα 1,25 του mockup.
        self.assertAlmostEqual(theme.metrics_for(9.0).scale, 1.25, delta=0.03)

    def test_mockup_scale_is_identity(self):
        m = theme.Metrics(theme.MOCKUP_BASE_PT)
        self.assertEqual(m.px(176), 176)
        self.assertEqual(m.font_pt(), 9.75)

    def test_stylesheet_scales_fonts_and_sizes(self):
        for base in (9.75, 12.0, 16.0):
            with self.subTest(base=base):
                m = theme.Metrics(base)
                qss = theme.stylesheet(m)
                self.assertIn(f"font-size: {m.font_pt()}pt;", qss)
                self.assertIn(f"font-size: {m.font_pt(theme.FONT_PAGE_TITLE)}pt;", qss)
                self.assertIn(f"min-height: {m.px(20)}px;", qss)
                self.assertNotRegex(qss, r"font-size: [0-9.]+px")

    def test_pill_radius_never_exceeds_half_height(self):
        for base in (9.75, 10.5, 12.0, 13.3, 16.0, 20.0):
            m = theme.Metrics(base)
            radius = int(re.search(r"#pill \{\{?\s*border-radius: (\d+)px", theme.stylesheet(m)).group(1))
            self.assertLessEqual(radius * 2, 2 * m.px(6) + m.px(20) + 2)
