"""Tests for the design tokens, the icon loader and the native toolbar fallback."""

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

from unittest import mock  # noqa: E402

from PySide6.QtCore import QSize  # noqa: E402
from PySide6.QtGui import QColor, QIcon, QPalette  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QMainWindow, QSplitter, QWidget  # noqa: E402

from universe.ui import icons, motion, style  # noqa: E402
from universe.ui.platform import toolbar  # noqa: E402

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


class NativeToolbarTests(unittest.TestCase):
    def test_offscreen_keeps_the_qt_toolbar(self):
        self.assertFalse(toolbar.available())


class LiveThemeTests(unittest.TestCase):
    """Icons look their colour up when painted, so a light/dark switch reaches them."""

    def centre_colour(self, icon) -> QColor:
        image = icon.pixmap(QSize(32, 32)).toImage()
        return max((image.pixelColor(x, y) for x in range(32) for y in range(32)), key=lambda c: c.alpha())

    def test_text_icons_follow_a_palette_change(self):
        before = app.palette()
        self.addCleanup(app.setPalette, before)
        icon = icons.icon("square")
        palette = QPalette(before)
        palette.setColor(QPalette.ColorGroup.Active, QPalette.ColorRole.WindowText, QColor("#ff0000"))
        app.setPalette(palette)
        self.assertEqual(self.centre_colour(icon).name(), "#ff0000")
        palette.setColor(QPalette.ColorGroup.Active, QPalette.ColorRole.WindowText, QColor("#00ff00"))
        app.setPalette(palette)
        self.assertEqual(self.centre_colour(icon).name(), "#00ff00")

    def test_accent_icons_follow_the_theme(self):
        icon = icons.icon("square", icons.ACCENT)
        with mock.patch.object(style, "_is_dark", return_value=False):
            light = self.centre_colour(icon).name().upper()
        with mock.patch.object(style, "_is_dark", return_value=True):
            dark = self.centre_colour(icon).name().upper()
        self.assertEqual((light, dark), (style.ACCENT_LIGHT, style.ACCENT_DARK))

    def test_selected_rows_get_white_icons(self):
        image = icons.icon("square").pixmap(QSize(32, 32), QIcon.Mode.Selected).toImage()
        strongest = max((image.pixelColor(x, y) for x in range(32) for y in range(32)), key=lambda c: c.alpha())
        self.assertEqual(strongest.name(), "#ffffff")


class MotionTests(unittest.TestCase):
    def setUp(self):
        self.window = QMainWindow()
        self.addCleanup(self.window.deleteLater)
        self.split = QSplitter()
        for _ in range(2):
            self.split.addWidget(QWidget())
        self.window.setCentralWidget(self.split)
        self.window.resize(400, 300)
        self.window.show()
        self.addCleanup(self.window.hide)
        app.processEvents()

    def covers(self) -> list[QLabel]:
        return [w for w in self.window.findChildren(QLabel) if w.graphicsEffect() is not None]

    def test_fade_covers_the_old_look_then_goes(self):
        changed = []
        with mock.patch.object(motion, "enabled", return_value=True):
            motion.crossfade(self.split, lambda: changed.append(True))
        self.assertEqual(changed, [True])  # the change itself is immediate
        self.assertEqual(len(self.covers()), 1)
        self.assertEqual(self.split.count(), 2)  # the cover is not a new splitter column
        QTest.qWait(motion.FADE_MS + 100)
        app.processEvents()
        self.assertEqual(self.covers(), [])

    def test_reduce_motion_changes_without_fading(self):
        changed = []
        with mock.patch.object(motion, "enabled", return_value=False):
            motion.crossfade(self.split, lambda: changed.append(True))
        self.assertEqual((changed, self.covers()), ([True], []))


if __name__ == "__main__":
    unittest.main()
