"""Tests for the design tokens, the icon loader and the glass fallback."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("UNIVERSE_DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMainWindow, QWidget  # noqa: E402

from universe.ui import icons, style  # noqa: E402
from universe.ui.platform import glass  # noqa: E402

app = QApplication.instance() or QApplication([])


class TokenTests(unittest.TestCase):
    def test_light_and_dark_use_their_own_accent(self):
        self.assertEqual(style.tokens(False).accent, style.ACCENT_LIGHT)
        self.assertEqual(style.tokens(True).accent, style.ACCENT_DARK)

    def test_stylesheet_uses_tokens_not_old_blue(self):
        for dark in (False, True):
            sheet = style.stylesheet(style.tokens(dark))
            self.assertIn(style.tokens(dark).accent_fill, sheet)
            self.assertNotIn("#2f6fed", sheet.lower())

    def test_accent_tint_keeps_alpha(self):
        self.assertEqual(style.tokens(False).accent_tint(60).alpha(), 60)

    def test_mono_font_is_monospaced(self):
        self.assertTrue(style.tokens().mono)


class IconTests(unittest.TestCase):
    def test_every_bundled_icon_renders(self):
        self.assertGreaterEqual(len(icons.names()), 40)
        for name in icons.names():
            self.assertFalse(icons.icon(name).isNull(), name)

    def test_icon_pixmap_is_retina_sharp(self):
        pixmap = icons.pixmap("timer", "#2F6497", 16, 2.0)
        self.assertEqual((pixmap.width(), pixmap.devicePixelRatio()), (32, 2.0))
        image = pixmap.toImage()
        self.assertTrue(any(image.pixelColor(x, y).alpha() for x in range(32) for y in range(32)))

    def test_translucent_colour_dims_the_icon(self):
        image = icons.pixmap("timer", "#802F6497", 16, 1.0).toImage()
        self.assertLess(max(image.pixelColor(x, y).alpha() for x in range(16) for y in range(16)), 200)

    def test_unknown_icon_raises(self):
        with self.assertRaises(KeyError):
            icons.icon("no-such-icon")


class GlassTests(unittest.TestCase):
    def test_glass_falls_back_to_solid_offscreen(self):
        window = QMainWindow()
        self.assertFalse(glass.available())
        self.assertFalse(glass.prepare_window(window))
        side = QWidget(window)
        self.assertFalse(glass.apply(side, glass.Role.SIDEBAR))
        self.assertTrue(side.autoFillBackground())


if __name__ == "__main__":
    unittest.main()
