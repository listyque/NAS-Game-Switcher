"""TACTIC REST transport adapted from openheavens update_rest.py and tactic.ts."""

import json
import re
import urllib.error
import urllib.request
from urllib.parse import urlsplit


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("TACTIC redirects are disabled to protect credentials.")


class TacticClient:
    def __init__(self, base_url, project):
        parsed = urlsplit(base_url)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("Expected an HTTP(S) TACTIC base URL without credentials.")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", project):
            raise ValueError("Invalid TACTIC project code.")
        self.endpoint = base_url.rstrip("/") + "/" + project + "/REST/"
        self.ticket = ""
        self.opener = urllib.request.build_opener(_NoRedirect)

    def call(self, method, payload, timeout=30):
        if not re.fullmatch(r"[a-z][a-z0-9_]*", method):
            raise ValueError("Invalid REST method.")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.ticket:
            headers["Authorization"] = "Bearer " + self.ticket
        request = urllib.request.Request(
            self.endpoint + method, data=json.dumps(payload).encode("utf-8"), headers=headers
        )
        try:
            with self.opener.open(request, timeout=timeout) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            # Server error pages can contain request details and authentication tickets.
            error.close()
            raise RuntimeError("TACTIC HTTP error %s; inspect the server log." % error.code) from None
        except (ValueError, UnicodeError):
            raise RuntimeError("TACTIC returned an invalid JSON response.") from None
        if isinstance(result, dict) and any(
            result.get(key) for key in ("faultCode", "exception", "__exception__", "error")
        ):
            raise RuntimeError("TACTIC rejected the request; inspect the server log.")
        return result

    def login(self, login, password):
        result = self.call("get_ticket", {"login": login, "password": password})
        ticket = result if isinstance(result, str) else result.get("ticket") if isinstance(result, dict) else None
        if not isinstance(ticket, str) or not ticket:
            raise RuntimeError("TACTIC did not return an authentication ticket.")
        self.ticket = ticket

    def script(self, code, timeout=30, **kwargs):
        result = self.call(
            "execute_python_script", {"script_path": "", "kwargs": {"code": code, **kwargs}}, timeout
        )
        if not isinstance(result, dict) or "info" not in result:
            raise RuntimeError("Unexpected TACTIC Script Editor response.")
        return result["info"]
