"""Inspect TACTIC or create only the new ngs_launcher project. No worker restarts."""

import argparse
import inspect
import json
import os
from pathlib import Path

from ngs.tactic import TacticClient
from ngs.portable import DEFAULT_TACTIC_URL

DEFAULT_CREDENTIALS = Path(__file__).resolve().parents[1] / "data/provisioning/tactic-admin.env"


def project_snapshot():
    """Read-only preflight, also used to verify project creation."""
    from pyasm.biz import Project
    from pyasm.common import Config, Environment
    from pyasm.search import Search, DbContainer

    if not Environment.get_security().is_admin():
        raise PermissionError("TACTIC administrator access is required.")
    projects = Search("sthpw/project").get_sobjects()
    types = Search("sthpw/project_type").get_sobjects()
    sql = DbContainer.get("sthpw")
    return {
        "version": Environment.get_release_version(),
        "default_project": Config.get_value("install", "default_project"),
        "projects": [
            {key: obj.get_value(key) for key in ("code", "title", "type", "description", "s_status", "last_db_update", "last_version_update")}
            for obj in projects
        ],
        "project_types": [obj.get_value("code") for obj in types],
        "target_database_exists": bool(sql.do_query(
            "SELECT 1 FROM pg_database WHERE datname = 'ngs_launcher'"
        )),
    }


def create_ngs_project():
    """Server-side operation with an intentionally fixed, isolated target."""
    from pyasm.biz import Project
    from pyasm.common import Config, Environment
    from pyasm.search import Search, SearchType, DbContainer
    from pyasm.command import Command
    from tactic.command import CreateProjectCmd

    code = "ngs_launcher"
    marker = "NGS handheld game catalog; managed from D:/code/NGS"
    if not Environment.get_security().is_admin():
        raise PermissionError("TACTIC administrator access is required.")
    # shortcut: initialization targets TACTIC 5.0.0.a02, review before a TACTIC upgrade.
    if Environment.get_release_version() != "5.0.0.a02":
        raise RuntimeError("Project creation was reviewed for TACTIC 5.0.0.a02 only.")
    existing = Project.get_by_code(code, use_cache=False)
    if existing:
        if existing.get_value("description") != marker:
            raise RuntimeError("Target project already exists and is not owned by NGS.")
        raise RuntimeError("NGS project already exists; use read-only inspection instead.")
    sql = DbContainer.get("sthpw")
    if sql.do_query("SELECT 1 FROM pg_database WHERE datname = 'ngs_launcher'"):
        raise RuntimeError("Target database already exists; refusing to overwrite it.")
    query = Search("sthpw/project_type")
    query.add_filter("code", code)
    if query.get_sobject():
        raise RuntimeError("Target project type already exists; inspect it before continuing.")

    class NgsCreateProject(CreateProjectCmd):
        def upgrade(self):
            # The general upgrader also changes shared project state and copies shared templates.
            from pyasm.common import Container
            from pyasm.search.upgrade.project import ConfigUpgrade, SimpleUpgrade
            for upgrade_type in (ConfigUpgrade, SimpleUpgrade):
                upgrade = upgrade_type()
                upgrade.set_project(code)
                upgrade.set_to_version(Environment.get_release_version())
                upgrade.set_forced(True)
                upgrade.set_quiet(True)
                upgrade.set_confirmed(True)
                upgrade.execute()
                errors = Container.get_seq(code + "|" + upgrade_type.__name__)
                # The shipped 5.0 schema has spt_* tables; legacy migrations also try old names.
                legacy_errors = (
                    'relation "prod_setting" does not exist',
                    'relation "widget_config" does not exist',
                    'relation "naming" does not exist',
                    'relation "custom_script" does not exist',
                )
                if any(not str(error).startswith(legacy_errors) for method, error in errors):
                    raise RuntimeError("New project initialization reported migration errors; inspect before retrying.")
            project = Project.get_by_code(code, use_cache=False)
            if project.get_database_name() != code:
                raise RuntimeError("Unexpected project database.")
            database = DbContainer.get(project.get_project_db_resource())
            columns = set(tuple(row) for row in database.do_query(
                "SELECT table_name,column_name FROM information_schema.columns WHERE table_schema='public'"
            ))
            required = {
                ("spt_widget_config", "priority"), ("spt_widget_config", "title"),
                ("spt_widget_config", "description"), ("spt_process", "workflow"),
                ("spt_process", "subpipeline_code"), ("spt_pipeline", "name"),
                ("spt_pipeline", "type"), ("spt_authenticate", "data"),
                ("spt_process_state", "state"), ("spt_process_state", "status"),
            }
            if not required.issubset(columns):
                raise RuntimeError("New project schema is incomplete; inspect before retrying.")
            project.set_value("last_version_update", Environment.get_release_version())
            project.set_value("last_db_update", "now()", quoted=False)
            project.commit(triggers=False)

    before = Config.get_value("install", "default_project")
    project_type = SearchType.create("sthpw/project_type")
    project_type.set_value("code", code)
    project_type.set_value("type", "simple")
    project_type.commit()
    command = NgsCreateProject(
        project_code=code,
        project_title="NGS Game Library",
        project_type=code,
        description=marker,
        is_main_project=False,
        use_default_side_bar=True,
    )
    Command.execute_cmd(command)
    created = Project.get_by_code(code, use_cache=False)
    if not created or created.get_value("description") != marker:
        raise RuntimeError("Project creation could not be verified; do not retry blindly.")
    if Config.get_value("install", "default_project") != before:
        raise RuntimeError("Default project changed unexpectedly; inspect the server.")
    return {"status": "created", "project": code, "database": created.get_database_name(),
            "default_project": before}


def admin_password(env_path=DEFAULT_CREDENTIALS):
    password = os.environ.get("NGS_TACTIC_ADMIN_PASSWORD", "")
    if password:
        return password
    for line in env_path.read_text(encoding="utf-8-sig").splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() == "TACTIC_ADMIN_PASSWORD":
            value = value.strip().strip("\"'")
            if value:
                return value
    raise ValueError("TACTIC_ADMIN_PASSWORD is missing; credentials were not copied.")


def admin_client(base_url, credentials=DEFAULT_CREDENTIALS, project="ngs_launcher", password=""):
    client = TacticClient(base_url, project)
    client.login("admin", password or admin_password(credentials))
    return client


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_TACTIC_URL)
    parser.add_argument("--credentials-env", type=Path, default=DEFAULT_CREDENTIALS)
    parser.add_argument("--apply", action="store_true", help="Create only ngs_launcher; default is read-only")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    client = admin_client(args.base_url, args.credentials_env, "admin")
    result = client.script(inspect.getsource(project_snapshot) + "\nreturn project_snapshot()")
    if args.apply:
        before = result
        created = client.script(inspect.getsource(create_ngs_project) + "\nreturn create_ngs_project()", timeout=180)
        after = client.script(inspect.getsource(project_snapshot) + "\nreturn project_snapshot()")
        old_projects = {p["code"]: p for p in before["projects"]}
        other_projects = {p["code"]: p for p in after["projects"] if p["code"] != "ngs_launcher"}
        if old_projects != other_projects or before["default_project"] != after["default_project"]:
            raise RuntimeError("Existing project metadata changed; inspect the server before continuing.")
        result = {"creation": created, "before": before, "after": after}
    output = json.dumps(result, ensure_ascii=False, indent=2)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(output + "\n", encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
