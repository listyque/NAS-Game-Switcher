import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from ngs.tactic import TacticClient, _NoRedirect
from tools.tactic_project import admin_password, create_ngs_project


class TacticTests(unittest.TestCase):
    def test_transport_authenticates_and_uses_selected_project(self):
        client = TacticClient("http://nas:8888/tactic/", "ngs_launcher")
        with patch.object(client.opener, "open", side_effect=[
            io.BytesIO(b'"test-ticket"'), io.BytesIO(b'{"info":{"status":"ok"}}')
        ]) as opened:
            client.login("scanner", "private-password")
            self.assertEqual(client.script("return {}"), {"status": "ok"})
        login_request = opened.call_args_list[0].args[0]
        script_request = opened.call_args_list[1].args[0]
        self.assertEqual(login_request.full_url, "http://nas:8888/tactic/ngs_launcher/REST/get_ticket")
        self.assertEqual(script_request.get_header("Authorization"), "Bearer test-ticket")
        self.assertNotIn("private-password", script_request.data.decode())
        self.assertEqual(json.loads(script_request.data)["kwargs"]["code"], "return {}")

    def test_rejects_endpoint_credentials_and_path_injection(self):
        for url in ("http://user:secret@nas/tactic", "file:///tmp/tactic", "http://nas/tactic?secret=x"):
            with self.assertRaises(ValueError):
                TacticClient(url, "ngs_launcher")
        with self.assertRaises(ValueError):
            TacticClient("http://nas/tactic", "../open_heavens_cafe")
        with self.assertRaises(ValueError):
            TacticClient("http://nas/tactic", "admin").call("../query", {})

    def test_errors_do_not_disclose_server_response(self):
        client = TacticClient("http://nas/tactic", "admin")
        errors = [
            io.BytesIO(b'{"exception":"secret ticket"}'),
            io.BytesIO(b'<html>secret ticket</html>'),
            urllib.error.HTTPError("http://nas", 500, "secret ticket", {}, None),
        ]
        for response in errors:
            with self.subTest(response=type(response).__name__):
                with patch.object(client.opener, "open", side_effect=[response]):
                    with self.assertRaises(RuntimeError) as raised:
                        client.call("query", {})
                self.assertNotIn("secret", str(raised.exception))

    def test_redirect_is_rejected(self):
        with self.assertRaises(RuntimeError):
            _NoRedirect().redirect_request(None, None, 302, "", {}, "http://other-host")

    def test_password_file_is_read_without_changes(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as folder:
            path = Path(folder) / "credentials.env"
            content = b'# local settings\nTACTIC_ADMIN_PASSWORD="a=b"\n'
            path.write_bytes(content)
            with patch.dict("os.environ", {"NGS_TACTIC_ADMIN_PASSWORD": ""}):
                self.assertEqual(admin_password(path), "a=b")
            self.assertEqual(path.read_bytes(), content)

    def test_creation_requires_admin_before_any_database_operation(self):
        import sys
        from types import ModuleType, SimpleNamespace
        def module(name, **attributes):
            item = ModuleType(name)
            item.__dict__.update(attributes)
            return item
        security = SimpleNamespace(is_admin=lambda: False)
        modules = {
            "pyasm": module("pyasm"),
            "pyasm.biz": module("pyasm.biz", Project=object()),
            "pyasm.common": module("pyasm.common", Config=object(), Environment=SimpleNamespace(get_security=lambda: security)),
            "pyasm.search": module("pyasm.search", Search=object(), SearchType=object(), DbContainer=object()),
            "pyasm.command": module("pyasm.command", Command=object()),
            "tactic": module("tactic"),
            "tactic.command": module("tactic.command", CreateProjectCmd=object()),
        }
        with patch.dict(sys.modules, modules):
            with self.assertRaises(PermissionError):
                create_ngs_project()


if __name__ == "__main__":
    unittest.main()
