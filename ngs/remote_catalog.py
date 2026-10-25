"""Restricted HTTP catalogizer requests; never read a client-side NAS path."""
import re

from ngs.catalog import relative_game_path
from ngs.metadata import metadata_reference


def validate_catalog_request(value):
    if not isinstance(value, dict):
        raise ValueError("Invalid catalogizer request")
    action = value.get("action")
    if isinstance(action, str) and action.startswith("installer_"):
        action = action.removeprefix("installer_")
    fields = {"list": {"offset"}, "folders": {"offset", "hide_published"}, "results": {"offset", "hide_published", "preview"}, "batch": set(), "exes": {"folder"}, "search": {"query"},
              "preview": {"folder", "executable", "provider"},
              "publish": {"preview"}, "status": {"preview"}, "cancel": {"preview"}, "release": {"preview"},
              "custom": {"preview", "kind", "offset", "total", "sha256", "data"},
              "estimate": {"game", "revision"}, "download": {"game", "revision", "preview"}, "transfer": {"preview", "ack"}, "repair": {"preview", "index"}}
    expected = fields.get(action,set()) | {'action'} if isinstance(action,str) else set()
    if action == 'download' and 'archive' in value:
        if value['archive'] != 'tar-zstd':raise ValueError('Unsupported archive format')
        expected |= {'archive'}
    if not isinstance(action, str) or action not in fields or set(value) != expected:
        raise ValueError("Unsupported catalogizer action or fields")
    if action in ("list", "folders", "results") and (type(value["offset"]) is not int or not 0 <= value["offset"] <= 100_000):
        raise ValueError("Invalid catalog page")
    if action in ("folders", "results") and type(value["hide_published"]) is not bool:
        raise ValueError("Invalid catalog filter")
    if action in ("estimate", "download"):
        if (not isinstance(value["game"], str) or not re.fullmatch("[a-z0-9][a-z0-9_-]{0,79}", value["game"])
                or type(value["revision"]) is not int or not 1 <= value["revision"] <= 2**31):
            raise ValueError("Invalid download identity")
    if action == "transfer" and (type(value["ack"]) is not int or not -1 <= value["ack"] <= 100_000):
        raise ValueError("Invalid transfer acknowledgement")
    if action == "repair" and (type(value["index"]) is not int or not 0 <= value["index"] <= 100_000):
        raise ValueError("Invalid stream block")
    if "folder" in value:
        relative_game_path(value["folder"])
        if "/" in value["folder"]:
            raise ValueError("Select one game folder")
    if "executable" in value:
        relative_game_path(value["executable"])
        if not value["executable"].lower().endswith(".exe"):
            raise ValueError("Select an EXE")
    if "query" in value and (not isinstance(value["query"], str) or not 1 <= len(value["query"].strip()) <= 160
                             or any(ord(c) < 32 for c in value["query"])):
        raise ValueError("Invalid catalogizer search")
    if "provider" in value and value["provider"] != "manual":
        metadata_reference(value["provider"])
    if "preview" in value and (not isinstance(value["preview"], str) or not re.fullmatch("[0-9a-f]{32}", value["preview"])):
        raise ValueError("Invalid catalogizer preview")
    if action == 'custom':
        import base64
        limit = {'metadata': 16*1024, 'cover': 48*1024, 'background': 96*1024}.get(value['kind']) if isinstance(value['kind'], str) else None
        if (limit is None or type(value['total']) is not int or not 0 < value['total'] <= limit
                or type(value['offset']) is not int or not 0 <= value['offset'] < value['total']
                or not isinstance(value['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', value['sha256'])
                or not isinstance(value['data'], str) or not 0 < len(value['data']) <= 1708):
            raise ValueError('Invalid custom artwork block')
        raw = base64.b64decode(value['data'], validate=True)
        if not 0 < len(raw) <= 1280 or value['offset'] + len(raw) > value['total']:
            raise ValueError('Invalid custom artwork size')
    return value
