import sys
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from tools.tactic_project import create_ngs_project


def module(name, **attributes):
    result = ModuleType(name)
    result.__dict__.update(attributes)
    return result


class ProjectCreationTests(unittest.TestCase):
    def setup_server(self, existing=None, database=False, project_type=False):
        self.fields = {}
        self.created_type = Mock()
        self.project = SimpleNamespace(
            get_value=lambda key: self.fields.get(key, ""),
            get_database_name=lambda: "ngs_launcher",
        )
        self.Project = Mock()
        self.Project.get_by_code.side_effect = [existing, self.project]
        self.Config = SimpleNamespace(get_value=lambda *args: "open_heavens_cafe")
        self.Environment = SimpleNamespace(
            get_security=lambda: SimpleNamespace(is_admin=lambda: True),
            get_release_version=lambda: "5.0.0.a02",
        )
        self.Search = Mock()
        self.Search.return_value.get_sobject.return_value = object() if project_type else None
        self.SearchType = Mock()
        self.SearchType.create.return_value = self.created_type
        self.DbContainer = Mock()
        self.DbContainer.get.return_value.do_query.return_value = [(1,)] if database else []
        self.commands = []
        fields = self.fields

        class FakeCreateProject:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        def execute(command):
            self.commands.append(command.kwargs)
            fields["description"] = command.kwargs["description"]

        self.modules = {
            "pyasm": module("pyasm"),
            "pyasm.biz": module("pyasm.biz", Project=self.Project),
            "pyasm.common": module("pyasm.common", Config=self.Config, Environment=self.Environment),
            "pyasm.search": module("pyasm.search", Search=self.Search, SearchType=self.SearchType, DbContainer=self.DbContainer),
            "pyasm.command": module("pyasm.command", Command=SimpleNamespace(execute_cmd=execute)),
            "tactic": module("tactic"),
            "tactic.command": module("tactic.command", CreateProjectCmd=FakeCreateProject),
        }

    def test_creates_only_ngs_project_and_its_own_type(self):
        self.setup_server()
        with patch.dict(sys.modules, self.modules):
            result = create_ngs_project()
        self.assertEqual(result["project"], "ngs_launcher")
        self.assertEqual(result["default_project"], "open_heavens_cafe")
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(self.commands[0]["project_code"], "ngs_launcher")
        self.assertEqual(self.commands[0]["project_type"], "ngs_launcher")
        self.assertIs(self.commands[0]["is_main_project"], False)
        self.SearchType.create.assert_called_once_with("sthpw/project_type")
        self.created_type.set_value.assert_any_call("code", "ngs_launcher")
        self.created_type.set_value.assert_any_call("type", "simple")
        self.created_type.commit.assert_called_once_with()

    def test_existing_project_database_or_type_are_never_overwritten(self):
        for collision in ("project", "database", "type"):
            with self.subTest(collision=collision):
                self.setup_server(
                    existing=SimpleNamespace(get_value=lambda key: "another owner") if collision == "project" else None,
                    database=collision == "database", project_type=collision == "type",
                )
                with patch.dict(sys.modules, self.modules):
                    with self.assertRaises(RuntimeError):
                        create_ngs_project()
                self.assertEqual(self.commands, [])
                self.SearchType.create.assert_not_called()

    def test_different_tactic_version_is_read_only(self):
        self.setup_server()
        self.Environment.get_release_version = lambda: "5.1"
        with patch.dict(sys.modules, self.modules):
            with self.assertRaises(RuntimeError):
                create_ngs_project()
        self.Project.get_by_code.assert_not_called()
        self.assertEqual(self.commands, [])


if __name__ == "__main__":
    unittest.main()
