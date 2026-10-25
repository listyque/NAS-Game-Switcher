import unittest
from ngs.appearance import colors, ACCENTS


class AppearanceTests(unittest.TestCase):
    def test_accent_changes_the_whole_material_palette_and_glass_keeps_a_readable_tint(self):
        from PySide6.QtGui import QColor
        for theme in ('dark', 'light'):
            values = [colors(dict(theme=theme, accent_color=accent)) for accent in ACCENTS]
            for key in ('background', 'surface', 'base', 'button', 'hover', 'soft', 'glass', 'ambient'):
                self.assertEqual(len({value[key] for value in values}), len(ACCENTS), (theme, key))
            for value in values:
                self.assertTrue(all(QColor(color).isValid() for key, color in value.items() if key != 'light'))
                self.assertGreater(QColor(value['glass']).alphaF(), .5)
                self.assertLess(QColor(value['glass']).alphaF(), .75)
                self.assertEqual(QColor(value['border']).alpha(), 0)
        self.assertNotEqual(colors(dict(theme='dark'))['glass'], colors(dict(theme='light'))['glass'])
    def test_themes_keep_readable_text_and_filled_buttons_and_tolerate_old_settings(self):
        from PySide6.QtGui import QColor
        def over(foreground, background):
            color, base = QColor(foreground).getRgbF(), QColor(background).getRgbF()
            return QColor.fromRgbF(*(color[i]*color[3]+base[i]*(1-color[3]) for i in range(3))).name()
        def luminance(value):
            channels = [int(value[i:i+2], 16) / 255 for i in (1, 3, 5)]
            channels = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in channels]
            return sum(v * weight for v, weight in zip(channels, (.2126, .7152, .0722)))
        def contrast(a, b):
            a, b = sorted((luminance(a), luminance(b)))
            return (b + .05) / (a + .05)
        for theme in ('dark', 'light'):
            for accent in ACCENTS:
                palette = colors(dict(theme=theme, accent_color=accent))
                for foreground, background in (('text', 'surface'), ('muted', 'background'), ('accentText', 'accent')):
                    self.assertGreaterEqual(contrast(palette[foreground], palette[background]), 4.5, (theme, accent, foreground))
                backdrop = over(palette['scrim'], '#ffffff' if theme == 'dark' else '#000000')
                glass = over(palette['glass'], backdrop)
                for background in (glass, *(over(palette[key], glass) for key in ('soft', 'button', 'hover'))):
                    for foreground in ('text', 'muted'):
                        self.assertGreaterEqual(contrast(palette[foreground], background), 4.5, (theme, accent, foreground))
        self.assertEqual(colors({}), colors(dict(theme='unknown', accent_color=[])))
