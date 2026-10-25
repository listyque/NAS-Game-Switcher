"""Bounded diagnostic uploads on the existing signed NGS endpoint."""
import re


def validate_diagnostic_request(value):
    import base64
    if not isinstance(value, dict): raise ValueError('Invalid diagnostic request')
    action = value.get('action')
    fields = {'action', 'session', 'sha256', 'size'}
    if action == 'append': fields |= {'offset', 'data'}
    if action not in ('status', 'append', 'finish') or set(value) != fields:
        raise ValueError('Invalid diagnostic fields')
    if (not isinstance(value['session'], str) or not re.fullmatch('[0-9a-f]{32}', value['session'])
            or not isinstance(value['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', value['sha256'])
            or type(value['size']) is not int or not 0 < value['size'] <= 128 * 1024):
        raise ValueError('Invalid diagnostic identity')
    if action == 'append':
        if (type(value['offset']) is not int or not 0 <= value['offset'] < value['size']
                or not isinstance(value['data'], str) or not 0 < len(value['data']) <= 1368):
            raise ValueError('Invalid diagnostic block')
        raw = base64.b64decode(value['data'], validate=True)
        if not 0 < len(raw) <= 1024 or value['offset'] + len(raw) > value['size']:
            raise ValueError('Invalid diagnostic size')
    return value
