"""Small shared palette for the launcher and its Qt Quick controls."""
from functools import lru_cache
from PySide6.QtGui import QColor, QPalette

ACCENTS = {
    'green': ('#9ee8be', '#39694b'), 'blue': ('#8bbcff', '#355f92'),
    'violet': ('#c4a7ff', '#6b4c96'), 'amber': ('#ffd395', '#785821'),
    'rose': ('#ffaccb', '#944a64'),
}


def _tint(value, chosen, amount, alpha=1):
    base = QColor(value)
    return QColor.fromRgbF(*(base_channel*(1-amount)+accent_channel*amount for base_channel, accent_channel in
                             zip(base.getRgbF()[:3], chosen.getRgbF()[:3])), alpha).name(
                                 QColor.HexRgb if alpha == 1 else QColor.HexArgb)


def colors(settings):
    light = settings.get('theme') == 'light'
    accent = settings.get('accent_color')
    accent = accent if isinstance(accent, str) and accent in ACCENTS else 'green'
    return _colors(light, accent).copy()


@lru_cache(maxsize=10)
def _colors(light, accent):
    dark_accent, light_accent = ACCENTS[accent]
    chosen = QColor(light_accent if light else dark_accent)
    result = dict(background=_tint('#f2f1ef', chosen, .05), surface=_tint('#ffffff', chosen, .08), base=_tint('#e9e8e6', chosen, .06),
                  button=_tint('#ffffff', chosen, .06, .42), hover=_tint('#ffffff', chosen, .15, .62),
                  text='#202024', muted='#484852', border='#00ffffff') if light else dict(
                  background=_tint('#111113', chosen, .07), surface=_tint('#262629', chosen, .09), base=_tint('#171719', chosen, .07),
                  button=_tint('#ffffff', chosen, .06, .07), hover=_tint('#ffffff', chosen, .15, .10),
                  text='#f3f2f7', muted='#e5e4eb', border='#00ffffff')
    result.update(accent=light_accent if light else dark_accent,
                  accentText='#ffffff' if light else '#17171b', keyboardAccent=dark_accent,
                  soft=_tint(result['surface'], chosen, .28, .72),
                  glass=_tint('#ffffff' if light else '#121318', chosen, .04, .68 if light else .70),
                  glassEdge='#50ffffff' if light else '#24ffffff',
                  glassShine='#28ffffff' if light else '#0cffffff',
                  scrim=_tint(result['background'], chosen, 0, .26 if light else .38),
                  ambient=_tint(result['surface'], chosen, .65, .30),
                  warning='#87501e' if light else '#ffd2aa',
                  error='#a7364b' if light else '#ffacbb',
                  line='#18202024' if light else '#16ffffff', light=light)
    return result


def apply_palette(application, values):
    palette = QPalette()
    for role, key in ((QPalette.Window, 'surface'), (QPalette.WindowText, 'text'),
                      (QPalette.Base, 'base'), (QPalette.Text, 'text'), (QPalette.Button, 'button'),
                      (QPalette.ButtonText, 'text'), (QPalette.Highlight, 'accent'),
                      (QPalette.HighlightedText, 'accentText'), (QPalette.ToolTipBase, 'surface'),
                      (QPalette.ToolTipText, 'text'), (QPalette.PlaceholderText, 'muted'),
                      (QPalette.Light, 'border'), (QPalette.Mid, 'border'), (QPalette.Dark, 'border')):
        palette.setColor(role, QColor(values[key]))
    palette.setColor(QPalette.Disabled, QPalette.Text, QColor(values['muted']))
    palette.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(values['muted']))
    application.setPalette(palette)
