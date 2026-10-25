"""Portable screen-keyboard layouts; no system keyboard drivers or registry state."""
from ngs.i18n import tr
import math
import re

LAYERS = ('base', 'shift', 'altgr', 'shift_altgr')
ACTIONS = ('@settings', '@hide', '@language', '@symbols', '@letters', '@touchpads', '@voice')


def validate_layout(value, keys):
    if not isinstance(value, dict) or set(value) - {'id', 'name', 'rows'}:
        raise ValueError(tr("Invalid keyboard layout."))
    identity, name, rows = value.get('id', ''), value.get('name'), value.get('rows')
    if not isinstance(identity, str) or identity and not re.fullmatch('[0-9a-f]{32}', identity):
        raise ValueError(tr("Invalid layout ID."))
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60 or any(ord(c) < 32 or 0xd800 <= ord(c) <= 0xdfff for c in name):
        raise ValueError(tr("Name: 1 to 60 characters."))
    if not isinstance(rows, list) or not 1 <= len(rows) <= 8:
        raise ValueError(tr("A layout must contain 1 to 8 rows."))
    result, count, settings, hides = [], 0, 0, 0
    for row in rows:
        if not isinstance(row, list) or not 1 <= len(row) <= 24:
            raise ValueError(tr("A row must contain 1 to 24 keys."))
        clean = []
        for item in row:
            if not isinstance(item, dict) or set(item) - {'key', 'width', 'states'}:
                raise ValueError(tr("Invalid key."))
            key, width, states = item.get('key'), item.get('width', 1), item.get('states', {})
            if not isinstance(key, str) or key not in keys and key not in ACTIONS and key != 'AltGr':
                raise ValueError(tr("Unknown key."))
            if type(width) not in (int, float) or not math.isfinite(width) or not .5 <= width <= 8:
                raise ValueError(tr("Key width: 0.5 to 8 normal keys."))
            if not isinstance(states, dict) or set(states) - set(LAYERS):
                raise ValueError(tr("Unknown symbol layer."))
            for text in states.values():
                if not isinstance(text, str) or len(text) > 16 or any(ord(c) < 32 or ord(c) == 127 or 0xd800 <= ord(c) <= 0xdfff for c in text):
                    raise ValueError(tr("Layer symbols: up to 16 characters without control codes."))
            if states and (key not in keys or key in ('Ctrl', 'Shift', 'Alt', 'Win', 'AltGr', 'RightShift', 'RightCtrl', 'RightAlt')):
                raise ValueError(tr("System buttons cannot type characters."))
            settings += key == '@settings'; hides += key == '@hide'; count += 1
            clean.append(dict(key=key, width=width, states=dict(states)))
        result.append(clean)
    if count > 160 or settings != 1 or hides != 1:
        raise ValueError(tr("Up to 160 keys, one settings button and one hide button."))
    return dict(id=identity, name=name.strip(), rows=result)


def layer_name(modifiers, caps=False):
    shifted = ('Shift' in modifiers) != caps
    altgr = 'AltGr' in modifiers
    return ('shift_altgr' if shifted else 'altgr') if altgr else ('shift' if shifted else 'base')
